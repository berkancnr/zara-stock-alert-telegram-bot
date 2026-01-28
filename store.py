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
            last_price TEXT,
            UNIQUE(chat_id, url, size)
        );
        """)
        # Check if last_price column exists (migration for existing dbs)
        try:
            con.execute("SELECT last_price FROM watches LIMIT 1")
        except sqlite3.OperationalError:
            con.execute("ALTER TABLE watches ADD COLUMN last_price TEXT")

        # Check if product_name column exists (migration for existing dbs)
        try:
            con.execute("SELECT product_name FROM watches LIMIT 1")
        except sqlite3.OperationalError:
            con.execute("ALTER TABLE watches ADD COLUMN product_name TEXT")
        
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
    last_price: Optional[str]
    product_name: Optional[str] = None

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

def remove_all_watches(chat_id: str) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "DELETE FROM watches WHERE chat_id=?",
            (chat_id,),
        )
        con.commit()
        return cur.rowcount

def list_watches(chat_id: str) -> List[Watch]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT id, chat_id, url, size, created_at, last_status, last_availability, last_price, product_name FROM watches WHERE chat_id=? ORDER BY id DESC",
            (chat_id,),
        )
        rows = cur.fetchall()
    return [Watch(*r) for r in rows]

def list_all_watches() -> List[Watch]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT id, chat_id, url, size, created_at, last_status, last_availability, last_price, product_name FROM watches ORDER BY id ASC"
        )
        rows = cur.fetchall()
    return [Watch(*r) for r in rows]

def update_watch_status(watch_id: int, last_status: str, last_availability: str, last_price: Optional[str] = None, product_name: Optional[str] = None):
    with sqlite3.connect(DB_PATH) as con:
        sql = "UPDATE watches SET last_status=?, last_availability=?"
        params = [last_status, last_availability]
        
        if last_price is not None:
            sql += ", last_price=?"
            params.append(last_price)
            
        if product_name is not None:
            sql += ", product_name=?"
            params.append(product_name)
            
        sql += " WHERE id=?"
        params.append(watch_id)
        
        con.execute(sql, tuple(params))
        con.commit()
