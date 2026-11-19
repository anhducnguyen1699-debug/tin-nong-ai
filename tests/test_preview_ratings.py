import json
import re

import pytest

from pipeline import db, ratings
from pipeline.classify import classify
from pipeline.cluster import cluster
from pipeline.config import load_settings
from pipeline.eval import action_rate, agreement
from pipeline.preview import build_data, event_key, render
from pipeline.summarize import summarize
from tests.test_pipeline import FakeLLM, conn  # noqa: F401  (dùng lại dữ liệu mẫu)


@pytest.fixture
def processed(conn):  # noqa: F811
    settings = load_settings()
    llm = FakeLLM()
    classify(conn, llm, settings, db.unscored_articles(conn, 36))
    cluster(conn, llm, settings)
    summarize(conn, llm, settings, http_client=None)
    return conn, settings


def test_preview_embeds_safe_json(processed):
    conn, settings = processed
    conn.execute("UPDATE articles SET title = 'Fed </script><b>x' WHERE id = 1")
    data = build_data(conn, settings)
    assert len(data["events"]) == 1 and data["events"][0]["card"]["title_vi"] == "Fed giữ nguyên lãi suất"
    html = render(data)
    assert "__DATA__" not in html and "</script><b>" not in html
    payload = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
    assert json.loads(payload)["events"][0]["key"] == data["events"][0]["key"]


def test_event_key_is_valid_db_path_segment(processed):
    conn, _ = processed
    key = event_key(conn.execute("SELECT * FROM events").fetchone())
    assert re.fullmatch(r"e\d+-\d{8}T\d{6}", key)


def test_import_ratings_writes_feedback_and_golden(processed, tmp_path, monkeypatch):
    conn, settings = processed
    monkeypatch.setattr(ratings, "GOLDEN", tmp_path / "golden.json")
    ev = build_data(conn, settings)["events"][0]
    export = [{"id": ev["key"], "data": {
        "key": ev["key"], "worth": "dang_doc", "score_judgement": "dung", "action_ok": "co", "note": "",
        "event": {"title": ev["title"], "category": ev["category"], "score": ev["score"], "hotness": ev["hotness"],
                  "importance": ev["importance"], "impact": ev["impact"], "summary": "",
                  "articles": ev["articles"]},
    }}, {"id": "e999-20250101T000000", "data": {"key": "e999-20250101T000000", "worth": None}}]
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf-8")

    saved, total = ratings.import_ratings(ratings.load_export(path), conn)
    assert saved == 1 and total == 1
    liked, disliked = db.feedback_examples(conn)
    assert liked == ["Fed giữ nguyên lãi suất"] and disliked == []
    golden = json.loads((tmp_path / "golden.json").read_text(encoding="utf-8"))
    assert golden[0]["user_worth"] is True and golden[0]["action_ok"] == "co"


def test_agreement_and_action_rate():
    golden = [{"user_worth": True}, {"user_worth": False}, {"user_worth": True}, {"user_worth": False}]
    a = agreement(golden, [8.0, 7.5, 6.0, None], threshold=7)
    assert a["n"] == 3 and a["ai_too_high"] == 1 and a["ai_too_low"] == 1
    assert a["agreement"] == pytest.approx(1 / 3)
    n, rate = action_rate([{"action_ok": "co"}, {"action_ok": "khong"}, {"action_ok": None}])
    assert n == 2 and rate == 0.5
