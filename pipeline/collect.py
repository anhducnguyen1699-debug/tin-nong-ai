"""Thu thập tin từ RSS, Google News, Hacker News, Reddit. Không dùng AI.

Kiểm tra nguồn nào còn sống:  python -m pipeline.collect --check
"""
import argparse
import hashlib
import html
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import feedparser
import httpx

from .config import load_settings, load_sources
from .models import Article

TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ocid", "cmpid", "ref", "rss", "src")
GOOGLE_NEWS_LOCALES = {
    "en": "hl=en-US&gl=US&ceid=US:en",
    "vi": "hl=vi&gl=VN&ceid=VN:vi",
}


# ---------- Làm sạch và lọc trùng ----------
def clean_text(raw: str | None, limit: int = 600) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(TRACKING_PARAMS)]
    path = p.path.rstrip("/") or "/"
    return urlunparse(("https", host, path, "", urlencode(query), ""))


def normalize_title(title: str) -> str:
    t = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", t).strip()


def dedupe_key(article: Article) -> str:
    if article.origin == "google_news":
        # Đường dẫn Google News là đường dẫn chuyển hướng, mỗi lần một khác → lọc theo tiêu đề + tên báo
        raw = f"{normalize_title(article.title)}|{article.source.lower()}"
        return "gn:" + hashlib.sha1(raw.encode()).hexdigest()
    return "u:" + hashlib.sha1(normalize_url(article.url).encode()).hexdigest()


def dedupe(articles: list[Article]) -> list[tuple[str, Article]]:
    """Bỏ bài trùng trong cùng một lượt, giữ bài xuất hiện trước."""
    seen: set[str] = set()
    out = []
    for a in articles:
        key = dedupe_key(a)
        if key not in seen:
            seen.add(key)
            out.append((key, a))
    return out


# ---------- Đọc từng loại nguồn ----------
def _entry_time(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def _too_old(dt: datetime | None, max_age_hours: int) -> bool:
    return dt is not None and dt < datetime.now(timezone.utc) - timedelta(hours=max_age_hours)


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def parse_rss(content: bytes, source: dict, max_age_hours: int, limit: int) -> list[Article]:
    feed = feedparser.parse(content)
    out = []
    for entry in feed.entries[:limit]:
        title = clean_text(entry.get("title"), 300)
        link = entry.get("link")
        if not title or not link:
            continue
        published = _entry_time(entry)
        if _too_old(published, max_age_hours):
            continue
        out.append(Article(
            url=link, title=title, source=source["name"],
            summary=clean_text(entry.get("summary") or entry.get("description")),
            lang=source.get("lang", "en"), published_at=published, origin="rss",
        ))
    return out


def google_news_url(query: str, lang: str, window: str = "1d") -> str:
    locale = GOOGLE_NEWS_LOCALES.get(lang, GOOGLE_NEWS_LOCALES["en"])
    return f"https://news.google.com/rss/search?q={quote_plus(query + f' when:{window}')}&{locale}"


def parse_google_news(content: bytes, query: dict, max_age_hours: int, limit: int) -> list[Article]:
    feed = feedparser.parse(content)
    out = []
    for entry in feed.entries[:limit]:
        title = clean_text(entry.get("title"), 300)
        publisher = (entry.get("source") or {}).get("title") or "Google News"
        suffix = f" - {publisher}"
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        published = _entry_time(entry)
        if not title or _too_old(published, max_age_hours):
            continue
        out.append(Article(
            url=entry.get("link", ""), title=title, source=publisher, summary="",
            lang=query.get("lang", "en"), published_at=published, origin="google_news",
            extra={"query": query["query"]},
        ))
    return out


def parse_hacker_news(data: dict, min_points: int) -> list[Article]:
    out = []
    for hit in data.get("hits", []):
        points = int(hit.get("points") or 0)
        title = (hit.get("title") or "").strip()
        if points < min_points or not title:
            continue
        hn_link = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        url = hit.get("url") or hn_link
        created = hit.get("created_at_i")
        out.append(Article(
            url=url, title=title, source=domain_of(url) if hit.get("url") else "Hacker News",
            summary="", lang="en",
            published_at=datetime.fromtimestamp(created, timezone.utc) if created else None,
            hn_points=points, origin="hacker_news", extra={"discussion": hn_link},
        ))
    return out


def parse_reddit(data: dict, sub: dict) -> list[Article]:
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        score = int(d.get("score") or 0)
        title = (d.get("title") or "").strip()
        if score < sub.get("min_score", 0) or not title or d.get("stickied"):
            continue
        permalink = "https://www.reddit.com" + d.get("permalink", "")
        is_self = d.get("is_self", False)
        url = permalink if is_self else d.get("url", permalink)
        out.append(Article(
            url=url, title=title,
            source=f"Reddit r/{sub['sub']}" if is_self else domain_of(url),
            summary=clean_text(d.get("selftext"), 500), lang="en",
            published_at=datetime.fromtimestamp(d["created_utc"], timezone.utc) if d.get("created_utc") else None,
            reddit_score=score, origin="reddit", extra={"discussion": permalink},
        ))
    return out


# ---------- Điều phối ----------
def make_client(settings: dict) -> httpx.Client:
    c = settings["collect"]
    return httpx.Client(
        timeout=c["timeout_seconds"], follow_redirects=True, headers={"User-Agent": c["user_agent"]}
    )


def build_jobs(sources: dict, window: str = "1d") -> list[dict]:
    """Mỗi việc = một đường dẫn cần tải + cách đọc nó. Hacker News và Reddit xếp trước để giữ điểm nóng."""
    jobs = []
    hn = sources.get("hacker_news") or {}
    if hn.get("enabled"):
        jobs.append({"kind": "hn", "name": "Hacker News",
                     "url": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50", "cfg": hn})
    for sub in sources.get("reddit") or []:
        jobs.append({"kind": "reddit", "name": f"Reddit r/{sub['sub']}",
                     "url": f"https://www.reddit.com/r/{sub['sub']}/top.json?t=day&limit=25", "cfg": sub})
    for src in sources.get("rss") or []:
        if src.get("enabled", True):
            jobs.append({"kind": "rss", "name": src["name"], "url": src["url"], "cfg": src})
    queries = list(sources.get("google_news") or [])
    if window != "1d":
        queries += sources.get("google_news_week") or []
    for q in queries:
        jobs.append({"kind": "gnews", "name": f"Google News: {q['query']}",
                     "url": google_news_url(q["query"], q.get("lang", "en"), window), "cfg": q})
    return jobs


def run_job(client: httpx.Client, job: dict, settings: dict) -> list[Article]:
    c = settings["collect"]
    resp = client.get(job["url"])
    resp.raise_for_status()
    if job["kind"] == "hn":
        return parse_hacker_news(resp.json(), job["cfg"].get("min_points", 100))
    if job["kind"] == "reddit":
        return parse_reddit(resp.json(), job["cfg"])
    if job["kind"] == "gnews":
        return parse_google_news(resp.content, job["cfg"], c["max_age_hours"], c["per_source_limit"])
    return parse_rss(resp.content, job["cfg"], c["max_age_hours"], c["per_source_limit"])


def collect_all(settings: dict, sources: dict) -> tuple[list[tuple[str, Article]], dict]:
    """Tải mọi nguồn song song. Trả về (danh sách bài đã lọc trùng, báo cáo từng nguồn)."""
    jobs = build_jobs(sources, settings["collect"].get("gnews_window", "1d"))
    report: dict[str, str] = {}
    results: list[list[Article]] = [[] for _ in jobs]
    with make_client(settings) as client, ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(run_job, client, job, settings): i for i, job in enumerate(jobs)}
        for fut, i in futures.items():
            name = jobs[i]["name"]
            try:
                results[i] = fut.result()
                report[name] = f"{len(results[i])} bài"
            except Exception as e:  # một nguồn lỗi không được làm hỏng cả lượt
                report[name] = f"LỖI: {type(e).__name__}: {str(e)[:120]}"
    articles = [a for batch in results for a in batch]
    return dedupe(articles), report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Thu thập tin (không dùng AI)")
    parser.add_argument("--check", action="store_true", help="Kiểm tra từng nguồn và in số bài lấy được")
    args = parser.parse_args(argv)
    settings, sources = load_settings(), load_sources()
    items, report = collect_all(settings, sources)
    width = max(len(n) for n in report) if report else 10
    for name, status in report.items():
        print(f"{name:<{width}}  {status}")
    print(f"\nTổng: {len(items)} bài sau khi lọc trùng.")
    if not args.check:
        for _, a in items[:20]:
            print(f"- [{a.source}] {a.title}")
    dead = [n for n, s in report.items() if s.startswith("LỖI") or s == "0 bài"]
    return 1 if len(dead) == len(report) else 0


if __name__ == "__main__":
    sys.exit(main())
