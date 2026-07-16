import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "diary.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT    NOT NULL UNIQUE,
                pin      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id),
                ciphertext   TEXT    NOT NULL,
                shift        INTEGER NOT NULL,
                created_at   TEXT    NOT NULL
            );
        """)
        conn.commit()
    finally:
        conn.close()
