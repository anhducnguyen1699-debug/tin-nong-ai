"""Bài phân tích sâu cho tin từ 7 điểm. Claude trong phiên đọc toàn văn nguồn bằng WebSearch/WebFetch rồi viết bài;
mã này chỉ liệt kê việc cần làm, kiểm khuôn bài, lưu và xuất trang.

  python -m pipeline.deep list                 # các sự kiện cần bài, kèm nguồn
  python -m pipeline.deep fetch <mã> <url>     # tải nguyên văn một nguồn vào data/nguon/<mã>/
  python -m pipeline.deep save <mã> <url>      # (dự phòng) lưu kết quả WebFetch, ghi là bản tóm tắt
  python -m pipeline.deep draft <mã> < bai.md  # kiểm khuôn, tách câu cần bằng chứng, ra đề đối chiếu
  python -m pipeline.deep verify <mã> < kq     # ghi kết quả của tác tử đối chiếu; máy soát lại đoạn trích
  python -m pipeline.deep write <mã>           # lưu bài khi mọi câu đã có bằng chứng
  python -m pipeline.deep render               # xuất lại trang out/preview.html

Luật viết bài: pipeline/prompts/deep.md. Luật đối chiếu: pipeline/prompts/verify.md
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
from .verify import (LABEL, SOURCES_DIR, VerifyError, apply_answer, extract_claims, failing, fetch_source, fresh_state,
                     load_state, new_draft, parse_answer, read_source, save_source, save_state, state_path,
                     summarize, verification_block, verify_task)

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
        out.append(f"Tải nguồn: .venv/bin/python -m pipeline.deep fetch {e['id']} <url>  ·  "
                   f"Gửi bản nháp: .venv/bin/python -m pipeline.deep draft {e['id']} <<'EOF' ... EOF\n")
    return "\n".join(out)


def _sources_by_id(article: dict) -> dict[int, dict]:
    return {s["id"]: s for s in article["sources"]}


def cmd_draft(conn, settings, event_id: int, text: str, base: Path) -> int:
    try:
        article = parse_article(text, settings)
    except ArticleError as e:
        print(f"CHƯA NHẬN: {e}")
        return 2
    missing = [s["id"] for s in article["sources"] if s["full"] and read_source(event_id, s["url"], base) is None]
    if missing:
        print(f"CHƯA NHẬN: nguồn {missing} ghi 'toàn văn' nhưng chưa lưu file. Chạy `deep fetch {event_id} <url>` "
              "(hoặc `deep save` nếu phải đọc bằng WebFetch), hoặc đổi thành 'chỉ tiêu đề'.")
        return 2
    max_rounds = settings["deep"].get("max_fix_rounds", 2) + 1
    state = load_state(event_id, base)
    if state.get("written"):
        state = fresh_state(state.get("history"))  # viết lại bài: bắt đầu vòng mới
    if state["rounds"] >= max_rounds:
        print(f"DỪNG: đã qua {max_rounds - 1} vòng sửa mà vẫn còn câu chưa có bằng chứng. "
              "Bỏ sự kiện này và ghi vào báo cáo: 'bỏ vì không kiểm chứng được'.")
        return 3
    state = new_draft(state, text, extract_claims(article))
    save_state(event_id, state, base)
    task = state_path(event_id, base).with_name(f"de_doi_chieu_vong{state['rounds']}.md")
    task.write_text(verify_task(event_id, state, article["sources"], base), encoding="utf-8")
    pending = sum(1 for c in state["claims"] if str(c["n"]) not in state["results"])
    print(f"Đã nhận bản nháp vòng {state['rounds']}: {article['words']} từ, {len(state['claims'])} câu cần bằng chứng "
          f"({len(state['claims']) - pending} câu giữ kết quả khớp từ vòng trước, {pending} câu cần đối chiếu).")
    if pending:
        print(f"Giao cho tác tử đối chiếu (mô hình sonnet, phiên mới): đọc pipeline/prompts/verify.md rồi làm đề {task}")
        print(f"Ghi câu trả lời: .venv/bin/python -m pipeline.deep verify {event_id} <<'EOF' ... EOF")
    else:
        print(f"Mọi câu đã có kết luận. Chạy: .venv/bin/python -m pipeline.deep write {event_id}")
    return 0


def cmd_verify(conn, settings, event_id: int, text: str, base: Path) -> int:
    state = load_state(event_id, base)
    if not state["draft"]:
        print(f"Chưa có bản nháp. Chạy `deep draft {event_id}` trước.")
        return 1
    article = parse_article(state["draft"], settings)
    try:
        state = apply_answer(state, parse_answer(text), _sources_by_id(article), event_id, settings, base)
    except VerifyError as e:
        print(f"CHƯA NHẬN: {e}")
        return 2
    save_state(event_id, state, base)
    s = summarize(state)
    print(f"Vòng {state['rounds']}: {s['ok']}/{s['checked']} câu có bằng chứng khớp, {s['opinion']} câu nhận định, "
          f"{s['pending']} câu chưa kiểm." + (f" {s['weak']} câu chỉ dựa vào bản tóm tắt WebFetch." if s["weak"] else ""))
    if state["first"] and state["rounds"] == 1:
        f = state["first"]
        print(f"Lần viết đầu: {f['ok']}/{f['checked']} câu khớp.")
    bad = failing(state)
    if not bad:
        print(f"Đủ bằng chứng. Chạy: .venv/bin/python -m pipeline.deep write {event_id}")
        return 0
    print("Các câu phải sửa theo bằng chứng hoặc xóa, rồi gửi lại cả bài bằng `deep draft`:")
    for c, r in bad:
        ev = "; ".join(f"nguồn {e['source']}: “{e['text']}”" for e in r.get("evidence", []))
        print(f"  {c['n']}. [{LABEL[r['verdict']]}] {c['text']}")
        if r.get("note"):
            print(f"     → {r['note']}")
        if ev:
            print(f"     → {ev}")
    return 0


def cmd_write(conn, settings, event_id: int, base: Path) -> int:
    event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        print(f"Không có sự kiện {event_id}.")
        return 1
    state = load_state(event_id, base)
    if not state["draft"]:
        print(f"CHƯA LƯU: chưa có bản nháp. Chạy `deep draft {event_id}` rồi đối chiếu trước.")
        return 2
    bad = failing(state)
    if bad:
        print(f"CHƯA LƯU: còn {len(bad)} câu chưa có bằng chứng khớp (số {[c['n'] for c, _ in bad]}). "
              "Sửa hoặc xóa các câu đó, `deep draft` lại rồi đối chiếu.")
        return 2
    article = parse_article(state["draft"], settings)
    article["verification"] = verification_block(state)
    db.save_article(conn, event_id, article, event["source_count"])
    save_state(event_id, {**state, "written": True}, base)
    v = article["verification"]
    print(f"Đã lưu bài cho sự kiện {event_id}: {article['words']} từ, {len(article['sources'])} nguồn "
          f"({article['full_sources']} đọc toàn văn). Kiểm chứng: {v['ok']}/{v['checked']} câu có bằng chứng; "
          f"lần viết đầu khớp {v['first_ok']}/{v['first_checked']}; {v['rounds']} vòng.")
    for warn in article["warnings"]:
        print(f"  Lưu ý: {warn}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Bài phân tích sâu cho tin từ 7 điểm")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--nguon", default=str(SOURCES_DIR), help="Thư mục lưu nguyên văn nguồn (không commit)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("fetch", "save"):
        c = sub.add_parser(name)
        c.add_argument("event_id", type=int)
        c.add_argument("url")
    for name in ("draft", "verify", "write"):
        sub.add_parser(name).add_argument("event_id", type=int)
    sub.add_parser("render")
    args = p.parse_args(argv)

    settings = load_settings()
    base = Path(args.nguon)
    if args.cmd == "fetch":
        try:
            path, words = fetch_source(args.event_id, args.url, settings, base)
        except VerifyError as e:
            print(f"CHƯA LƯU: {e} Thử bài khác đưa cùng tin; nếu buộc phải dùng WebFetch thì chép kết quả vào "
                  f"`deep save {args.event_id} <url>` (nguồn sẽ bị ghi là bản tóm tắt).")
            return 2
        print(f"Đã lưu nguyên văn {words} từ: {path}  (đọc file này bằng Read)")
        return 0
    if args.cmd == "save":
        text = sys.stdin.read()
        if len(text.split()) < 80:
            print("CHƯA LƯU: nội dung quá ngắn.")
            return 2
        path = save_source(args.event_id, args.url, text, "tom_tat", base)
        print(f"Đã lưu bản WebFetch ({len(text.split())} từ, ghi là bản tóm tắt): {path}")
        return 0

    conn = db.connect(args.db)
    if args.cmd == "list":
        print(list_pending(conn, settings))
        return 0
    if args.cmd == "draft":
        return cmd_draft(conn, settings, args.event_id, sys.stdin.read(), base)
    if args.cmd == "verify":
        return cmd_verify(conn, settings, args.event_id, sys.stdin.read(), base)
    if args.cmd == "write":
        return cmd_write(conn, settings, args.event_id, base)
    from .preview import write_preview
    out = write_preview(conn, settings, ROOT / "out" / "preview.html",
                        byline="Claude chấm điểm, viết thẻ và bài phân tích trong phiên (không gọi API).")
    print(f"Đã xuất trang: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
