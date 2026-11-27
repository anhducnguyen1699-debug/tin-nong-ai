"""Kiểm thử cả luồng chấm điểm → gộp sự kiện → viết thẻ với một AI giả (không tốn tiền, không cần mạng)."""
import pytest

from pipeline import db
from pipeline.classify import build_user_message, classify, map_results
from pipeline.cluster import best_fuzzy_match, cluster
from pipeline.config import load_settings
from pipeline.llm import LLM, BudgetExceeded
from pipeline.models import (
    Article, ArticleScore, ClassifyResult, ClusterDecision, ClusterResult, EventCard, KeyPoint,
)
from pipeline.summarize import pick_sources, summarize, to_card


class FakeLLM:
    """Đóng vai Claude: chấm điểm theo từ khóa, gộp theo nhãn đặt sẵn, viết thẻ mẫu."""

    def __init__(self):
        self.calls: list[str] = []

    def parse(self, step, system, user, output_format, max_tokens):
        self.calls.append(step)
        assert "Hồ sơ người đọc" in system  # hồ sơ luôn được gắn vào hướng dẫn hệ thống
        if step == "classify":
            items = []
            for i, line in enumerate([l for l in user.splitlines() if l.startswith("tieu_de:")], start=1):
                important = "Fed" in line
                items.append(ArticleScore(
                    id=i, category="thoi_su_kinh_te" if important else "bo_qua",
                    importance=9 if important else 1, impact_dau_tu=9 if important else 0,
                    impact_nghe_y=0, impact_dia_chinh_tri=6, impact_viet_nam=8, reason="lý do",
                ))
            items.append(ArticleScore(id=99, category="bo_qua", importance=1, impact_dau_tu=0, impact_nghe_y=0,
                                      impact_dia_chinh_tri=0, impact_viet_nam=0, reason="id lạ"))
            return ClassifyResult(items=items)
        if step == "cluster":
            n = sum(1 for l in user.splitlines() if l[:1].isdigit() and ". [" in l)
            return ClusterResult(decisions=[
                ClusterDecision(article_id=i, event_id=0, new_group="fed") for i in range(1, n + 1)
            ])
        if step == "summarize":
            return EventCard(
                title_vi="Fed giữ nguyên lãi suất",
                key_points=[KeyPoint(text="Ý 1", source_id=1), KeyPoint(text="Ý 2", source_id=2),
                            KeyPoint(text="Ý 3", source_id=42)],
                why_it_matters="Ảnh hưởng tỷ giá.", what_to_do="Theo dõi tỷ giá trung tâm.",
                affected_goals=["dau_tu", "dau_tu", "dia_chinh_tri"], is_investment_related=True,
            )
        raise AssertionError(step)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.insert_articles(c, [
        ("k1", Article(url="https://bbc.com/1", title="Fed holds interest rates steady", source="BBC")),
        ("k2", Article(url="https://cnbc.com/2", title="Fed keeps rates unchanged, signals caution", source="CNBC")),
        ("k3", Article(url="https://vnexpress.net/3", title="Fed giữ nguyên lãi suất", source="VnExpress", lang="vi")),
        ("k4", Article(url="https://bbc.com/4", title="Football club wins cup", source="BBC")),
    ])
    return c


def test_insert_articles_ignores_duplicates(conn):
    again = db.insert_articles(conn, [("k1", Article(url="x", title="y", source="z"))])
    assert again == 0


def test_full_flow_with_fake_llm(conn):
    settings = load_settings()
    llm = FakeLLM()
    rows = db.unscored_articles(conn, 36)
    scored, errors = classify(conn, llm, settings, rows)
    assert scored == 4 and errors == []

    stats, errors = cluster(conn, llm, settings)
    assert errors == []
    events = conn.execute("SELECT * FROM events").fetchall()
    assert len(events) == 1  # 3 bài về Fed gộp thành 1 sự kiện, bài bóng đá bị bỏ qua
    assert events[0]["source_count"] == 3 and events[0]["score"] >= 7

    written, errors = summarize(conn, llm, settings, http_client=None)
    assert written == 1 and errors == []
    card = conn.execute("SELECT card_json, title_vi FROM events").fetchone()
    assert card["title_vi"] == "Fed giữ nguyên lãi suất"
    assert '"disclaimer": "Không phải lời khuyên đầu tư."' in card["card_json"]

    # Lượt sau không viết lại thẻ khi chưa có nguồn mới
    written, _ = summarize(conn, llm, settings, http_client=None)
    assert written == 0
    assert llm.calls == ["classify", "cluster", "summarize"]


def test_map_results_drops_unknown_ids_and_forces_skip_low():
    batch = [{"id": 10}, {"id": 11}]
    result = ClassifyResult(items=[
        ArticleScore(id=1, category="bo_qua", importance=8, impact_dau_tu=5, impact_nghe_y=0,
                     impact_dia_chinh_tri=0, impact_viet_nam=0, reason="r"),
        ArticleScore(id=7, category="ai_cong_nghe", importance=20, impact_dau_tu=0, impact_nghe_y=0,
                     impact_dia_chinh_tri=0, impact_viet_nam=0, reason="r"),
    ])
    scores = map_results(batch, result)
    assert list(scores) == [10]
    assert scores[10]["importance"] == 2 and scores[10]["impact_dau_tu"] == 0


def test_classify_message_escapes_and_includes_preferences():
    msg = build_user_message([{"source": "A&B", "title": "<x>", "summary": None}], ["thích"], ["chán"])
    assert "A&amp;B" in msg and "&lt;x&gt;" in msg
    assert "<so_thich>" in msg and "thích" in msg and "chán" in msg


def test_fuzzy_match_is_strict_for_short_titles():
    events = {1: ["Gold hits record high as dollar weakens"]}
    assert best_fuzzy_match("Gold hits record high as dollar weakens!", events, 88) == 1
    assert best_fuzzy_match("Gold", events, 88) is None


def test_to_card_drops_bad_source_ids_and_duplicate_goals():
    sources = [{"id": 1, "name": "BBC", "url": "u1", "title": "t1"}, {"id": 2, "name": "CNBC", "url": "u2", "title": "t2"}]
    card = FakeLLM().parse("summarize", "Hồ sơ người đọc", "", EventCard, 0)
    out = to_card(card, sources, "claude-haiku-5-5")
    assert [kp["source_id"] for kp in out["key_points"]] == [1, 2, None]
    assert out["affected_goals"] == ["dau_tu", "dia_chinh_tri"]


def test_pick_sources_prefers_distinct_outlets():
    rows = [
        {"source": "BBC", "importance": 9, "summary": "a"},
        {"source": "BBC", "importance": 8, "summary": "b"},
        {"source": "CNBC", "importance": 7, "summary": ""},
    ]
    assert [r["source"] for r in pick_sources(rows, 4)] == ["BBC", "CNBC"]


def test_pick_sources_puts_title_only_google_news_last():
    rows = [
        {"source": "AP News", "importance": 9, "summary": "", "origin": "google_news"},
        {"source": "CafeF", "importance": 7, "summary": "Mô tả dài", "origin": "rss"},
        {"source": "BBC", "importance": 8, "summary": "", "origin": "rss"},
    ]
    assert [r["source"] for r in pick_sources(rows, 4)] == ["CafeF", "BBC", "AP News"]


def test_budget_guard_blocks_summaries_but_not_classify(conn):
    settings = load_settings()
    db.record_llm_call(conn, None, "summarize", "claude-haiku-5-5", 0, 0, 50.0)
    llm = LLM(settings, conn, client=object())
    llm.check_budget("classify")  # vẫn được chạy
    with pytest.raises(BudgetExceeded):
        llm.check_budget("summarize")


def test_cluster_recomputes_scores_even_when_interrupted(conn):  # noqa: F811
    settings = load_settings()
    classify(conn, FakeLLM(), settings, db.unscored_articles(conn, 36))

    class Stops(FakeLLM):
        calls_left = 1

        def parse(self, step, *a, **kw):
            if step == "cluster" and self.calls_left == 0:
                raise RuntimeError("dừng giữa chừng")
            self.calls_left -= 1
            return super().parse(step, *a, **kw)

    settings = {**settings, "cluster": {**settings["cluster"], "batch_size": 2}}
    with pytest.raises(RuntimeError):
        cluster(conn, Stops(), settings)
    scored = conn.execute("SELECT COUNT(*) FROM events WHERE score > 0").fetchone()[0]
    assert scored >= 1  # lô đầu đã gộp xong thì sự kiện của nó phải có điểm
