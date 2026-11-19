"""Chạy một lượt: thu thập → chấm điểm → gộp sự kiện → viết thẻ → xuất trang xem thử.

Ví dụ:
  python -m pipeline.main                       # chạy thật, ghi vào data/tin.db
  python -m pipeline.main --dry-run --limit 30  # chạy thử 30 bài, không đụng dữ liệu thật
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import db
from .classify import classify
from .cluster import cluster
from .collect import collect_all, make_client
from .config import ROOT, load_settings, load_sources
from .llm import LLM
from .models import CATEGORY_LABELS, GOAL_LABELS
from .summarize import summarize

DEFAULT_DB = ROOT / "data" / "tin.db"


def print_cards(conn, settings: dict, limit: int = 10) -> None:
    rows = db.events_for_preview(conn, since_hours=24, min_score=settings["scoring"]["show_min"])
    print(f"\n===== {len(rows)} sự kiện từ {settings['scoring']['show_min']} điểm trở lên (24 giờ qua) =====")
    for e in rows[:limit]:
        card = json.loads(e["card_json"]) if e["card_json"] else None
        cat = CATEGORY_LABELS.get(e["category"], e["category"])
        title = card["title_vi"] if card else e["title"]
        print(f"\n[{cat}] {e['score']}/10 · {e['source_count']} nguồn\n{title}")
        if card:
            for kp in card["key_points"]:
                print(f"  • {kp['text']}")
            print(f"  Vì sao quan trọng: {card['why_it_matters']}")
            print(f"  Nên làm gì: {card['what_to_do']}")
            goals = ", ".join(GOAL_LABELS.get(g, g) for g in card["affected_goals"])
            if goals:
                print(f"  Ảnh hưởng: {goals}")
            if card.get("disclaimer"):
                print(f"  ({card['disclaimer']})")
            print("  Nguồn: " + ", ".join(s["name"] for s in card["sources"]))


def run(args) -> int:
    settings, sources = load_settings(), load_sources()
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        tmp = Path(tempfile.mkdtemp()) / "dry-run.db"
        if db_path.exists():
            shutil.copy(db_path, tmp)
        db_path = tmp
        print(f"Chế độ chạy thử: dùng bản sao dữ liệu tạm ở {db_path}")

    conn = db.connect(db_path)
    run_id = db.start_run(conn)
    llm = LLM(settings, conn, run_id)
    stats: dict = {}
    errors: list[str] = []

    with make_client(settings) as http:
        if not args.skip_collect:
            items, report = collect_all(settings, sources)
            stats["collected"] = len(items)
            stats["new_articles"] = db.insert_articles(conn, items)
            errors += [f"{name}: {s}" for name, s in report.items() if s.startswith("LỖI")]
            print(f"Thu thập: {len(items)} bài, {stats['new_articles']} bài mới. "
                  f"{sum(1 for s in report.values() if s.startswith('LỖI'))}/{len(report)} nguồn lỗi.")

        rows = db.unscored_articles(conn, settings["collect"]["max_age_hours"], args.limit)
        stats["scored"], errs = classify(conn, llm, settings, rows)
        errors += errs
        print(f"Chấm điểm: {stats['scored']}/{len(rows)} bài.")

        stats["cluster"], errs = cluster(conn, llm, settings)
        errors += errs
        print(f"Gộp sự kiện: {stats['cluster']}")

        stats["cards"], errs = summarize(conn, llm, settings, http)
        errors += errs
        print(f"Viết thẻ: {stats['cards']} thẻ.")

    stats.update({"input_tokens": llm.input_tokens, "output_tokens": llm.output_tokens,
                  "cost_usd": round(llm.run_cost, 4)})
    db.finish_run(conn, run_id, stats, errors)

    print_cards(conn, settings)
    print(f"\nChi phí lượt này: {llm.run_cost:.4f} USD "
          f"({llm.input_tokens:,} token vào, {llm.output_tokens:,} token ra). "
          f"Từ đầu tháng: {db.month_cost(conn):.4f} USD.")
    if errors:
        print(f"\n{len(errors)} lỗi:")
        for e in errors:
            print(f"  - {e}")

    if args.preview:
        from .preview import write_preview
        out = write_preview(conn, settings, Path(args.preview))
        print(f"\nTrang xem thử: {out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Chạy một lượt trang tin AI")
    p.add_argument("--dry-run", action="store_true", help="Chạy trên bản sao dữ liệu, không ghi vào dữ liệu thật")
    p.add_argument("--limit", type=int, default=None, help="Chỉ chấm tối đa N bài mới nhất")
    p.add_argument("--db", default=str(DEFAULT_DB), help="Đường dẫn file SQLite")
    p.add_argument("--skip-collect", action="store_true", help="Không thu thập, chỉ xử lý bài đã có")
    p.add_argument("--preview", default=None, help="Xuất trang xem thử HTML ra đường dẫn này")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
