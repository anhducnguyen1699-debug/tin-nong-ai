"""Bước chấm điểm: Haiku xếp chuyên mục, chấm mức quan trọng và mức ảnh hưởng cho từng bài."""
import json
from html import escape

import anthropic

from . import db
from .config import ROOT, system_prompt
from .llm import LLMError
from .models import ArticleScore, ClassifyResult
from .scoring import clamp


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def preference_block(liked: list[str], disliked: list[str]) -> str:
    if not liked and not disliked:
        return ""
    lines = ["<so_thich>"]
    if liked:
        lines.append("Người đọc thấy HỮU ÍCH:")
        lines += [f"- {escape(t)}" for t in liked]
    if disliked:
        lines.append("Người đọc muốn BỎ QUA:")
        lines += [f"- {escape(t)}" for t in disliked]
    lines.append("</so_thich>\n")
    return "\n".join(lines)


def build_user_message(batch: list, liked: list[str], disliked: list[str]) -> str:
    """Dùng số thứ tự 1..n thay cho mã trong cơ sở dữ liệu để AI khó nhầm."""
    parts = [preference_block(liked, disliked), "Chấm điểm các bài sau:\n"]
    for i, row in enumerate(batch, start=1):
        parts.append(
            f'<bai id="{i}">\n'
            f"nguon: {escape(row['source'])}\n"
            f"tieu_de: {escape(row['title'])}\n"
            f"mo_ta: {escape((row['summary'] or '')[:400])}\n"
            f"</bai>"
        )
    return "\n".join(parts)


def normalize_score(item: ArticleScore) -> dict:
    """Kẹp mọi điểm về đúng khoảng; bài bị bỏ qua thì ép điểm thấp."""
    s = {
        "category": item.category,
        "importance": clamp(item.importance, 1, 10),
        "impact_dau_tu": clamp(item.impact_dau_tu, 0, 10),
        "impact_nghe_y": clamp(item.impact_nghe_y, 0, 10),
        "impact_dia_chinh_tri": clamp(item.impact_dia_chinh_tri, 0, 10),
        "impact_viet_nam": clamp(item.impact_viet_nam, 0, 10),
        "reason": (item.reason or "").strip()[:300],
    }
    if s["category"] == "bo_qua":
        s["importance"] = min(s["importance"], 2)
        for k in ("impact_dau_tu", "impact_nghe_y", "impact_dia_chinh_tri", "impact_viet_nam"):
            s[k] = 0
    return s


def map_results(batch: list, result: ClassifyResult) -> dict[int, dict]:
    """Đổi số thứ tự trong lô về mã bài thật. Mục có số lạ bị bỏ; bài bị AI bỏ sót sẽ được chấm lại ở lượt sau."""
    scores = {}
    for item in result.items:
        if 1 <= item.id <= len(batch):
            scores[batch[item.id - 1]["id"]] = normalize_score(item)
    return scores


def classify_rows_for_eval(llm, settings: dict, items: list[dict]) -> list[dict | None]:
    """Chấm lại các tin trong bộ đề chuẩn, không ghi gì vào cơ sở dữ liệu. Kết quả xếp đúng thứ tự đầu vào."""
    cfg = settings["classify"]
    rows = [{"id": i, "source": g.get("source", ""), "title": g["title"], "summary": g.get("summary", "")}
            for i, g in enumerate(items)]
    out: list[dict | None] = [None] * len(rows)
    system = system_prompt("classify")
    for batch in chunks(rows, cfg["batch_size"]):
        try:
            result = llm.parse("classify", system, build_user_message(batch, [], []), ClassifyResult, cfg["max_tokens"])
        except (LLMError, anthropic.APIError):
            continue
        for row_id, score in map_results(batch, result).items():
            out[row_id] = score
    return out


def golden_examples(per_side: int = 15) -> tuple[list[str], list[str]]:
    """Ví dụ sở thích lấy từ bộ đề chuẩn (mọi phiếu bạn từng chấm, kể cả của dữ liệu cũ)."""
    path = ROOT / "tests" / "golden.json"
    if not path.exists():
        return [], []
    items = json.loads(path.read_text(encoding="utf-8"))
    title = lambda g: g.get("title_vi") or g.get("title") or ""
    liked = [title(g) for g in reversed(items) if g.get("user_worth")]
    disliked = [title(g) for g in reversed(items) if g.get("user_worth") is False]
    return liked[:per_side], disliked[:per_side]


def preference_examples(conn, per_side: int = 15) -> tuple[list[str], list[str]]:
    """Phản hồi mới trong cơ sở dữ liệu đứng trước; thiếu thì bù bằng bộ đề chuẩn, bỏ trùng."""
    liked, disliked = db.feedback_examples(conn, per_side)
    g_liked, g_disliked = golden_examples(per_side)
    fill = lambda a, b: (a + [t for t in b if t and t not in a])[:per_side]
    return fill(liked, g_liked), fill(disliked, g_disliked)


def classify(conn, llm, settings: dict, rows: list) -> tuple[int, list[str]]:
    cfg = settings["classify"]
    liked, disliked = preference_examples(conn)
    system = system_prompt("classify")
    scored, errors = 0, []
    for batch in chunks(rows, cfg["batch_size"]):
        user = build_user_message(batch, liked, disliked)
        try:
            result = llm.parse("classify", system, user, ClassifyResult, cfg["max_tokens"])
        except (LLMError, anthropic.APIError) as e:
            errors.append(f"Chấm điểm lỗi ở một lô {len(batch)} bài: {e}")
            continue
        scores = map_results(batch, result)
        db.save_scores(conn, scores)
        scored += len(scores)
    return scored, errors
