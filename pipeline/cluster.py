"""Bước gộp sự kiện: gộp sơ bộ bằng so khớp tiêu đề, phần còn lại để Haiku quyết định."""
from html import escape

import anthropic
from rapidfuzz import fuzz

from . import db
from .classify import chunks
from .config import system_prompt
from .llm import LLMError
from .models import ClusterResult


def best_fuzzy_match(title: str, event_titles: dict[int, list[str]], threshold: int) -> int | None:
    """Tìm sự kiện có tiêu đề gần giống nhất. token_sort_ratio đủ chặt để không gộp nhầm tiêu đề ngắn."""
    best_id, best = None, 0.0
    for event_id, titles in event_titles.items():
        for t in titles:
            ratio = fuzz.token_sort_ratio(title.lower(), t.lower())
            if ratio > best:
                best_id, best = event_id, ratio
    return best_id if best >= threshold else None


def build_user_message(events: list[tuple[int, str]], batch: list) -> str:
    lines = ["<su_kien_dang_mo>"]
    lines += [f"{eid}: {escape(title)}" for eid, title in events] or ["(chưa có)"]
    lines += ["</su_kien_dang_mo>", "", "<bai_moi>"]
    lines += [f'{i}. [{escape(r["source"])}] {escape(r["title"])}' for i, r in enumerate(batch, start=1)]
    lines.append("</bai_moi>")
    return "\n".join(lines)


def apply_decisions(conn, batch: list, result: ClusterResult | None, valid_event_ids: set[int]) -> set[int]:
    """Gắn bài vào sự kiện theo quyết định của AI. Quyết định thiếu hoặc sai thì bài thành sự kiện riêng."""
    touched: set[int] = set()
    groups: dict[str, int] = {}
    decided = {}
    if result:
        for d in result.decisions:
            if 1 <= d.article_id <= len(batch):
                decided[d.article_id] = d
    for i, row in enumerate(batch, start=1):
        d = decided.get(i)
        if d and d.event_id in valid_event_ids:
            event_id = d.event_id
        else:
            label = (d.new_group.strip() if d and d.new_group else "") or f"_rieng_{row['id']}"
            if label not in groups:
                groups[label] = db.create_event(conn, row["title"], row["category"])
            event_id = groups[label]
        db.attach(conn, row["id"], event_id)
        touched.add(event_id)
    return touched


def cluster(conn, llm, settings: dict) -> tuple[dict, list[str]]:
    cfg = settings["cluster"]
    weights = settings["scoring"]["weights"]
    rows = db.unclustered_articles(conn, cfg["min_importance"])
    stats = {"fuzzy": 0, "ai": 0, "new_events": 0}
    errors: list[str] = []
    if not rows:
        return stats, errors

    events = db.open_events(conn, cfg["window_hours"], cfg["max_events_in_prompt"])
    event_titles = {e["id"]: [e["title"]] + [a["title"] for a in db.event_articles(conn, e["id"])] for e in events}
    display = {e["id"]: e["title_vi"] or e["title"] for e in events}
    touched: set[int] = set()

    # 1) Gộp sơ bộ: tiêu đề gần như trùng khớp
    remaining = []
    for r in rows:
        eid = best_fuzzy_match(r["title"], event_titles, cfg["fuzzy_merge"])
        if eid:
            db.attach(conn, r["id"], eid)
            event_titles[eid].append(r["title"])
            touched.add(eid)
            stats["fuzzy"] += 1
        else:
            remaining.append(r)

    # 2) Phần còn lại: hỏi AI, mỗi lần một lô
    system = system_prompt("cluster")
    for batch in chunks(remaining, cfg["batch_size"]):
        event_list = list(display.items())[-cfg["max_events_in_prompt"]:]
        before = len(display)
        try:
            result = llm.parse("cluster", system, build_user_message(event_list, batch), ClusterResult, 6000)
        except (LLMError, anthropic.APIError) as e:
            errors.append(f"Gộp sự kiện lỗi, tạm để mỗi bài một sự kiện: {e}")
            result = None
        new_touched = apply_decisions(conn, batch, result, {eid for eid, _ in event_list})
        touched |= new_touched
        stats["ai"] += len(batch)
        # Sự kiện mới tạo ở lô này được đưa vào danh sách cho lô sau
        for eid in new_touched:
            if eid not in display:
                row = conn.execute("SELECT title FROM events WHERE id=?", (eid,)).fetchone()
                display[eid] = row["title"]
        stats["new_events"] += len(display) - before

    for eid in touched:
        db.recompute_event(conn, eid, weights)
    stats["events_touched"] = len(touched)
    return stats, errors
