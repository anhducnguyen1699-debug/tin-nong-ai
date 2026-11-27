"""Công cụ cho Claude chạy bản tin mà không gọi API (Claude trong phiên tự làm phần "AI").

  python -m pipeline.hand reset             # xóa đề/câu trả lời cũ trước một lượt mới
  python -m pipeline.hand next [--sample N] [--collect]
                                            # chạy tiếp đường ống; dừng ở đề đang chờ và in đề ra gọn
  python -m pipeline.hand answer < file     # ghi câu trả lời cho đề đang chờ (định dạng gọn, xem bên dưới)

Định dạng câu trả lời gọn (mỗi bước):
- classify: mỗi bài một dòng  id|c|quan_trong|dau_tu|nghe_y|dia_chinh_tri|viet_nam|lý do
            c là a (AI & công nghệ), t (thời sự & kinh tế) hoặc b (bỏ qua)
- cluster:  các cụm cách nhau bởi khoảng trắng hoặc xuống dòng, dạng  nhan:1,2,3  (sự kiện mới)
            hoặc  e<mã>:4,5  (gắn vào sự kiện có sẵn). Bài không nhắc tới thành sự kiện riêng.
- summarize: đúng 11 dòng: tiêu đề | ý 1 | nguồn ý 1 | ý 2 | nguồn ý 2 | ý 3 | nguồn ý 3 |
            vì sao quan trọng | nên làm gì | mục tiêu (dau_tu,nghe_y,dia_chinh_tri cách nhau bằng dấu phẩy) |
            có nói tới đầu tư (1/0)
"""
import argparse
import html
import json
import re
import shutil
import subprocess
import sys

from .config import ROOT
from .models import ClassifyResult, ClusterResult, EventCard

HANDOFF = ROOT / "data" / "handoff"
CAT = {"a": "ai_cong_nghe", "t": "thoi_su_kinh_te", "b": "bo_qua"}


def current() -> tuple[str, dict] | None:
    pointer = HANDOFF / "CURRENT"
    if not pointer.exists():
        return None
    name = pointer.read_text(encoding="utf-8").strip()
    if (HANDOFF / f"{name}.answer.json").exists() or not (HANDOFF / f"{name}.json").exists():
        return None
    return name, json.loads((HANDOFF / f"{name}.json").read_text(encoding="utf-8"))


def show(task: dict) -> str:
    step, user = task["step"], html.unescape(task["user"])
    out = [f"=== ĐỀ: {step} ==="]
    if step == "classify":
        out.append("Trả lời mỗi bài một dòng: id|c|quan_trong|dau_tu|nghe_y|dia_chinh_tri|viet_nam|lý do (c = a/t/b)")
        pref = re.search(r"<so_thich>(.*?)</so_thich>", user, re.S)
        if pref:
            out.append("Sở thích người đọc:" + pref.group(1).rstrip())
        for m in re.finditer(r'<bai id="(\d+)">\nnguon: (.*)\ntieu_de: (.*)\nmo_ta: (.*)\n</bai>', user):
            out.append(f"{m[1]}|{m[2][:22]}|{m[3][:140]}|{m[4][:120]}")
    elif step == "cluster":
        out.append("Trả lời: nhan:1,2,3 (sự kiện mới) hoặc e<mã>:4,5 (sự kiện có sẵn); bài không nhắc tới thành sự kiện riêng")
        out.append(user)
    elif step == "summarize":
        out.append("Trả lời đúng 11 dòng: tiêu đề | ý 1 | nguồn 1 | ý 2 | nguồn 2 | ý 3 | nguồn 3 | vì sao quan trọng | "
                   "nên làm gì | mục tiêu (dau_tu,nghe_y,dia_chinh_tri) | đầu tư 1/0")
        for m in re.finditer(r'<nguon id="(\d+)" ten="(.*?)">\ntieu_de: (.*?)\nnoi_dung: (.*?)\n</nguon>', user, re.S):
            out.append(f"[{m[1]}] {m[2]} | {m[3]}\n    {m[4][:1500].replace(chr(10), ' ')}")
    return "\n".join(out)


def parse_answer(step: str, text: str, user: str) -> str:
    """Đổi câu trả lời gọn thành JSON đúng khuôn. Sai khuôn thì báo lỗi rõ ràng."""
    text = text.strip()
    if step == "classify":
        items = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 7)
            if len(parts) != 8:
                raise ValueError(f"Dòng cần 8 phần ngăn bởi '|': {line}")
            i, c, imp, d, n, g, v, r = (p.strip() for p in parts)
            items.append({"id": int(i), "category": CAT[c], "importance": int(imp), "impact_dau_tu": int(d),
                          "impact_nghe_y": int(n), "impact_dia_chinh_tri": int(g), "impact_viet_nam": int(v),
                          "reason": r})
        expected = len(re.findall(r'<bai id="', user))
        if len(items) != expected:
            raise ValueError(f"Đề có {expected} bài nhưng câu trả lời có {len(items)} dòng")
        return ClassifyResult(items=items).model_dump_json()
    if step == "cluster":
        n = len(re.findall(r"^\d+\. \[", user.split("<bai_moi>")[-1], re.M))
        mapping: dict[int, str] = {}
        for token in text.split():
            label, ids = token.split(":")
            for i in ids.split(","):
                mapping[int(i)] = label
        decisions = []
        for i in range(1, n + 1):
            label = mapping.get(i, f"rieng-{i}")
            existing = re.fullmatch(r"e(\d+)", label)
            decisions.append({"article_id": i, "event_id": int(existing[1]) if existing else 0,
                              "new_group": "" if existing else label})
        return ClusterResult(decisions=decisions).model_dump_json()
    if step == "summarize":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) != 11:
            raise ValueError(f"Thẻ cần đúng 11 dòng, đang có {len(lines)}")
        t, k1, s1, k2, s2, k3, s3, why, todo, goals, inv = lines
        card = {"title_vi": t, "key_points": [{"text": k1, "source_id": int(s1)}, {"text": k2, "source_id": int(s2)},
                                              {"text": k3, "source_id": int(s3)}],
                "why_it_matters": why, "what_to_do": todo,
                "affected_goals": [g.strip() for g in goals.split(",") if g.strip()],
                "is_investment_related": inv == "1"}
        return EventCard(**card).model_dump_json()
    raise ValueError(f"Không biết bước {step}")


def run_pipeline(extra: list[str]) -> str:
    cmd = [sys.executable, "-m", "pipeline.main", "--hand", "--preview", str(ROOT / "out" / "preview.html"), *extra]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    lines = [l for l in (res.stdout + res.stderr).splitlines() if "warn" not in l.lower()]
    return "\n".join(lines[-40:])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Chạy bản tin bằng Claude trong phiên (không gọi API)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset")
    nx = sub.add_parser("next")
    nx.add_argument("--collect", action="store_true", help="Thu thập tin mới trước (chỉ dùng ở lần gọi đầu)")
    nx.add_argument("--sample", type=int, default=None)
    nx.add_argument("--db", default=None, help="File SQLite (mặc định data/tin.db)")
    sub.add_parser("show")
    sub.add_parser("answer")
    args = p.parse_args(argv)

    if args.cmd == "reset":
        shutil.rmtree(HANDOFF, ignore_errors=True)
        HANDOFF.mkdir(parents=True, exist_ok=True)
        print("Đã xóa đề cũ.")
        return 0
    if args.cmd == "answer":
        cur = current()
        if not cur:
            print("Không có đề nào đang chờ.")
            return 1
        name, task = cur
        out = parse_answer(task["step"], sys.stdin.read(), html.unescape(task["user"]))
        (HANDOFF / f"{name}.answer.json").write_text(out, encoding="utf-8")
        print(f"Đã ghi câu trả lời cho {name}.")
        return 0
    if args.cmd == "next":
        extra = [] if args.collect else ["--skip-collect"]
        if args.sample:
            extra += ["--sample", str(args.sample)]
        if args.db:
            extra += ["--db", args.db]
        log = run_pipeline(extra)
    else:
        log = ""
    cur = current()
    if cur:
        print(show(cur[1]))
        return 3
    print(log)
    print("\nXONG: không còn đề nào chờ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
