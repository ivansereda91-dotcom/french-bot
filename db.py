"""Хранилище французского бота: SQLite, время по Парижу."""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Paris"))
DB_PATH = os.getenv("DB_PATH", "french.db")

LEVELS = ["A1", "A2", "A2+", "B1", "B1+", "B2", "C1"]
# Лейтнер: через сколько дней повторять слово в коробке N
BOX_DAYS = {1: 1, 2: 3, 3: 7, 4: 16, 5: 35}


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


class _ClosingConnection(sqlite3.Connection):
    """`with connect() as c:` — коммит и сразу закрытие соединения (иначе копятся открытые файлы)."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id   INTEGER PRIMARY KEY,
                name    TEXT NOT NULL,
                level   TEXT NOT NULL DEFAULT 'B1',
                time1   TEXT NOT NULL DEFAULT '09:00',
                time2   TEXT NOT NULL DEFAULT '19:00',
                topics  TEXT NOT NULL DEFAULT 'повседневная жизнь во Франции, Ницца',
                paused  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                kind        TEXT NOT NULL,          -- vocab | grammar | writing | review
                payload     TEXT NOT NULL,          -- JSON от ИИ
                created_at  TEXT NOT NULL,
                day         TEXT NOT NULL,
                answer      TEXT,
                correct     INTEGER,                -- 1/0, для writing — балл 0..10
                feedback    TEXT,                   -- JSON проверки
                answered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_tasks_user ON tasks(user_id, day);
            CREATE TABLE IF NOT EXISTS words (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                fr          TEXT NOT NULL,
                ru          TEXT NOT NULL,
                example     TEXT,
                box         INTEGER NOT NULL DEFAULT 1,
                next_review TEXT NOT NULL,
                UNIQUE (user_id, fr)
            );
            CREATE TABLE IF NOT EXISTS sent (
                user_id INTEGER NOT NULL,
                day     TEXT NOT NULL,
                slot    INTEGER NOT NULL,
                PRIMARY KEY (user_id, day, slot)
            );
            """
        )


# ---------- пользователи ----------

def ensure_user(tg_id: int, name: str, default_level: str = "B1") -> sqlite3.Row:
    with connect() as c:
        c.execute(
            "INSERT INTO users(tg_id, name, level) VALUES (?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name",
            (tg_id, name, default_level),
        )
        return c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def user(tg_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def all_users() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM users ORDER BY tg_id").fetchall()


def update_user(tg_id: int, **fields) -> None:
    allowed = {"level", "time1", "time2", "topics", "paused"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with connect() as c:
        c.execute(
            f"UPDATE users SET {', '.join(f'{k}=?' for k in sets)} WHERE tg_id=?",
            (*sets.values(), tg_id),
        )


def parse_times(text: str) -> tuple[str, str] | None:
    """'9 19' / '09:00 19:30' / '8.30, 20' -> ('09:00','19:00')."""
    import re
    parts = re.findall(r"(\d{1,2})(?:[:.h](\d{2}))?", text or "")
    if len(parts) != 2:
        return None
    out = []
    for h, m in parts:
        h, m = int(h), int(m or 0)
        if h > 23 or m > 59:
            return None
        out.append(f"{h:02d}:{m:02d}")
    return tuple(sorted(out))


# ---------- задания ----------

def add_task(user_id: int, kind: str, payload: dict) -> int:
    t = now()
    with connect() as c:
        return c.execute(
            "INSERT INTO tasks(user_id, kind, payload, created_at, day) VALUES (?,?,?,?,?)",
            (user_id, kind, json.dumps(payload, ensure_ascii=False), t.isoformat(timespec="seconds"),
             t.date().isoformat()),
        ).lastrowid


def task(task_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _task_dict(r) if r else None


def _task_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    d["feedback"] = json.loads(d["feedback"]) if d["feedback"] else None
    return d


def save_answer(task_id: int, answer: str, correct: int, feedback: dict | None = None) -> bool:
    """False, если на задание уже отвечали."""
    with connect() as c:
        cur = c.execute(
            "UPDATE tasks SET answer=?, correct=?, feedback=?, answered_at=? WHERE id=? AND answer IS NULL",
            (answer, correct, json.dumps(feedback, ensure_ascii=False) if feedback else None,
             now().isoformat(timespec="seconds"), task_id),
        )
        return cur.rowcount > 0


def open_writing_task(user_id: int) -> dict | None:
    """Последнее неотвеченное письменное задание за двое суток."""
    since = (today() - timedelta(days=2)).isoformat()
    with connect() as c:
        r = c.execute(
            "SELECT * FROM tasks WHERE user_id=? AND kind='writing' AND answer IS NULL AND day>=? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, since),
        ).fetchone()
    return _task_dict(r) if r else None


def recent_items(user_id: int, limit: int = 40) -> list[str]:
    """Что уже было — чтобы ИИ не повторялся."""
    with connect() as c:
        rows = c.execute(
            "SELECT kind, payload FROM tasks WHERE user_id=? AND kind!='review' ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        out.append(p.get("fr") or p.get("topic") or p.get("text") or "")
    return [x for x in out if x]


def tasks_on(day: str, user_id: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM tasks WHERE day=?", [day]
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    with connect() as c:
        return [_task_dict(r) for r in c.execute(sql + " ORDER BY id", args)]


# ---------- расписание ----------

def mark_sent(user_id: int, day: str, slot: int) -> bool:
    """True, если слот ещё не отправлялся (и теперь помечен)."""
    with connect() as c:
        cur = c.execute("INSERT OR IGNORE INTO sent(user_id, day, slot) VALUES (?,?,?)", (user_id, day, slot))
        return cur.rowcount > 0


def due_slots(at: datetime | None = None, grace_hours: int = 3) -> list[tuple[sqlite3.Row, int]]:
    """Пары (пользователь, слот), которым пора прислать задание."""
    at = at or now()
    hm = at.strftime("%H:%M")
    day = at.date().isoformat()
    limit = (at - timedelta(hours=grace_hours)).strftime("%H:%M")
    out = []
    with connect() as c:
        sent = {(r["user_id"], r["slot"]) for r in c.execute("SELECT * FROM sent WHERE day=?", (day,))}
    for u in all_users():
        if u["paused"]:
            continue
        for slot, t in ((1, u["time1"]), (2, u["time2"])):
            if (u["tg_id"], slot) not in sent and t <= hm and (t >= limit or limit > hm):
                out.append((u, slot))
    return out


# ---------- слова (интервальное повторение) ----------

def add_word(user_id: int, fr: str, ru: str, example: str = "") -> None:
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO words(user_id, fr, ru, example, next_review) VALUES (?,?,?,?,?)",
            (user_id, fr, ru, example, (today() + timedelta(days=1)).isoformat()),
        )


def due_word(user_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute(
            "SELECT * FROM words WHERE user_id=? AND next_review<=? ORDER BY box, next_review LIMIT 1",
            (user_id, today().isoformat()),
        ).fetchone()


def distractors(user_id: int, word_id: int, n: int = 3) -> list[str]:
    with connect() as c:
        return [r["ru"] for r in c.execute(
            "SELECT ru FROM words WHERE user_id=? AND id!=? ORDER BY RANDOM() LIMIT ?", (user_id, word_id, n))]


def review_word(word_id: int, ok: bool) -> None:
    with connect() as c:
        w = c.execute("SELECT box FROM words WHERE id=?", (word_id,)).fetchone()
        box = min(5, w["box"] + 1) if ok else 1
        c.execute("UPDATE words SET box=?, next_review=? WHERE id=?",
                  (box, (today() + timedelta(days=BOX_DAYS[box])).isoformat(), word_id))


def words(user_id: int) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM words WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()


# ---------- статистика ----------

def stats(user_id: int) -> dict:
    with connect() as c:
        rows = c.execute(
            "SELECT kind, correct, day FROM tasks WHERE user_id=? AND answer IS NOT NULL", (user_id,)
        ).fetchall()
        n_words = c.execute("SELECT COUNT(*) FROM words WHERE user_id=?", (user_id,)).fetchone()[0]
        learned = c.execute("SELECT COUNT(*) FROM words WHERE user_id=? AND box>=4", (user_id,)).fetchone()[0]
    quiz = [r["correct"] for r in rows if r["kind"] != "writing"]
    writing = [r["correct"] for r in rows if r["kind"] == "writing"]
    days = {r["day"] for r in rows}
    streak, d = 0, today()
    if d.isoformat() not in days:
        d -= timedelta(days=1)  # сегодня ещё не занимались — серия не прервана
    while d.isoformat() in days:
        streak += 1
        d -= timedelta(days=1)
    return {
        "answered": len(rows),
        "quiz_total": len(quiz),
        "quiz_ok": sum(quiz),
        "writing_avg": round(sum(writing) / len(writing), 1) if writing else None,
        "streak": streak,
        "words": n_words,
        "learned": learned,
    }
