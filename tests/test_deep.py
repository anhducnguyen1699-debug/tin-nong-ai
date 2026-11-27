import json
import sqlite3

import pytest

from pipeline import db
from pipeline.config import load_settings
from pipeline.deep import SECTIONS, ArticleError, list_pending, parse_article
from pipeline.models import Article
from pipeline.preview import build_data, render

SETTINGS = load_settings()
FILLER = "Đây là câu phân tích mẫu có dẫn nguồn để đủ độ dài cho bài viết thử nghiệm của bản tin [1]. "


def make_article(words_per_section=30, sources=None, extra="", heading_override=None):
    sources = sources or [
        "1 | Reuters | https://reuters.com/a | báo | toàn văn | số liệu chính",
        "2 | AP | https://apnews.com/b | báo | toàn văn | phản ứng",
        "3 | Fed | https://federalreserve.gov/c | nguồn gốc | toàn văn | thông cáo gốc",
    ]
    sentences = max(1, words_per_section // 17)
    parts = ["# Fed tăng lãi suất", "> Sapo ngắn gọn về sự kiện [3].", ""]
    for name in SECTIONS:
        parts += [f"## {heading_override.get(name, name) if heading_override else name}", FILLER * sentences + extra, ""]
    parts += ["## Nguồn", *sources]
    return "\n".join(parts)


def test_parse_valid_article():
    a = parse_article(make_article(170), SETTINGS)
    assert a["title"] == "Fed tăng lãi suất" and len(a["sections"]) == 6
    assert a["full_sources"] == 3 and a["sources"][2]["kind"] == "goc"
    assert 700 <= a["words"] <= 1400
    assert any("Nguồn [2]" in w for w in a["warnings"])  # nguồn 2 chưa được dẫn


def test_rejects_too_short():
    with pytest.raises(ArticleError, match="từ"):
        parse_article(make_article(30), SETTINGS)


def test_rejects_missing_section():
    text = make_article(170, heading_override={"Bối cảnh": "Ngữ cảnh"})
    with pytest.raises(ArticleError, match="Thiếu mục: Bối cảnh"):
        parse_article(text, SETTINGS)


def test_rejects_unknown_citation():
    with pytest.raises(ArticleError, match=r"\[9\]"):
        parse_article(make_article(170, extra="Một nhận định khác [9]."), SETTINGS)


def test_rejects_long_quote():
    quote = "“" + " ".join(["chữ"] * 30) + "”"
    with pytest.raises(ArticleError, match="Trích dẫn"):
        parse_article(make_article(140, extra=quote), SETTINGS)


def test_rejects_too_few_full_sources():
    sources = ["1 | Reuters | https://r.com | báo | toàn văn | x", "2 | AP | https://a.com | báo | chỉ tiêu đề | y"]
    with pytest.raises(ArticleError, match="toàn văn"):
        parse_article(make_article(170, sources=sources), SETTINGS)


def test_migration_adds_article_columns_to_old_db(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT NOT NULL, title_vi TEXT, category TEXT, "
                "importance INTEGER, impact INTEGER, hotness REAL, score REAL, source_count INTEGER, "
                "first_seen TEXT NOT NULL, last_updated TEXT NOT NULL, card_json TEXT, card_at TEXT, card_source_count INTEGER)")
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    assert {"article_json", "article_at", "article_source_count"} <= cols


def test_list_save_and_render(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.insert_articles(conn, [("k", Article(url="https://news.google.com/x", title="Fed hikes", source="AP",
                                            origin="google_news"))])
    eid = db.create_event(conn, "Fed hikes", "thoi_su_kinh_te")
    db.attach(conn, 1, eid)
    conn.execute("UPDATE events SET score=8.0, source_count=1 WHERE id=?", (eid,))
    conn.commit()
    listing = list_pending(conn, SETTINGS)
    assert f"Sự kiện {eid}" in listing and "Google News" in listing

    db.save_article(conn, eid, parse_article(make_article(170), SETTINGS), 1)
    assert "Không có sự kiện" in list_pending(conn, SETTINGS)
    data = build_data(conn, SETTINGS)
    assert data["events"][0]["article"]["words"] >= 700
    html = render(data)
    assert "Đọc bài phân tích" in html and "__DATA__" not in html
    payload = json.loads(html.split('id="data">')[1].split("</script>")[0].replace("<\\/", "</"))
    assert payload["events"][0]["article"]["sections"][0]["heading"] == SECTIONS[0]
