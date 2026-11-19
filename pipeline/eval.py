"""So điểm AI với phiếu chấm của bạn trong tests/golden.json.

  python -m pipeline.eval --offline   # dùng điểm AI đã chấm lúc chạy (miễn phí)
  python -m pipeline.eval             # chấm lại bằng câu lệnh hiện tại (tốn vài xu) — dùng sau mỗi lần sửa câu lệnh

Tiêu chí đạt: khớp "đáng đọc hay không" từ 75% trở lên, và từ 2/3 số thẻ có "Nên làm gì" dùng được.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import db
from .classify import classify_rows_for_eval
from .config import load_settings
from .llm import LLM
from .ratings import GOLDEN
from .scoring import final_score

PASS_AGREEMENT = 0.75
PASS_ACTION = 2 / 3


def agreement(golden: list[dict], predicted_scores: list[float], threshold: float) -> dict:
    pairs = [(g["user_worth"], s >= threshold) for g, s in zip(golden, predicted_scores) if s is not None]
    n = len(pairs)
    agree = sum(1 for u, p in pairs if u == p)
    return {
        "n": n,
        "agreement": agree / n if n else 0.0,
        "ai_too_high": sum(1 for u, p in pairs if p and not u),   # AI cho đáng đọc, bạn thì không
        "ai_too_low": sum(1 for u, p in pairs if u and not p),    # bạn thấy đáng đọc, AI chấm thấp
    }


def action_rate(golden: list[dict]) -> tuple[int, float]:
    rated = [g for g in golden if g.get("action_ok") in ("co", "khong")]
    ok = sum(1 for g in rated if g["action_ok"] == "co")
    return len(rated), (ok / len(rated) if rated else 0.0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Đo độ khớp giữa AI và bạn")
    p.add_argument("--offline", action="store_true", help="Không gọi AI, dùng điểm đã lưu")
    p.add_argument("--threshold", type=float, default=None, help="Ngưỡng 'đáng đọc' (mặc định = summarize_min)")
    args = p.parse_args(argv)

    settings = load_settings()
    threshold = args.threshold or settings["scoring"]["summarize_min"]
    if not GOLDEN.exists():
        print("Chưa có tests/golden.json. Chấm tin ở trang Bàn chấm tin rồi chạy pipeline.ratings trước.")
        return 1
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    if args.offline:
        scores = [g.get("ai_score") for g in golden]
        label = "điểm lúc chạy"
    else:
        conn = db.connect(Path(tempfile.mkdtemp()) / "eval.db")
        llm = LLM(settings, conn)
        result = classify_rows_for_eval(llm, settings, golden)
        weights = settings["scoring"]["weights"]
        scores = []
        for g, s in zip(golden, result):
            if s is None:
                scores.append(None)
                continue
            impact = max(s["impact_dau_tu"], s["impact_nghe_y"], s["impact_dia_chinh_tri"], s["impact_viet_nam"])
            scores.append(final_score(s["importance"], impact, g.get("hotness", 0), weights))
        label = f"chấm lại bằng câu lệnh hiện tại, chi phí {llm.run_cost:.4f} USD"

    a = agreement(golden, scores, threshold)
    n_action, rate = action_rate(golden)
    print(f"Bộ đề: {len(golden)} tin ({label}). Ngưỡng đáng đọc: {threshold} điểm.")
    print(f"Khớp 'đáng đọc hay không': {a['agreement']:.0%} trên {a['n']} tin "
          f"(AI chấm cao quá: {a['ai_too_high']}, thấp quá: {a['ai_too_low']}). Cần ≥ {PASS_AGREEMENT:.0%}.")
    if n_action:
        print(f"'Nên làm gì' dùng được: {rate:.0%} trên {n_action} thẻ. Cần ≥ {PASS_ACTION:.0%}.")
    passed = a["agreement"] >= PASS_AGREEMENT and (n_action == 0 or rate >= PASS_ACTION)
    print("ĐẠT" if passed else "CHƯA ĐẠT")
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
