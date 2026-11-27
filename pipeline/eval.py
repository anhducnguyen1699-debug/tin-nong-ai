"""So điểm AI với phiếu chấm của bạn trong tests/golden.json.

  python -m pipeline.eval --offline   # dùng điểm AI đã chấm lúc chạy (miễn phí)
  python -m pipeline.eval             # chấm lại bằng câu lệnh hiện tại (tốn vài xu) — dùng sau mỗi lần sửa câu lệnh
  python -m pipeline.eval --articles  # độ chính xác của bài phân tích (máy đối chiếu + bạn đối chiếu tay)

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
from .scoring import effective_hotness, final_score

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


def article_accuracy(conn) -> dict:
    """(a) câu khớp ngay lần viết đầu, (b) câu khớp khi đăng, (c) câu đúng theo bạn đối chiếu tay."""
    a_ok = a_n = b_ok = b_n = arts = weak = 0
    for row in conn.execute("SELECT article_json FROM events WHERE article_json IS NOT NULL"):
        v = json.loads(row["article_json"]).get("verification")
        if not v:
            continue
        arts += 1
        a_ok += v["first_ok"]; a_n += v["first_checked"]
        b_ok += v["ok"]; b_n += v["checked"]
        weak += v.get("weak", 0)
    checks = conn.execute("SELECT COUNT(*) n, SUM(wrong_count) w, SUM(claims_total) t, "
                          "SUM(verdict='loi') bad FROM article_checks").fetchone()
    c_n = checks["t"] or 0
    return {
        "articles": arts, "first_ok": a_ok, "first_n": a_n, "final_ok": b_ok, "final_n": b_n, "weak": weak,
        "checked_articles": checks["n"] or 0, "articles_with_errors": checks["bad"] or 0,
        "human_wrong": checks["w"] or 0, "human_claims": c_n,
    }


def print_article_accuracy(r: dict) -> None:
    pct = lambda a, b: f"{a / b:.1%}" if b else "chưa có"
    print(f"Bài phân tích đã kiểm chứng: {r['articles']}")
    print(f"(a) Câu khớp ngay lần viết đầu: {r['first_ok']}/{r['first_n']} = {pct(r['first_ok'], r['first_n'])}"
          "  ← mức AI viết sai trước khi được sửa")
    print(f"(b) Câu khớp khi đăng:          {r['final_ok']}/{r['final_n']} = {pct(r['final_ok'], r['final_n'])}"
          + (f"  ({r['weak']} câu chỉ dựa vào bản tóm tắt WebFetch)" if r["weak"] else ""))
    right = r["human_claims"] - r["human_wrong"]
    print(f"(c) Bạn đối chiếu tay {r['checked_articles']} bài ({r['articles_with_errors']} bài có lỗi): "
          f"{right}/{r['human_claims']} câu đúng = {pct(right, r['human_claims'])}  ← sai sót lọt qua cả máy")
    if r["checked_articles"] < 10:
        print("   Cần khoảng 10 bài đối chiếu tay thì con số (c) mới đáng tin.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Đo độ khớp giữa AI và bạn")
    p.add_argument("--offline", action="store_true", help="Không gọi AI, dùng điểm đã lưu")
    p.add_argument("--articles", action="store_true", help="Đo độ chính xác của bài phân tích")
    p.add_argument("--db", default=None, help="File dữ liệu (mặc định data/tin.db)")
    p.add_argument("--threshold", type=float, default=None, help="Ngưỡng 'đáng đọc' (mặc định = summarize_min)")
    args = p.parse_args(argv)

    settings = load_settings()
    if args.articles:
        from .main import DEFAULT_DB
        print_article_accuracy(article_accuracy(db.connect(args.db or DEFAULT_DB)))
        return 0
    threshold = args.threshold or settings["scoring"]["summarize_min"]
    if not GOLDEN.exists():
        print("Chưa có tests/golden.json. Chấm tin ở trang Bàn chấm tin rồi chạy pipeline.ratings trước.")
        return 1
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    if args.offline:
        # Giữ nguyên điểm AI đã chấm (quan trọng, ảnh hưởng), nhưng tính lại điểm cuối theo công thức hiện tại
        weights = settings["scoring"]["weights"]
        scores = []
        for g in golden:
            if g.get("ai_importance") is None or g.get("ai_impact") is None:
                scores.append(g.get("ai_score"))
                continue
            hot = effective_hotness(g.get("hotness", 0) or 0, g["ai_impact"])
            scores.append(final_score(g["ai_importance"], g["ai_impact"], hot, weights))
        label = "điểm AI lúc chạy, công thức hiện tại"
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
            hot = effective_hotness(g.get("hotness", 0) or 0, impact)
            scores.append(final_score(s["importance"], impact, hot, weights))
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
