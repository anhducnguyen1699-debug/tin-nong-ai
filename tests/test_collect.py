from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from pipeline.collect import (
    build_jobs, clean_text, dedupe, dedupe_key, google_news_url, normalize_url,
    parse_google_news, parse_hacker_news, parse_reddit, parse_rss,
)
from pipeline.models import Article


def rfc822(hours_ago: float) -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))


def rss_xml(items: list[tuple[str, str, str, float]]) -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><description>{d}</description>"
        f"<pubDate>{rfc822(h)}</pubDate></item>"
        for t, l, d, h in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def test_parse_rss_keeps_fresh_and_drops_old():
    xml = rss_xml([
        ("Fed giữ nguyên lãi suất", "https://vnexpress.net/a.html", "&lt;p&gt;Mô tả &amp;amp; chi tiết&lt;/p&gt;", 2),
        ("Tin cũ", "https://vnexpress.net/b.html", "", 72),
    ])
    out = parse_rss(xml, {"name": "VnExpress", "lang": "vi"}, max_age_hours=36, limit=30)
    assert [a.title for a in out] == ["Fed giữ nguyên lãi suất"]
    assert out[0].summary == "Mô tả & chi tiết"
    assert out[0].lang == "vi" and out[0].source == "VnExpress"


def test_parse_google_news_strips_publisher_suffix():
    xml = (
        '<?xml version="1.0"?><rss version="2.0"><channel><item>'
        "<title>Gold hits record high - Reuters</title>"
        "<link>https://news.google.com/rss/articles/abc</link>"
        f"<pubDate>{rfc822(1)}</pubDate>"
        '<source url="https://www.reuters.com">Reuters</source>'
        "</item></channel></rss>"
    ).encode()
    out = parse_google_news(xml, {"query": "gold", "lang": "en"}, 36, 30)
    assert out[0].title == "Gold hits record high"
    assert out[0].source == "Reuters"
    assert out[0].origin == "google_news"


def test_parse_hacker_news_filters_by_points():
    data = {"hits": [
        {"title": "New open model beats GPT", "url": "https://www.example.com/x", "points": 420,
         "objectID": "1", "created_at_i": 1763500000},
        {"title": "Small thing", "url": "https://e.com/y", "points": 20, "objectID": "2"},
        {"title": "Ask HN: no url", "url": None, "points": 300, "objectID": "3"},
    ]}
    out = parse_hacker_news(data, min_points=150)
    assert [a.hn_points for a in out] == [420, 300]
    assert out[0].source == "example.com"
    assert out[1].url == "https://news.ycombinator.com/item?id=3"


def test_parse_reddit_skips_low_score_and_stickied():
    data = {"data": {"children": [
        {"data": {"title": "Big news", "score": 900, "url": "https://site.org/p", "permalink": "/r/x/1",
                  "created_utc": 1763500000, "is_self": False}},
        {"data": {"title": "Low", "score": 5, "url": "https://a.b", "permalink": "/r/x/2"}},
        {"data": {"title": "Rules", "score": 5000, "stickied": True, "permalink": "/r/x/3"}},
    ]}}
    out = parse_reddit(data, {"sub": "geopolitics", "min_score": 300})
    assert len(out) == 1 and out[0].reddit_score == 900 and out[0].source == "site.org"


def test_normalize_url_removes_tracking_and_www():
    a = normalize_url("https://www.BBC.com/news/x/?utm_source=rss&id=5#top")
    b = normalize_url("http://bbc.com/news/x?id=5")
    assert a == b == "https://bbc.com/news/x?id=5"


def test_dedupe_by_url_and_google_news_by_title():
    arts = [
        Article(url="https://bbc.com/a?utm_medium=x", title="A", source="BBC"),
        Article(url="https://www.bbc.com/a", title="A khác tiêu đề", source="BBC"),
        Article(url="https://news.google.com/1", title="Gold hits record", source="Reuters", origin="google_news"),
        Article(url="https://news.google.com/2", title="Gold hits record!", source="Reuters", origin="google_news"),
    ]
    out = dedupe(arts)
    assert len(out) == 2
    assert dedupe_key(arts[2]) == dedupe_key(arts[3])


def test_clean_text_truncates():
    assert len(clean_text("x" * 1000, limit=100)) == 100
    assert clean_text(None) == ""


def test_build_jobs_orders_hot_sources_first():
    jobs = build_jobs({
        "rss": [{"name": "A", "url": "u"}, {"name": "B", "url": "v", "enabled": False}],
        "google_news": [{"query": "giá vàng", "lang": "vi"}],
        "hacker_news": {"enabled": True},
        "reddit": [{"sub": "economics"}],
    })
    assert [j["kind"] for j in jobs] == ["hn", "reddit", "rss", "gnews"]
    assert "hl=vi" in google_news_url("giá vàng", "vi")
