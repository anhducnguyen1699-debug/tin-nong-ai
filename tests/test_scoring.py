from pipeline.scoring import clamp, event_aggregate, final_score, hotness

W = {"importance": 0.5, "impact": 0.3, "hotness": 0.2}


def test_hotness_takes_strongest_signal():
    assert hotness(1) == 0
    assert hotness(3) == 5.0
    assert hotness(9) == 10.0
    assert hotness(1, max_hn_points=250) == 5.0
    assert hotness(1, max_reddit_score=6000) == 10.0


def test_final_score_formula():
    assert final_score(8, 9, 5, W) == 7.7
    assert final_score(10, 10, 10, W) == 10.0


def test_clamp_handles_bad_values():
    assert clamp(15, 1, 10) == 10
    assert clamp(-3, 0, 10) == 0
    assert clamp(None, 1, 10) == 1


def row(**kw):
    base = {"importance": 5, "impact_dau_tu": 0, "impact_nghe_y": 0, "impact_dia_chinh_tri": 0,
            "impact_viet_nam": 0, "source": "BBC", "hn_points": 0, "reddit_score": 0, "category": "thoi_su_kinh_te"}
    base.update(kw)
    return base


def test_event_aggregate_uses_max_and_counts_distinct_sources():
    rows = [
        row(importance=8, impact_dau_tu=9, source="BBC"),
        row(importance=6, impact_viet_nam=7, source="CNBC", category="ai_cong_nghe"),
        row(importance=5, source="BBC"),
    ]
    agg = event_aggregate(rows, W)
    assert agg["importance"] == 8 and agg["impact"] == 9
    assert agg["source_count"] == 2 and agg["hotness"] == 2.5
    assert agg["category"] == "thoi_su_kinh_te"
    # Ảnh hưởng 9 ≥ 8 nên độ nóng dùng để tính điểm được nâng lên tối thiểu 5 (độ nóng hiển thị vẫn là 2,5)
    assert agg["score"] == final_score(8, 9, 5.0, W)


def test_single_source_high_impact_news_is_not_buried():
    # Tin FDA cấp phép AI đọc CT: 1 nguồn, quan trọng 7, ảnh hưởng nghề y 9 → trước đây 6,2, nay 7,2
    agg = event_aggregate([row(importance=7, impact_nghe_y=9, category="ai_cong_nghe")], W)
    assert agg["hotness"] == 0 and agg["score"] == 7.2
    # Tin ảnh hưởng dưới 8 thì không được nâng
    assert event_aggregate([row(importance=7, impact_dau_tu=7)], W)["score"] == 5.6
