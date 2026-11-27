"""Tính điểm cuối của một sự kiện. Thuần mã, không gọi AI, để dễ kiểm thử và tinh chỉnh."""

IMPACT_FIELDS = ("impact_dau_tu", "impact_nghe_y", "impact_dia_chinh_tri", "impact_viet_nam")


def clamp(value, low: int, high: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, v))


def hotness(source_count: int, max_hn_points: int = 0, max_reddit_score: int = 0) -> float:
    """Độ nóng 0–10, lấy mức cao nhất trong ba tín hiệu.

    - Số nguồn cùng đưa tin: 1 nguồn = 0 điểm, 5 nguồn trở lên = 10 điểm.
    - Điểm Hacker News: 500 điểm trở lên = 10.
    - Điểm Reddit: 3000 điểm trở lên = 10.
    """
    by_sources = min(10.0, max(0, source_count - 1) * 2.5)
    by_hn = min(10.0, max(0, max_hn_points) / 50)
    by_reddit = min(10.0, max(0, max_reddit_score) / 300)
    return round(max(by_sources, by_hn, by_reddit), 1)


IMPACT_FLOOR_FROM = 8   # tin ảnh hưởng mạnh tới mục tiêu (≥ 8)...
HOT_FLOOR = 5.0         # ...thì độ nóng tính tối thiểu 5, để tin chuyên ngành ít báo đưa không bị chìm


def effective_hotness(hot: float, impact: float) -> float:
    """Độ nóng dùng để tính điểm. Tin y khoa hay đầu tư rất sát mục tiêu thường chỉ có 1 nguồn;
    không để việc ít báo đưa kéo điểm của chúng xuống dưới ngưỡng viết thẻ."""
    return max(hot, HOT_FLOOR) if impact >= IMPACT_FLOOR_FROM else hot


def final_score(importance: float, impact: float, hot: float, weights: dict) -> float:
    score = (
        weights.get("importance", 0.5) * importance
        + weights.get("impact", 0.3) * impact
        + weights.get("hotness", 0.2) * hot
    )
    return round(score, 1)


def article_impact(row) -> int:
    """Ảnh hưởng của một bài = mức cao nhất trong các mục tiêu."""
    return max(int(row[f] or 0) for f in IMPACT_FIELDS)


def event_aggregate(article_rows: list, weights: dict) -> dict:
    """Gộp điểm các bài trong một sự kiện thành điểm của sự kiện."""
    if not article_rows:
        return {"importance": 0, "impact": 0, "hotness": 0.0, "score": 0.0, "source_count": 0, "category": None}
    importance = max(int(r["importance"] or 0) for r in article_rows)
    impact = max(article_impact(r) for r in article_rows)
    sources = {r["source"] for r in article_rows}
    hot = hotness(
        len(sources),
        max(int(r["hn_points"] or 0) for r in article_rows),
        max(int(r["reddit_score"] or 0) for r in article_rows),
    )
    # Chuyên mục của sự kiện theo bài quan trọng nhất
    top = max(article_rows, key=lambda r: int(r["importance"] or 0))
    return {
        "importance": importance,
        "impact": impact,
        "hotness": hot,
        "score": final_score(importance, impact, effective_hotness(hot, impact), weights),
        "source_count": len(sources),
        "category": top["category"],
    }
