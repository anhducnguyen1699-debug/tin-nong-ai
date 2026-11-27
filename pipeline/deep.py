"""Bài phân tích sâu cho tin từ 7 điểm. Claude trong phiên đọc toàn văn nguồn bằng WebSearch/WebFetch rồi viết bài;
mã này chỉ liệt kê việc cần làm, kiểm khuôn bài, lưu và xuất trang.

  python -m pipeline.deep list               # các sự kiện cần bài, kèm nguồn và khuôn bài
  python -m pipeline.deep write <mã> < bai.md  # kiểm khuôn rồi lưu bài cho sự kiện <mã>
  python -m pipeline.deep render             # xuất lại trang out/preview.html

Luật viết bài: pipeline/prompts/deep.md
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import ROOT, load_settings
from .main import DEFAULT_DB

SECTIONS = [
    "Chuyện gì đã xảy ra",
    "Bối cảnh",
    "Các bên nói gì và số liệu",
    "Phân tích: liên kết các nguồn",
    "Tác động tới bạn",
    "Theo dõi tiếp",
]
SOURCES_HEADING = "Nguồn"
CITE = re.compile(r"\[(\d+)\]")
QUOTE = re.compile(r"[“\"]([^”\"]{2,})[”\"]")


class ArticleError(ValueError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", CITE.sub("", text)))


def parse_source_line(line: str) -> dict:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 5:
        raise ArticleError(f"Dòng nguồn cần ít nhất 5 phần ngăn bởi '|': {line}")
    sid, name, url, kind, read = parts[:5]
    note = " | ".join(parts[5:]).strip()
    try:
        sid_num = int(sid.strip(". "))
    except ValueError:
        raise ArticleError(f"Số thứ tự nguồn không hợp lệ: {line}") from None
    if not url.startswith("http"):
        raise ArticleError(f"Nguồn {sid_num} thiếu đường dẫn http: {line}")
    k = _norm(kind)
    r = _norm(read)
    return {
        "id": sid_num, "name": name, "url": url,
        "kind": "goc" if ("gốc" in k or "goc" in k) else "bao",
        "full": ("toàn văn" in r or "toan van" in r or "toan_van" in r),
        "note": note,
    }


def parse_article(text: str, settings: dict) -> dict:
    """Đọc bài markdown theo khuôn trong prompts/deep.md. Sai khuôn thì báo lỗi cụ thể để viết lại."""
    cfg = settings["deep"]
    title, sapo, current = "", [], None
    sections: dict[str, list[str]] = {}
    for raw in text.strip().splitlines():
        line = raw.rstrip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif line.startswith(">") and current is None:
            sapo.append(line.lstrip("> ").strip())
        elif current is not None:
            sections[current].append(line)

    if not title:
        raise ArticleError("Thiếu tiêu đề (dòng bắt đầu bằng '# ').")
    if not " ".join(sapo).strip():
        raise ArticleError("Thiếu sapo (dòng bắt đầu bằng '> ' ngay dưới tiêu đề).")
    by_norm = {_norm(k): k for k in sections}
    missing = [s for s in SECTIONS + [SOURCES_HEADING] if _norm(s) not in by_norm]
    if missing:
        raise ArticleError("Thiếu mục: " + ", ".join(missing) + ". Tên mục phải đúng như khuôn.")

    sources = [parse_source_line(l) for l in sections[by_norm[_norm(SOURCES_HEADING)]] if l.strip()]
    ids = [s["id"] for s in sources]
    if len(ids) != len(set(ids)):
        raise ArticleError("Có số thứ tự nguồn bị trùng.")
    full = sum(1 for s in sources if s["full"])
    if full < cfg["min_full_sources"]:
        raise ArticleError(f"Mới có {full} nguồn đọc toàn văn, cần ít nhất {cfg['min_full_sources']}.")

    body_sections = []
    for name in SECTIONS:
        body = "\n".join(sections[by_norm[_norm(name)]]).strip()
        if not body:
            raise ArticleError(f"Mục '{name}' đang trống.")
        body_sections.append({"heading": name, "body": body})
    body_text = "\n".join(s["body"] for s in body_sections)

    words = word_count(body_text) + word_count(" ".join(sapo))
    if not cfg["min_words"] <= words <= cfg["max_words"]:
        raise ArticleError(f"Bài dài {words} từ, cần trong khoảng {cfg['min_words']}–{cfg['max_words']}.")

    cited = {int(n) for n in CITE.findall(body_text + " ".join(sapo))}
    unknown = sorted(cited - set(ids))
    if unknown:
        raise ArticleError(f"Mã nguồn {unknown} được dẫn trong bài nhưng không có trong mục Nguồn.")
    if not cited:
        raise ArticleError("Bài chưa dẫn nguồn nào bằng [n].")

    for q in QUOTE.findall(body_text):
        if word_count(q) > cfg["max_quote_words"]:
            raise ArticleError(f"Trích dẫn nguyên văn dài quá {cfg['max_quote_words']} từ, hãy diễn đạt lại: “{q[:80]}…”")

    warnings = []
    if not any(s["kind"] == "goc" for s in sources):
        warnings.append("Chưa có nguồn gốc (thông cáo, nghiên cứu, số liệu chính thức).")
    uncited = [s["id"] for s in sources if s["id"] not in cited]
    if uncited:
        warnings.append(f"Nguồn {uncited} có trong danh sách nhưng chưa được dẫn trong bài.")

    return {
        "title": title,
        "sapo": " ".join(sapo).strip(),
        "sections": body_sections,
        "sources": sources,
        "words": words,
        "full_sources": full,
        "warnings": warnings,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def list_pending(conn, settings: dict) -> str:
    cfg = settings["deep"]
    events = db.events_needing_article(conn, cfg["min_score"], 24, cfg["rewrite_after_hours"],
                                       cfg["rewrite_new_sources"], cfg["max_per_run"])
    if not events:
        return "Không có sự kiện nào cần bài phân tích."
    out = [f"{len(events)} sự kiện cần bài phân tích (làm theo thứ tự, điểm cao trước). Luật: pipeline/prompts/deep.md\n"]
    for e in events:
        card = json.loads(e["card_json"]) if e["card_json"] else None
        out.append(f"=== Sự kiện {e['id']} · {e['score']}/10 · {e['source_count']} nguồn ===")
        out.append(f"Tiêu đề: {card['title_vi'] if card else e['title']}")
        if card:
            out += [f"  • {kp['text']}" for kp in card["key_points"]]
        out.append("Nguồn đã thu thập (đọc toàn văn từng bài):")
        for a in db.event_articles(conn, e["id"]):
            hint = "  ← Google News: tìm bài gốc bằng WebSearch theo tiêu đề + tên báo" if a["origin"] == "google_news" else ""
            out.append(f"  - [{a['source']}] {a['title']}\n    {a['url']}{hint}")
        out.append(f"Ghi bài: .venv/bin/python -m pipeline.deep write {e['id']} <<'EOF' ... EOF\n")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Bài phân tích sâu cho tin từ 7 điểm")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    w = sub.add_parser("write")
    w.add_argument("event_id", type=int)
    sub.add_parser("render")
    args = p.parse_args(argv)

    settings = load_settings()
    conn = db.connect(args.db)
    if args.cmd == "list":
        print(list_pending(conn, settings))
        return 0
    if args.cmd == "write":
        event = conn.execute("SELECT * FROM events WHERE id=?", (args.event_id,)).fetchone()
        if not event:
            print(f"Không có sự kiện {args.event_id}.")
            return 1
        try:
            article = parse_article(sys.stdin.read(), settings)
        except ArticleError as e:
            print(f"CHƯA LƯU: {e}")
            return 2
        db.save_article(conn, args.event_id, article, event["source_count"])
        print(f"Đã lưu bài cho sự kiện {args.event_id}: {article['words']} từ, {len(article['sources'])} nguồn "
              f"({article['full_sources']} đọc toàn văn).")
        for warn in article["warnings"]:
            print(f"  Lưu ý: {warn}")
        return 0
    from .preview import write_preview
    out = write_preview(conn, settings, ROOT / "out" / "preview.html",
                        byline="Claude chấm điểm, viết thẻ và bài phân tích trong phiên (không gọi API).")
    print(f"Đã xuất trang: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
