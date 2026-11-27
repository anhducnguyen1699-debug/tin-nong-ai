"""Lưu trữ bằng SQLite (giai đoạn kiểm chứng). Giai đoạn 2 sẽ thay bằng Supabase, giữ nguyên tên hàm."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Article
from .scoring import event_aggregate

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT,
    lang TEXT,
    origin TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    hn_points INTEGER DEFAULT 0,
    reddit_score INTEGER DEFAULT 0,
    category TEXT,
    importance INTEGER,
    impact_dau_tu INTEGER,
    impact_nghe_y INTEGER,
    impact_dia_chinh_tri INTEGER,
    impact_viet_nam INTEGER,
    reason TEXT,
    scored_at TEXT,
    event_id INTEGER REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_articles_event ON articles(event_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    title_vi TEXT,
    category TEXT,
    importance INTEGER DEFAULT 0,
    impact INTEGER DEFAULT 0,
    hotness REAL DEFAULT 0,
    score REAL DEFAULT 0,
    source_count INTEGER DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    card_json TEXT,
    card_at TEXT,
    card_source_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT,
    errors_json TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    step TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    at TEXT NOT NULL
);

-- Phản hồi của người đọc: nút Hữu ích/Bỏ qua (giai đoạn 2) và phiếu chấm ở trang xem thử (giai đoạn 1)
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    value TEXT NOT NULL,            -- huu_ich | bo_qua
    score_judgement TEXT,           -- dung | cao | thap (chỉ có ở phiếu chấm)
    action_ok INTEGER,              -- 1 nếu phần "Nên làm gì" dùng được
    note TEXT,
    at TEXT NOT NULL,
    UNIQUE(event_id)
);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---------- Bài báo ----------
def insert_articles(conn, items: list[tuple[str, Article]]) -> int:
    """Chèn các bài mới; bài đã có (trùng dedupe_key) bị bỏ qua. Trả về số bài thật sự mới."""
    now = iso(now_utc())
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO articles
           (dedupe_key, url, title, source, summary, lang, origin, published_at, fetched_at, hn_points, reddit_score)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (key, a.url, a.title, a.source, a.summary, a.lang, a.origin, iso(a.published_at), now,
             a.hn_points, a.reddit_score)
            for key, a in items
        ],
    )
    conn.commit()
    return conn.total_changes - before


def unscored_articles(conn, max_age_hours: int, limit: int | None = None) -> list[sqlite3.Row]:
    since = iso(now_utc() - timedelta(hours=max_age_hours))
    sql = "SELECT * FROM articles WHERE scored_at IS NULL AND fetched_at >= ? ORDER BY COALESCE(published_at, fetched_at) DESC"
    params: list = [since]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def skip_unsampled(conn, max_age_hours: int, keep: int) -> int:
    """Giữ lại tối đa `keep` bài chưa chấm để đem đi chấm, lấy đều theo nguồn (mỗi nguồn lần lượt một bài,
    bài mới trước; Hacker News xếp đầu vì có điểm nóng). Bài còn lại đánh dấu 'bo_mau' để không chấm lại.
    Trả về số bài bị bỏ."""
    rows = unscored_articles(conn, max_age_hours)
    if len(rows) <= keep:
        return 0
    queues: dict[str, list[int]] = {}
    for r in sorted(rows, key=lambda r: r["origin"] != "hacker_news"):
        queues.setdefault(r["source"], []).append(r["id"])
    picked: list[int] = []
    while len(picked) < keep and any(queues.values()):
        for q in queues.values():
            if q and len(picked) < keep:
                picked.append(q.pop(0))
    skipped = [r["id"] for r in rows if r["id"] not in set(picked)]
    now = iso(now_utc())
    conn.executemany("UPDATE articles SET scored_at=?, reason='bo_mau' WHERE id=?", [(now, i) for i in skipped])
    conn.commit()
    return len(skipped)


def save_scores(conn, scores: dict[int, dict]) -> None:
    now = iso(now_utc())
    conn.executemany(
        """UPDATE articles SET category=?, importance=?, impact_dau_tu=?, impact_nghe_y=?,
           impact_dia_chinh_tri=?, impact_viet_nam=?, reason=?, scored_at=? WHERE id=?""",
        [
            (s["category"], s["importance"], s["impact_dau_tu"], s["impact_nghe_y"],
             s["impact_dia_chinh_tri"], s["impact_viet_nam"], s["reason"], now, article_id)
            for article_id, s in scores.items()
        ],
    )
    conn.commit()


def unclustered_articles(conn, min_importance: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM articles WHERE scored_at IS NOT NULL AND event_id IS NULL
           AND category != 'bo_qua' AND importance >= ? ORDER BY importance DESC""",
        (min_importance,),
    ).fetchall()


# ---------- Sự kiện ----------
def open_events(conn, window_hours: int, limit: int) -> list[sqlite3.Row]:
    since = iso(now_utc() - timedelta(hours=window_hours))
    return conn.execute(
        "SELECT * FROM events WHERE last_updated >= ? ORDER BY score DESC, last_updated DESC LIMIT ?",
        (since, limit),
    ).fetchall()


def event_articles(conn, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM articles WHERE event_id = ? ORDER BY importance DESC, published_at DESC", (event_id,)
    ).fetchall()


def create_event(conn, title: str, category: str | None) -> int:
    now = iso(now_utc())
    cur = conn.execute(
        "INSERT INTO events (title, category, first_seen, last_updated) VALUES (?,?,?,?)",
        (title, category, now, now),
    )
    conn.commit()
    return cur.lastrowid


def attach(conn, article_id: int, event_id: int) -> None:
    conn.execute("UPDATE articles SET event_id=? WHERE id=?", (event_id, article_id))
    conn.execute("UPDATE events SET last_updated=? WHERE id=?", (iso(now_utc()), event_id))
    conn.commit()


def recompute_event(conn, event_id: int, weights: dict) -> dict:
    agg = event_aggregate(event_articles(conn, event_id), weights)
    conn.execute(
        """UPDATE events SET importance=?, impact=?, hotness=?, score=?, source_count=?,
           category=COALESCE(?, category) WHERE id=?""",
        (agg["importance"], agg["impact"], agg["hotness"], agg["score"], agg["source_count"],
         agg["category"], event_id),
    )
    conn.commit()
    return agg


def events_needing_card(conn, min_score: float, resummarize_after_hours: int, new_sources: int, limit: int):
    cutoff = iso(now_utc() - timedelta(hours=resummarize_after_hours))
    return conn.execute(
        """SELECT * FROM events WHERE score >= ? AND (
               card_json IS NULL
               OR (card_at < ? AND source_count - card_source_count >= ?)
           ) ORDER BY score DESC LIMIT ?""",
        (min_score, cutoff, new_sources, limit),
    ).fetchall()


def save_card(conn, event_id: int, card: dict, source_count: int) -> None:
    conn.execute(
        "UPDATE events SET title_vi=?, card_json=?, card_at=?, card_source_count=? WHERE id=?",
        (card.get("title_vi"), json.dumps(card, ensure_ascii=False), iso(now_utc()), source_count, event_id),
    )
    conn.commit()


def events_for_preview(conn, since_hours: int, min_score: float) -> list[sqlite3.Row]:
    since = iso(now_utc() - timedelta(hours=since_hours))
    return conn.execute(
        "SELECT * FROM events WHERE last_updated >= ? AND score >= ? ORDER BY score DESC",
        (since, min_score),
    ).fetchall()


# ---------- Chi phí và lượt chạy ----------
def record_llm_call(conn, run_id, step, model, input_tokens, output_tokens, cost) -> None:
    conn.execute(
        "INSERT INTO llm_calls (run_id, step, model, input_tokens, output_tokens, cost_usd, at) VALUES (?,?,?,?,?,?,?)",
        (run_id, step, model, input_tokens, output_tokens, cost, iso(now_utc())),
    )
    conn.commit()


def month_cost(conn) -> float:
    start = now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE at >= ?", (iso(start),)).fetchone()
    return float(row[0])


def start_run(conn) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (iso(now_utc()),))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, stats: dict, errors: list[str]) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, stats_json=?, errors_json=? WHERE id=?",
        (iso(now_utc()), json.dumps(stats, ensure_ascii=False), json.dumps(errors, ensure_ascii=False), run_id),
    )
    conn.commit()


# ---------- Phản hồi (bộ nhớ sở thích) ----------
def save_feedback(conn, event_id: int, value: str, score_judgement: str | None = None,
                  action_ok: bool | None = None, note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO feedback (event_id, value, score_judgement, action_ok, note, at) VALUES (?,?,?,?,?,?)
           ON CONFLICT(event_id) DO UPDATE SET value=excluded.value, score_judgement=excluded.score_judgement,
           action_ok=excluded.action_ok, note=excluded.note, at=excluded.at""",
        (event_id, value, score_judgement, None if action_ok is None else int(action_ok), note, iso(now_utc())),
    )
    conn.commit()


def feedback_examples(conn, per_side: int = 15) -> tuple[list[str], list[str]]:
    """Tiêu đề các sự kiện người đọc thấy hữu ích / muốn bỏ qua gần đây, dùng làm ví dụ cho AI."""
    def titles(value: str) -> list[str]:
        rows = conn.execute(
            """SELECT COALESCE(e.title_vi, e.title) AS t FROM feedback f JOIN events e ON e.id = f.event_id
               WHERE f.value = ? ORDER BY f.at DESC LIMIT ?""",
            (value, per_side),
        ).fetchall()
        return [r["t"] for r in rows]

    return titles("huu_ich"), titles("bo_qua")
