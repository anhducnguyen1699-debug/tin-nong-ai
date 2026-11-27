"""Nhập phiếu chấm từ trang "Bàn chấm tin" vào bộ nhớ và bộ đề chuẩn.

  python -m pipeline.ratings data/ratings_export.json

File đầu vào là danh sách phiếu (mỗi phiếu là object, hoặc {"id": ..., "data": {...}}).
- Ghi vào bảng `feedback` (để lượt sau AI học theo), nếu sự kiện còn trong file SQLite.
- Ghi đè `tests/golden.json` (bộ đề chuẩn để chạy `python -m pipeline.eval`).
"""
import argparse
import json
import re
import sys
from pathlib import Path

from . import db
from .config import ROOT
from .main import DEFAULT_DB

GOLDEN = ROOT / "tests" / "golden.json"


def load_export(path: Path) -> list[dict]:
    """Đọc phiếu từ một file JSON (danh sách phiếu) hoặc một thư mục chứa mỗi phiếu một file .json."""
    if path.is_dir():
        raw = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(path.glob("*.json"))]
    else:
        raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("documents") or raw.get("docs") or list(raw.values())
    out = []
    for item in raw:
        body = item.get("data") if isinstance(item, dict) and isinstance(item.get("data"), dict) else item
        if isinstance(body, dict) and body.get("key"):
            out.append(body)
    return out


def to_golden(r: dict) -> dict | None:
    if r.get("worth") not in ("dang_doc", "khong"):
        return None
    ev = r.get("event") or {}
    arts = ev.get("articles") or []
    return {
        "key": r["key"],
        "title": ev.get("title") or (arts[0]["title"] if arts else ""),
        "title_vi": ev.get("title_vi"),
        "source": arts[0]["source"] if arts else "",
        "summary": ev.get("summary", ""),
        "category": ev.get("category"),
        "hotness": ev.get("hotness", 0),
        "ai_score": ev.get("score"),
        "ai_importance": ev.get("importance"),
        "ai_impact": ev.get("impact"),
        "user_worth": r["worth"] == "dang_doc",
        "score_judgement": r.get("score_judgement"),
        "action_ok": r.get("action_ok"),
        "note": r.get("note", ""),
    }


def import_ratings(ratings: list[dict], conn=None) -> tuple[int, int]:
    """Trả về (số phiếu ghi vào feedback, số mục trong bộ đề chuẩn)."""
    saved = 0
    if conn is not None:
        for r in ratings:
            m = re.match(r"e(\d+)-", r["key"])
            if not m or r.get("worth") not in ("dang_doc", "khong"):
                continue
            event_id = int(m.group(1))
            ev = conn.execute("SELECT first_seen FROM events WHERE id=?", (event_id,)).fetchone()
            stamp = (ev["first_seen"] or "").replace("-", "").replace(":", "").split("+")[0] if ev else None
            if not ev or not r["key"].endswith(stamp):
                continue  # file SQLite đã làm lại, mã sự kiện không còn khớp
            db.save_feedback(
                conn, event_id, "huu_ich" if r["worth"] == "dang_doc" else "bo_qua",
                score_judgement=r.get("score_judgement"),
                action_ok=None if r.get("action_ok") is None else r["action_ok"] == "co",
                note=r.get("note") or None,
            )
            saved += 1

    golden = {g["key"]: g for g in json.loads(GOLDEN.read_text(encoding="utf-8"))} if GOLDEN.exists() else {}
    for r in ratings:
        g = to_golden(r)
        if g:
            golden[g["key"]] = g
    GOLDEN.write_text(json.dumps(list(golden.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    return saved, len(golden)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Nhập phiếu chấm từ trang Bàn chấm tin")
    p.add_argument("export", help="File JSON hoặc thư mục phiếu xuất từ kho phiếu chấm")
    p.add_argument("--db", default=str(DEFAULT_DB))
    args = p.parse_args(argv)
    ratings = load_export(Path(args.export))
    conn = db.connect(args.db) if Path(args.db).exists() else None
    saved, total = import_ratings(ratings, conn)
    print(f"Đọc {len(ratings)} phiếu. Ghi {saved} phiếu vào bộ nhớ sở thích. Bộ đề chuẩn có {total} tin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
