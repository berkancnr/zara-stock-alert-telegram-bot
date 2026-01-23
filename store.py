import sqlite3
from dataclasses import dataclass
from typing import List, Optional

DB_PATH = "watches.db"

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            url TEXT NOT NULL,
            size TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_status TEXT,
            last_availability TEXT,
            UNIQUE(chat_id, url, size)
        );
        """)
        con.commit()

@dataclass
class Watch:
    id: int
    chat_id: str
    url: str
    size: str
    created_at: int
    last_status: Optional[str]
    last_availability: Optional[str]

def add_watch(chat_id: str, url: str, size: str, now_ts: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        try:
            con.execute(
                "INSERT INTO watches(chat_id, url, size, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, url, size.upper(), now_ts),
            )
            con.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def remove_watch(chat_id: str, url: str, size: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "DELETE FROM watches WHERE chat_id=? AND url=? AND size=?",
            (chat_id, url, size.upper()),
        )
        con.commit()
        return cur.rowcount > 0

def list_watches(chat_id: str) -> List[Watch]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT id, chat_id, url, size, created_at, last_status, last_availability FROM watches WHERE chat_id=? ORDER BY id DESC",
            (chat_id,),
        )
        rows = cur.fetchall()
    return [Watch(*r) for r in rows]

def list_all_watches() -> List[Watch]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT id, chat_id, url, size, created_at, last_status, last_availability FROM watches ORDER BY id ASC"
        )
        rows = cur.fetchall()
    return [Watch(*r) for r in rows]

def update_watch_status(watch_id: int, last_status: str, last_availability: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE watches SET last_status=?, last_availability=? WHERE id=?",
            (last_status, last_availability, watch_id),
        )
        con.commit()
