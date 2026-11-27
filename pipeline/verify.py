"""Kiểm chứng bài phân tích: lưu nguyên văn nguồn, tách câu cần kiểm, đối chiếu bằng chứng.

Quy trình (xem docs/chay-ban-tin.md, bước 3b):
  fetch/save nguồn → draft (tách câu) → tác tử đối chiếu → verify (máy soát đoạn trích) → sửa → write.

Nguyên văn nguồn lưu ở data/nguon/<mã sự kiện>/ và KHÔNG commit (bài báo có bản quyền, chỉ dùng tạm).
Trong bài chỉ giữ đoạn trích bằng chứng ngắn (≤ 30 từ) cho từng câu.
"""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from .config import ROOT

SOURCES_DIR = ROOT / "data" / "nguon"
CITE = re.compile(r"\[(\d+)\]")
SENT_END = re.compile(r"(?<=[.!?…])\s+")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
VERDICTS = {
    "khop": "khop", "khớp": "khop",
    "lech": "lech", "lệch": "lech",
    "khong_thay": "khong_thay", "không_thấy": "khong_thay", "khong thay": "khong_thay", "không thấy": "khong_thay",
    "nhan_dinh": "nhan_dinh", "nhận_định": "nhan_dinh", "nhan dinh": "nhan_dinh", "nhận định": "nhan_dinh",
}
OK = {"khop", "nhan_dinh"}  # hai kết luận được phép đăng
LABEL = {"khop": "khớp", "lech": "lệch", "khong_thay": "không thấy", "sai_nguon": "sai nguồn",
         "nhan_dinh": "nhận định", "chua_kiem": "chưa kiểm"}


class VerifyError(ValueError):
    pass


# ---------- Lưu nguyên văn nguồn ----------
def url_key(url: str) -> str:
    return hashlib.sha1(url.strip().rstrip("/").encode()).hexdigest()[:12]


def source_path(event_id: int, url: str, base: Path = SOURCES_DIR) -> Path:
    return base / str(event_id) / f"{url_key(url)}.txt"


def save_source(event_id: int, url: str, text: str, how: str, base: Path = SOURCES_DIR) -> Path:
    """how: 'nguyen_van' (tải và bóc chữ bằng mã) hoặc 'tom_tat' (chép từ WebFetch, có thể đã bị tóm lược)."""
    path = source_path(event_id, url, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(f"# url: {url}\n# cach_doc: {how}\n# luc: {stamp}\n\n{text.strip()}\n", encoding="utf-8")
    return path


def read_source(event_id: int, url: str, base: Path = SOURCES_DIR) -> tuple[str, str] | None:
    path = source_path(event_id, url, base)
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    m = re.search(r"^# cach_doc: (\w+)", raw, re.M)
    body = raw.split("\n\n", 1)[1] if "\n\n" in raw else raw
    return body, (m.group(1) if m else "nguyen_van")


def fetch_source(event_id: int, url: str, settings: dict, base: Path = SOURCES_DIR) -> tuple[Path, int]:
    """Tải trang và bóc chữ bằng trafilatura (giữ cả bảng số liệu). Lỗi hoặc quá ngắn thì báo để dùng WebFetch."""
    import trafilatura

    from .collect import make_client

    try:
        with make_client(settings) as client:
            resp = client.get(url)
        resp.raise_for_status()
    except Exception as e:  # bị chặn, tường phí, lỗi mạng
        raise VerifyError(f"Không tải được ({type(e).__name__}: {str(e)[:120]}).") from None
    text = trafilatura.extract(resp.text, include_comments=False, include_tables=True, favor_recall=True) or ""
    words = len(text.split())
    if words < 150:
        raise VerifyError(f"Chỉ bóc được {words} từ (có thể bị tường phí hoặc trang dựng bằng JavaScript).")
    return save_source(event_id, url, text, "nguyen_van", base), words


# ---------- Tách câu cần kiểm ----------
def split_sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip().removeprefix("- ").strip()
        if not line:
            continue
        parts = SENT_END.split(line)
        merged: list[str] = []
        for p in parts:
            # câu sau bắt đầu bằng chữ thường thì là chỗ cắt nhầm (viết tắt), nối lại
            if merged and p[:1].islower():
                merged[-1] += " " + p
            else:
                merged.append(p)
        out += [m.strip() for m in merged if m.strip()]
    return out


def is_claim(sentence: str) -> bool:
    """Câu cần có bằng chứng: có dẫn nguồn, có con số, hoặc có trích dẫn."""
    return bool(CITE.search(sentence) or re.search(r"\d", CITE.sub("", sentence)) or re.search(r"[“\"]", sentence))


def extract_claims(article: dict) -> list[dict]:
    claims = []
    blocks = [("Sapo", article["sapo"])] + [(s["heading"], s["body"]) for s in article["sections"]]
    for heading, body in blocks:
        for sent in split_sentences(body):
            if is_claim(sent):
                claims.append({"n": len(claims) + 1, "section": heading, "text": sent,
                               "cites": sorted({int(c) for c in CITE.findall(sent)})})
    return claims


def claim_key(claim: dict) -> str:
    return _norm(claim["text"]) + "|" + ",".join(map(str, claim["cites"]))


# ---------- Đối chiếu bằng chứng ----------
def _norm(text: str) -> str:
    t = unicodedata.normalize("NFC", text).lower()
    t = re.sub(r"[“”„\"«»]", '"', t)
    t = re.sub(r"[‘’`´]", "'", t)
    t = re.sub(r"[‐-―−]", "-", t)
    t = t.replace("**", "").replace("…", "...")
    return re.sub(r"\s+", " ", t).strip()


def evidence_in_source(evidence: str, source_text: str, min_ratio: int) -> bool:
    ev = _norm(evidence).strip(" \"'.…")
    ev = re.sub(r"^\.\.\.|\.\.\.$", "", ev).strip()
    if len(ev.split()) < 4:
        return False
    src = _norm(source_text)
    if ev in src:
        return True
    # So gần đúng (chịu được khác dấu câu, xuống dòng), nhưng con số phải trùng tuyệt đối:
    # "4.1 percent" và "3.8 percent" giống nhau 94% về chữ mà khác hẳn về nghĩa.
    hit = fuzz.partial_ratio_alignment(ev, src, score_cutoff=min_ratio)
    if hit is None or hit.score < min_ratio:
        return False
    region = src[max(0, hit.dest_start - 5):hit.dest_end + 5]
    return all(re.search(rf"(?<![\d.,]){re.escape(n)}(?![\d])", region) for n in NUMBER.findall(ev))


def parse_answer(text: str) -> dict[int, list[dict]]:
    """Mỗi dòng: `số câu | khop/lech/khong_thay/nhan_dinh | số nguồn hoặc - | "đoạn trích" hoặc -`.
    Một câu có thể có nhiều dòng (mỗi dòng một bằng chứng)."""
    out: dict[int, list[dict]] = {}
    for raw in text.strip().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 3)]
        if len(parts) < 2:
            raise VerifyError(f"Dòng sai khuôn (cần `số | kết luận | nguồn | trích`): {line}")
        try:
            n = int(parts[0].strip("#. "))
        except ValueError:
            raise VerifyError(f"Số câu không hợp lệ: {line}") from None
        verdict = VERDICTS.get(parts[1].lower().replace("-", "_"))
        if not verdict:
            raise VerifyError(f"Kết luận phải là khop, lech, khong_thay hoặc nhan_dinh: {line}")
        src = parts[2] if len(parts) > 2 else "-"
        try:
            source = int(src.strip("[] ")) if src.strip("[] -") else None
        except ValueError:
            raise VerifyError(f"Số nguồn không hợp lệ: {line}") from None
        evidence = parts[3].strip() if len(parts) > 3 else ""
        evidence = "" if evidence in ("-", "") else evidence.strip("“”\" ")
        out.setdefault(n, []).append({"verdict": verdict, "source": source, "evidence": evidence})
    if not out:
        raise VerifyError("Câu trả lời trống.")
    return out


def judge(claim: dict, lines: list[dict], sources: dict[int, dict], event_id: int, settings: dict,
          base: Path = SOURCES_DIR) -> dict:
    """Máy soát lại kết luận của tác tử: đoạn trích phải có thật trong file nguồn và đúng nguồn câu đã dẫn."""
    cfg = settings["deep"]
    max_words = cfg.get("evidence_max_words", 30)
    min_ratio = cfg.get("evidence_min_ratio", 90)
    verdicts = {l["verdict"] for l in lines}
    result = {"verdict": "khop", "evidence": [], "note": "", "weak": False}

    if verdicts == {"nhan_dinh"}:
        if claim["cites"]:
            return {**result, "verdict": "chua_kiem", "note": "Câu có dẫn nguồn [n] thì phải đối chiếu, không ghi là nhận định."}
        return {**result, "verdict": "nhan_dinh"}
    for bad in ("lech", "khong_thay"):
        if bad in verdicts:
            ev, unconfirmed = [], []
            for l in lines:
                if l["verdict"] != bad or not l["evidence"]:
                    continue
                quote = " ".join(l["evidence"].split()[:max_words])
                stored = read_source(event_id, sources[l["source"]]["url"], base) if l["source"] in sources else None
                if stored and evidence_in_source(quote, stored[0], min_ratio):
                    ev.append({"source": l["source"], "text": quote})
                else:
                    unconfirmed.append(str(l["source"]))
            note = (f"Đoạn trích tác tử đưa ra không có trong nguồn {', '.join(unconfirmed)}: tự mở file nguồn để sửa cho đúng."
                    if unconfirmed else "")
            return {**result, "verdict": bad, "evidence": ev, "note": note}

    used = set()
    for l in lines:
        sid = l["source"]
        if sid not in sources:
            return {**result, "verdict": "khong_thay", "note": f"Nguồn {sid} không có trong danh sách nguồn của bài."}
        stored = read_source(event_id, sources[sid]["url"], base)
        if stored is None:
            return {**result, "verdict": "khong_thay", "note": f"Chưa lưu nguyên văn nguồn {sid}, không kiểm được."}
        text, how = stored
        quote = " ".join(l["evidence"].split()[:max_words])
        if not evidence_in_source(quote, text, min_ratio):
            return {**result, "verdict": "khong_thay",
                    "note": f"Đoạn trích không có trong file nguồn {sid} (có thể tác tử chép sai hoặc tự bịa)."}
        if how != "nguyen_van":
            result["weak"] = True
        used.add(sid)
        result["evidence"].append({"source": sid, "text": quote})
    if claim["cites"] and not used & set(claim["cites"]):
        return {**result, "verdict": "sai_nguon",
                "note": f"Câu dẫn {claim['cites']} nhưng bằng chứng nằm ở nguồn {sorted(used)}: sửa số nguồn trong câu."}
    return result


# ---------- Trạng thái từng sự kiện (bản nháp, kết quả đối chiếu qua các vòng) ----------
def state_path(event_id: int, base: Path = SOURCES_DIR) -> Path:
    return base / str(event_id) / "kiem_chung.json"


def fresh_state(history: dict | None = None) -> dict:
    return {"rounds": 0, "first": None, "draft": None, "claims": [], "results": {}, "history": history or {}}


def load_state(event_id: int, base: Path = SOURCES_DIR) -> dict:
    p = state_path(event_id, base)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return fresh_state()


def save_state(event_id: int, state: dict, base: Path = SOURCES_DIR) -> None:
    p = state_path(event_id, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def new_draft(state: dict, draft_text: str, claims: list[dict]) -> dict:
    """Bản nháp mới: câu nào giữ nguyên chữ và nguồn so với vòng trước, đã khớp, thì giữ kết quả; còn lại kiểm lại."""
    history = state.get("history", {})
    results = {}
    for c in claims:
        old = history.get(claim_key(c))
        if old and old["verdict"] in OK:
            results[str(c["n"])] = old
    return {**state, "rounds": state["rounds"] + 1, "draft": draft_text, "claims": claims, "results": results}


def apply_answer(state: dict, answers: dict[int, list[dict]], sources: dict[int, dict], event_id: int,
                 settings: dict, base: Path = SOURCES_DIR) -> dict:
    by_n = {c["n"]: c for c in state["claims"]}
    unknown = sorted(set(answers) - set(by_n))
    if unknown:
        raise VerifyError(f"Không có câu số {unknown} trong bản nháp hiện tại.")
    results = dict(state["results"])
    history = dict(state.get("history", {}))
    for n, lines in answers.items():
        r = judge(by_n[n], lines, sources, event_id, settings, base)
        results[str(n)] = r
        history[claim_key(by_n[n])] = r
    state = {**state, "results": results, "history": history}
    if state["first"] is None and all(str(c["n"]) in results for c in state["claims"]):
        state["first"] = summarize(state)  # tỷ lệ của lần viết đầu: đo mức AI viết sai trước khi được sửa
    return state


def summarize(state: dict) -> dict:
    res = [state["results"].get(str(c["n"]), {"verdict": "chua_kiem"}) for c in state["claims"]]
    factual = [r for r in res if r["verdict"] != "nhan_dinh"]
    return {
        "claims": len(res),
        "checked": len(factual),
        "ok": sum(1 for r in factual if r["verdict"] == "khop"),
        "opinion": len(res) - len(factual),
        "pending": sum(1 for r in res if r["verdict"] == "chua_kiem"),
        "weak": sum(1 for r in res if r.get("weak")),
    }


def failing(state: dict) -> list[tuple[dict, dict]]:
    return [(c, state["results"].get(str(c["n"]), {"verdict": "chua_kiem", "note": "Chưa có kết luận."}))
            for c in state["claims"] if state["results"].get(str(c["n"]), {}).get("verdict") not in OK]


def verification_block(state: dict) -> dict:
    """Phần `verification` lưu kèm bài để trang hiện dòng kiểm chứng và đoạn trích bằng chứng."""
    s = summarize(state)
    first = state["first"] or s
    return {
        **s,
        "first_checked": first["checked"], "first_ok": first["ok"],
        "rounds": state["rounds"],
        "items": [{"n": c["n"], "section": c["section"], "text": c["text"], "cites": c["cites"],
                   "verdict": state["results"][str(c["n"])]["verdict"],
                   "weak": state["results"][str(c["n"])].get("weak", False),
                   "evidence": state["results"][str(c["n"])].get("evidence", [])}
                  for c in state["claims"]],
    }


def verify_task(event_id: int, state: dict, sources: list[dict], base: Path = SOURCES_DIR) -> str:
    """Đề cho tác tử đối chiếu: chỉ các câu chưa có kết luận, kèm đường dẫn file nguyên văn của từng nguồn."""
    pending = [c for c in state["claims"] if str(c["n"]) not in state["results"]]
    lines = [f"# Đề đối chiếu — sự kiện {event_id}, vòng {state['rounds']}",
             "Luật: pipeline/prompts/verify.md. Chỉ dùng các file nguồn dưới đây, không dùng hiểu biết riêng hay web.", "",
             "## Nguồn"]
    for s in sources:
        stored = source_path(event_id, s["url"], base)
        how = read_source(event_id, s["url"], base)
        state_txt = "CHƯA LƯU, không kiểm được" if how is None else ("nguyên văn" if how[1] == "nguyen_van" else "bản tóm tắt WebFetch")
        lines.append(f"{s['id']} | {s['name']} | {stored} | {state_txt}")
    lines += ["", f"## Câu cần kiểm ({len(pending)} câu)"]
    for c in pending:
        cites = ",".join(map(str, c["cites"])) or "không dẫn nguồn"
        lines.append(f"{c['n']} | nguồn: {cites} | {c['text']}")
    lines += ["", "## Khuôn trả lời (mỗi dòng một bằng chứng, chỉ in các dòng này)",
              '<số câu> | khop | <số nguồn> | "đoạn chép nguyên văn từ file nguồn, tối đa 30 từ"',
              '<số câu> | lech | <số nguồn> | "đoạn nguồn nói khác"',
              "<số câu> | khong_thay | - | -",
              "<số câu> | nhan_dinh | - | -"]
    return "\n".join(lines)
