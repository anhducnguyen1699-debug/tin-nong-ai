"""Bước viết thẻ: với mỗi sự kiện đủ điểm, đưa vài bài nguồn cho Haiku viết thẻ tiếng Việt."""
from datetime import datetime, timezone
from html import escape

import anthropic
import httpx
import trafilatura

from . import db
from .config import system_prompt
from .llm import BudgetExceeded, LLMError
from .models import EventCard

NO_FULLTEXT_ORIGINS = {"google_news"}  # đường dẫn chuyển hướng, không bóc được
INVESTMENT_DISCLAIMER = "Không phải lời khuyên đầu tư."


def pick_sources(article_rows: list, max_sources: int) -> list:
    """Ưu tiên mỗi báo một bài. Bài bóc được nội dung (không phải Google News) và có mô tả xếp trước,
    vì bài Google News chỉ có tiêu đề làm thẻ nghèo thông tin; sau đó mới xét mức quan trọng."""
    def key(r):
        origin = r["origin"] if "origin" in r.keys() else ""
        return (origin not in NO_FULLTEXT_ORIGINS, bool(r["summary"]), int(r["importance"] or 0))

    ordered = sorted(article_rows, key=key, reverse=True)
    picked, seen = [], set()
    for r in ordered:
        if r["source"] not in seen:
            picked.append(r)
            seen.add(r["source"])
        if len(picked) == max_sources:
            break
    return picked


def fetch_fulltext(client: httpx.Client | None, row, max_chars: int) -> str:
    if client is None or row["origin"] in NO_FULLTEXT_ORIGINS:
        return ""
    try:
        resp = client.get(row["url"])
        resp.raise_for_status()
        text = trafilatura.extract(resp.text, include_comments=False, include_tables=False) or ""
    except Exception:
        return ""  # tường phí, bị chặn, lỗi mạng: dùng mô tả RSS thay thế
    return text[:max_chars]


def build_user_message(sources: list[dict]) -> str:
    parts = ["Viết thẻ tin cho sự kiện được các nguồn sau đưa tin:\n"]
    for s in sources:
        body = s["text"] or s["summary"] or "(chỉ có tiêu đề)"
        parts.append(
            f'<nguon id="{s["id"]}" ten="{escape(s["name"])}">\n'
            f"tieu_de: {escape(s['title'])}\n"
            f"noi_dung: {escape(body)}\n"
            f"</nguon>"
        )
    return "\n".join(parts)


def to_card(card: EventCard, sources: list[dict], model: str) -> dict:
    """Ghép thẻ AI viết với danh sách nguồn thật. Ý chính ghi sai mã nguồn thì bỏ mã đó."""
    valid_ids = {s["id"] for s in sources}
    key_points = [
        {"text": kp.text.strip(), "source_id": kp.source_id if kp.source_id in valid_ids else None}
        for kp in card.key_points[:3]
        if kp.text.strip()
    ]
    return {
        "title_vi": card.title_vi.strip(),
        "key_points": key_points,
        "why_it_matters": card.why_it_matters.strip(),
        "what_to_do": card.what_to_do.strip(),
        "affected_goals": list(dict.fromkeys(card.affected_goals)),
        "disclaimer": INVESTMENT_DISCLAIMER if card.is_investment_related else None,
        "sources": [{"id": s["id"], "name": s["name"], "url": s["url"], "title": s["title"]} for s in sources],
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def summarize(conn, llm, settings: dict, http_client: httpx.Client | None) -> tuple[int, list[str]]:
    cfg = settings["summarize"]
    sc = settings["scoring"]
    events = db.events_needing_card(
        conn, sc["summarize_min"], cfg["resummarize_after_hours"], cfg["resummarize_new_sources"],
        sc["max_summaries_per_run"],
    )
    system = system_prompt("summarize")
    written, errors = 0, []
    for event in events:
        rows = pick_sources(db.event_articles(conn, event["id"]), cfg["max_sources"])
        sources = []
        for i, r in enumerate(rows, start=1):
            text = fetch_fulltext(http_client, r, cfg["fulltext_chars"]) if i <= cfg["fetch_fulltext"] else ""
            sources.append({"id": i, "name": r["source"], "url": r["url"], "title": r["title"],
                            "summary": r["summary"] or "", "text": text})
        try:
            card = llm.parse("summarize", system, build_user_message(sources), EventCard, 6000)
        except BudgetExceeded as e:
            errors.append(str(e))
            break
        except (LLMError, anthropic.APIError) as e:
            errors.append(f"Viết thẻ lỗi ở sự kiện {event['id']}: {e}")
            continue
        db.save_card(conn, event["id"], to_card(card, sources, settings["models"]["summarize"]), event["source_count"])
        written += 1
    return written, errors
