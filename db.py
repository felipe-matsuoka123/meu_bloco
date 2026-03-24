from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from flask import current_app, g
from psycopg.rows import dict_row

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/meu_bloco"


def database_url() -> str:
    return current_app.config.get("DATABASE_URL") or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_db() -> psycopg.Connection:
    if "db" not in g:
        g.db = psycopg.connect(database_url(), row_factory=dict_row)
    return g.db


def close_db(_exception: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def execute(query: str, params: tuple[Any, ...] = ()):
    return get_db().execute(query, params)


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return execute(query, params).fetchone()


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return execute(query, params).fetchall()


def init_db() -> None:
    db = get_db()
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = [statement.strip() for statement in schema.split(";") if statement.strip()]
    for statement in statements:
        db.execute(statement)
    db.commit()


def create_user(username: str, password_hash: str) -> int:
    row = fetch_one(
        """
        INSERT INTO users (username, password_hash)
        VALUES (%s, %s)
        RETURNING id
        """,
        (username, password_hash),
    )
    get_db().commit()
    return int(row["id"])


def find_user_by_username(username: str) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT id, username, password_hash, failed_login_attempts, locked_until
        FROM users
        WHERE username = %s
        """,
        (username,),
    )


def update_user_password(user_id: int, password_hash: str) -> None:
    execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (password_hash, user_id),
    )
    get_db().commit()


def reset_login_failures(user_id: int) -> None:
    execute(
        """
        UPDATE users
        SET failed_login_attempts = 0,
            locked_until = NULL
        WHERE id = %s
        """,
        (user_id,),
    )


def register_failed_login(user_id: int, failed_attempts: int, locked_until: datetime | None) -> None:
    execute(
        """
        UPDATE users
        SET failed_login_attempts = %s,
            locked_until = %s
        WHERE id = %s
        """,
        (failed_attempts, locked_until, user_id),
    )


def list_user_notes(user_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT id, content, created_at
        FROM notes
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,),
    )


def get_user_note(user_id: int, note_id: int) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT id, content, created_at
        FROM notes
        WHERE user_id = %s AND id = %s
        """,
        (user_id, note_id),
    )


def create_note(user_id: int, content: str) -> None:
    execute(
        "INSERT INTO notes (user_id, content) VALUES (%s, %s)",
        (user_id, content),
    )
    get_db().commit()


def update_note(user_id: int, note_id: int, content: str) -> bool:
    cursor = execute(
        """
        UPDATE notes
        SET content = %s
        WHERE id = %s AND user_id = %s
        """,
        (content, note_id, user_id),
    )
    get_db().commit()
    return cursor.rowcount > 0


def delete_note(user_id: int, note_id: int) -> None:
    execute(
        "DELETE FROM notes WHERE id = %s AND user_id = %s",
        (note_id, user_id),
    )
    get_db().commit()


def get_note_review_count(note_id: int, usage_date: date) -> int:
    row = fetch_one(
        """
        SELECT request_count
        FROM note_review_usage
        WHERE note_id = %s AND usage_date = %s
        """,
        (note_id, usage_date),
    )
    return int(row["request_count"]) if row else 0


def increment_note_review_count(note_id: int, usage_date: date) -> None:
    execute(
        """
        INSERT INTO note_review_usage (note_id, usage_date, request_count)
        VALUES (%s, %s, 1)
        ON CONFLICT (note_id, usage_date)
        DO UPDATE SET request_count = note_review_usage.request_count + 1
        """,
        (note_id, usage_date),
    )
    get_db().commit()
