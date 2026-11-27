"""Xuất trang "Bản tin nóng" từ dữ liệu SQLite.

Trang này được đăng lên claude.ai (riêng tư) để đọc trên điện thoại và chấm từng tin.
Phiếu chấm lưu trong kho dữ liệu của trang, Claude đọc lại rồi nhập bằng `python -m pipeline.ratings`.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import db

TEMPLATE = Path(__file__).resolve().parent / "preview_template.html"


def event_key(event) -> str:
    """Khóa ổn định cho phiếu chấm: mã sự kiện + thời điểm tạo (không trùng kể cả khi làm lại file SQLite)."""
    stamp = (event["first_seen"] or "").replace("-", "").replace(":", "").split("+")[0]
    return f"e{event['id']}-{stamp}"


def build_data(conn, settings: dict, since_hours: int = 24, byline: str = "") -> dict:
    show_min = settings["scoring"]["show_min"]
    events = []
    for e in db.events_for_preview(conn, since_hours, show_min):
        arts = db.event_articles(conn, e["id"])
        top = arts[0] if arts else None
        events.append({
            "key": event_key(e),
            "id": e["id"],
            "category": e["category"],
            "score": e["score"],
            "importance": e["importance"],
            "impact": e["impact"],
            "hotness": e["hotness"],
            "source_count": e["source_count"],
            "title": e["title"],
            "reason": top["reason"] if top else "",
            "summary": (top["summary"] or "")[:400] if top else "",
            "card": json.loads(e["card_json"]) if e["card_json"] else None,
            "articles": [{"source": a["source"], "title": a["title"], "url": a["url"]} for a in arts[:8]],
        })
    last_run = conn.execute("SELECT stats_json FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    cost = json.loads(last_run["stats_json"]).get("cost_usd", 0) if last_run and last_run["stats_json"] else 0
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "show_min": show_min,
        "byline": byline,
        "run_cost_usd": cost,
        "events": events,
    }


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)


def write_preview(conn, settings: dict, out_path: Path, byline: str = "") -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(build_data(conn, settings, byline=byline)), encoding="utf-8")
    return out_path
