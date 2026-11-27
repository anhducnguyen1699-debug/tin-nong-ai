import json

import pytest

from pipeline import db
from pipeline.hand import parse_answer
from pipeline.models import Article


def test_skip_unsampled_keeps_sources_balanced(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    items = [(f"a{i}", Article(url=f"https://a.com/{i}", title=f"A {i}", source="A")) for i in range(10)]
    items += [(f"b{i}", Article(url=f"https://b.com/{i}", title=f"B {i}", source="B")) for i in range(2)]
    items += [("hn", Article(url="https://h.com/1", title="HN", source="h.com", origin="hacker_news", hn_points=500))]
    db.insert_articles(conn, items)
    skipped = db.skip_unsampled(conn, 36, keep=5)
    assert skipped == 8
    kept = conn.execute("SELECT source FROM articles WHERE scored_at IS NULL").fetchall()
    sources = sorted(r["source"] for r in kept)
    assert sources.count("B") == 2 and "h.com" in sources and sources.count("A") == 2
    # Bài bị bỏ không bị đem đi gộp sự kiện
    assert db.unclustered_articles(conn, 0) == []


def test_skip_unsampled_noop_when_under_limit(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.insert_articles(conn, [("x", Article(url="u", title="t", source="s"))])
    assert db.skip_unsampled(conn, 36, keep=300) == 0


CLASSIFY_USER = '<bai id="1">\nnguon: A\ntieu_de: x\nmo_ta: \n</bai>\n<bai id="2">\nnguon: B\ntieu_de: y\nmo_ta: \n</bai>'


def test_parse_classify_answer():
    out = json.loads(parse_answer("classify", "1|t|8|7|0|6|9|Thuế Mỹ\n2|b|1|0|0|0|0|Thể thao", CLASSIFY_USER))
    assert out["items"][0]["category"] == "thoi_su_kinh_te" and out["items"][1]["category"] == "bo_qua"


def test_parse_classify_rejects_missing_rows():
    with pytest.raises(ValueError, match="2 bài"):
        parse_answer("classify", "1|t|8|7|0|6|9|Thuế Mỹ", CLASSIFY_USER)


def test_parse_cluster_answer():
    user = "<su_kien_dang_mo>\n5: Cũ\n</su_kien_dang_mo>\n\n<bai_moi>\n1. [A] x\n2. [B] y\n3. [C] z\n</bai_moi>"
    out = json.loads(parse_answer("cluster", "e5:1 moi:2", user))
    d = {x["article_id"]: x for x in out["decisions"]}
    assert d[1]["event_id"] == 5 and d[2]["new_group"] == "moi" and d[3]["new_group"] == "rieng-3"


def test_parse_summarize_answer():
    lines = ["Tiêu đề", "Ý 1", "1", "Ý 2", "2", "Ý 3", "1", "Vì sao", "Nên làm", "dau_tu,nghe_y", "1"]
    out = json.loads(parse_answer("summarize", "\n".join(lines), ""))
    assert out["affected_goals"] == ["dau_tu", "nghe_y"] and out["is_investment_related"] is True
    with pytest.raises(ValueError, match="11 dòng"):
        parse_answer("summarize", "\n".join(lines[:5]), "")


def test_preference_examples_fall_back_to_golden(tmp_path, monkeypatch):
    from pipeline import classify as cl
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "golden.json").write_text(json.dumps([
        {"title": "Tin A", "user_worth": True}, {"title": "Tin B", "user_worth": False},
        {"title": "Tin C", "title_vi": "Tin C tiếng Việt", "user_worth": True},
    ]), encoding="utf-8")
    monkeypatch.setattr(cl, "ROOT", tmp_path)
    conn = db.connect(tmp_path / "t.db")
    liked, disliked = cl.preference_examples(conn)
    assert liked == ["Tin C tiếng Việt", "Tin A"] and disliked == ["Tin B"]
