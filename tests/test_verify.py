import json

import pytest

from pipeline import db, deep
from pipeline.config import load_settings
from pipeline.eval import article_accuracy
from pipeline.models import Article
from pipeline.ratings import import_article_checks
from pipeline.verify import (VerifyError, evidence_in_source, extract_claims, load_state, parse_answer,
                             save_source, split_sentences)
from tests.test_deep import make_article

SETTINGS = load_settings()
SRC1 = ("The Federal Reserve held its benchmark rate steady at a range of 3.75% to 4% on Wednesday, "
        "and Chair Powell said further cuts were not a foregone conclusion. ") * 3
SRC2 = "Core PCE inflation rose 3.8 percent over the past year, according to the Bureau of Economic Analysis. " * 3
SRC3 = "The statement said the committee will meet again on December 9 and 10 to assess incoming data. " * 3
SOURCES = {1: "https://reuters.com/a", 2: "https://apnews.com/b", 3: "https://federalreserve.gov/c"}


def setup_event(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.insert_articles(conn, [("k", Article(url="https://reuters.com/a", title="Fed holds", source="Reuters"))])
    eid = db.create_event(conn, "Fed holds", "thoi_su_kinh_te")
    db.attach(conn, 1, eid)
    conn.execute("UPDATE events SET score=8.0, source_count=1 WHERE id=?", (eid,))
    conn.commit()
    base = tmp_path / "nguon"
    for sid, text in ((1, SRC1), (2, SRC2), (3, SRC3)):
        save_source(eid, SOURCES[sid], text, "nguyen_van", base)
    return conn, eid, base


def draft_text():
    extra = ("Fed giữ lãi suất ở mức 3,75–4% [1]. Lạm phát PCE lõi tăng 3,8% [2]. "
             "Fed họp tiếp ngày 9–10/12 [3]. Nhiều khả năng thị trường sẽ chờ thêm.")
    return make_article(160).replace("## Bối cảnh", extra + "\n\n## Bối cảnh", 1)


def run(cmd, eid, base, conn_path, stdin, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = deep.main(["--db", str(conn_path), "--nguon", str(base), cmd, str(eid)])
    return code, capsys.readouterr().out


def test_split_sentences_keeps_abbreviation_and_lists():
    text = "Fed giữ lãi suất [1]. Ông Powell nói thêm. tiếp nối\n- Mục một có số 5%.\n- Mục hai."
    assert split_sentences(text) == ["Fed giữ lãi suất [1].", "Ông Powell nói thêm. tiếp nối",
                                     "Mục một có số 5%.", "Mục hai."]


def test_extract_claims_only_sentences_with_facts():
    a = {"sapo": "Fed giữ lãi suất [1]. Đây là tin lớn.", "sections": [
        {"heading": "Bối cảnh", "body": "Lạm phát 3,8%. Câu bình thường. Ông nói “chưa vội”."}]}
    claims = extract_claims(a)
    assert [c["text"] for c in claims] == ["Fed giữ lãi suất [1].", "Lạm phát 3,8%.", "Ông nói “chưa vội”."]
    assert claims[0]["cites"] == [1] and claims[1]["cites"] == []


def test_evidence_must_exist_in_source():
    assert evidence_in_source("held its benchmark rate steady at a range of 3.75%", SRC1, 90)
    assert evidence_in_source("Held its benchmark  rate steady at a range of 3.75%…", SRC1, 90)  # khác hoa thường, dấu
    assert not evidence_in_source("held its benchmark rate steady at a range of 5.25%", SRC1 + "x" * 5, 97)
    assert not evidence_in_source("rate steady", SRC1, 90)  # quá ngắn thì không tính
    # giống 94% về chữ nhưng sai con số: phải bị loại
    assert not evidence_in_source("Core PCE inflation rose 4.1 percent over the past year", SRC2, 90)
    assert evidence_in_source("Core PCE inflation rose 3.8 percent, over the past year", SRC2, 90)
    assert not evidence_in_source("the central bank raised rates sharply amid fears of recession", SRC1, 90)


def test_parse_answer_formats_and_errors():
    out = parse_answer('1 | khớp | 2 | "abc def ghi jkl"\n1 | khop | 3 | "x y z w"\n2 | không thấy | - | -')
    assert len(out[1]) == 2 and out[2][0]["verdict"] == "khong_thay"
    with pytest.raises(VerifyError):
        parse_answer("1 | dung | 1 | x")


def test_full_flow_fix_round_then_write(tmp_path, monkeypatch, capsys):
    conn, eid, base = setup_event(tmp_path)
    path = tmp_path / "t.db"
    code, out = run("draft", eid, base, path, draft_text(), monkeypatch, capsys)
    assert code == 0 and "cần đối chiếu" in out
    claims = load_state(eid, base)["claims"]
    by_text = {c["text"]: c["n"] for c in claims}
    n_rate, n_pce, n_meet, = by_text["Fed giữ lãi suất ở mức 3,75–4% [1]."], by_text["Lạm phát PCE lõi tăng 3,8% [2]."], \
        by_text["Fed họp tiếp ngày 9–10/12 [3]."]
    filler = [c["n"] for c in claims if c["n"] not in (n_rate, n_pce, n_meet)]
    # câu mẫu dẫn [1] nhưng nguồn 1 không nói gì về nó → khong_thay; câu PCE bị gắn đoạn trích bịa
    answer = [f'{n_rate} | khop | 1 | "held its benchmark rate steady at a range of 3.75% to 4%"',
              f'{n_pce} | khop | 2 | "Core PCE inflation rose 4.1 percent over the past year"',
              f'{n_meet} | khop | 1 | "Chair Powell said further cuts were not a foregone conclusion"']
    answer += [f"{n} | khong_thay | - | -" for n in filler]
    code, out = run("verify", eid, base, path, "\n".join(answer), monkeypatch, capsys)
    assert "1/" in out and "Lần viết đầu: 1/" in out
    assert "không có trong file nguồn 2" in out and "sai nguồn" in out
    code, out = run("write", eid, base, path, "", monkeypatch, capsys)
    assert code == 2 and "CHƯA LƯU" in out

    # vòng 2: xóa câu không có bằng chứng, giữ câu đã khớp → chỉ phải đối chiếu lại các câu đã sửa
    fixed = ("# Fed giữ lãi suất\n> Fed giữ nguyên lãi suất [1].\n\n" + "\n".join(
        f"## {name}\n" + "Đây là câu phân tích mẫu không có dữ kiện để đủ độ dài cho bài viết thử nghiệm của bản tin. " * 9
        + ("Fed giữ lãi suất ở mức 3,75–4% [1]. Lạm phát PCE lõi tăng 3,8% [2]. Fed họp tiếp ngày 9–10/12 [3]."
           if name == deep.SECTIONS[0] else "") + "\n" for name in deep.SECTIONS)
        + "## Nguồn\n" + "\n".join(f"{i} | Báo {i} | {u} | báo | toàn văn | x" for i, u in SOURCES.items()))
    code, out = run("draft", eid, base, path, fixed, monkeypatch, capsys)
    assert code == 0 and "1 câu giữ kết quả khớp" in out
    state = load_state(eid, base)
    nums = {c["text"]: c["n"] for c in state["claims"]}
    answer = [f'{nums["Fed giữ nguyên lãi suất [1]."]} | khop | 1 | "The Federal Reserve held its benchmark rate steady"',
              f'{nums["Lạm phát PCE lõi tăng 3,8% [2]."]} | khop | 2 | "Core PCE inflation rose 3.8 percent over the past year"',
              f'{nums["Fed họp tiếp ngày 9–10/12 [3]."]} | khop | 3 | "the committee will meet again on December 9 and 10"']
    code, out = run("verify", eid, base, path, "\n".join(answer), monkeypatch, capsys)
    assert "Đủ bằng chứng" in out
    code, out = run("write", eid, base, path, "", monkeypatch, capsys)
    assert code == 0 and "lần viết đầu khớp 1/" in out

    conn = db.connect(path)
    art = json.loads(conn.execute("SELECT article_json FROM events WHERE id=?", (eid,)).fetchone()[0])
    v = art["verification"]
    assert v["ok"] == v["checked"] == 4 and v["rounds"] == 2 and v["first_ok"] == 1
    assert v["items"][0]["evidence"][0]["text"].startswith("The Federal Reserve")
    acc = article_accuracy(conn)
    assert acc["first_ok"] == 1 and acc["final_ok"] == 4


def test_draft_rejects_full_source_without_saved_file(tmp_path, monkeypatch, capsys):
    conn, eid, base = setup_event(tmp_path)
    text = make_article(170).replace("https://reuters.com/a", "https://reuters.com/khac")
    code, out = run("draft", eid, base, tmp_path / "t.db", text, monkeypatch, capsys)
    assert code == 2 and "chưa lưu file" in out


def test_opinion_with_citation_is_not_accepted(tmp_path, monkeypatch, capsys):
    conn, eid, base = setup_event(tmp_path)
    run("draft", eid, base, tmp_path / "t.db", draft_text(), monkeypatch, capsys)
    n = load_state(eid, base)["claims"][0]["n"]
    code, out = run("verify", eid, base, tmp_path / "t.db", f"{n} | nhan_dinh | - | -", monkeypatch, capsys)
    assert "phải đối chiếu" in out


def test_stop_after_max_rounds(tmp_path, monkeypatch, capsys):
    conn, eid, base = setup_event(tmp_path)
    for _ in range(3):
        assert run("draft", eid, base, tmp_path / "t.db", draft_text(), monkeypatch, capsys)[0] == 0
    code, out = run("draft", eid, base, tmp_path / "t.db", draft_text(), monkeypatch, capsys)
    assert code == 3 and "DỪNG" in out


def test_import_article_checks(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ratings = [{"key": "e1-x", "article_check": "loi", "article_errors": "Câu A sai\n\nCâu B sai",
                "event": {"article": {"title": "T", "verification": {"checked": 40}}}, "rated_at": "2026-11-27"},
               {"key": "e2-x", "article_check": "dung", "event": {"article": {"verification": {"checked": 30}}}},
               {"key": "e3-x", "worth": "dang_doc"}]
    assert import_article_checks(ratings, conn) == 2
    acc = article_accuracy(conn)
    assert acc["checked_articles"] == 2 and acc["human_wrong"] == 2 and acc["human_claims"] == 70
