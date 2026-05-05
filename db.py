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
        SELECT id, title, diagnosis, review_output, content, created_at, location_id, status, is_favorite
        FROM notes
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,),
    )


def get_user_note(user_id: int, note_id: int) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT id, title, diagnosis, review_output, content, created_at, location_id, status, is_favorite
        FROM notes
        WHERE user_id = %s AND id = %s
        """,
        (user_id, note_id),
    )


def create_note(user_id: int, title: str, diagnosis: str, content: str, location_id: int | None = None) -> int:
    cursor = execute(
        "INSERT INTO notes (user_id, title, diagnosis, review_output, content, location_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, title, diagnosis, "", content, location_id),
    )
    new_id = cursor.fetchone()["id"]
    get_db().commit()
    return int(new_id)


def update_note(user_id: int, note_id: int, title: str, diagnosis: str, review_output: str) -> bool:
    cursor = execute(
        """
        UPDATE notes
        SET title = %s,
            diagnosis = %s,
            review_output = %s
        WHERE id = %s AND user_id = %s
        """,
        (title, diagnosis, review_output, note_id, user_id),
    )
    get_db().commit()
    return cursor.rowcount > 0


def delete_note(user_id: int, note_id: int) -> None:
    execute(
        "UPDATE notes SET status = 'deletado', updated_at = CURRENT_TIMESTAMP WHERE id = %s AND user_id = %s",
        (note_id, user_id),
    )
    get_db().commit()

def hard_delete_old_notes(user_id: int) -> None:
    execute(
        "DELETE FROM notes WHERE user_id = %s AND status = 'deletado' AND updated_at < CURRENT_TIMESTAMP - INTERVAL '7 days'",
        (user_id,)
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


def get_user_saved_sbar(user_id: int) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT selected_note_ids, rows, updated_at
        FROM user_saved_sbar
        WHERE user_id = %s
        """,
        (user_id,),
    )


def save_user_sbar(user_id: int, selected_note_ids: list[int], rows: list[dict[str, Any]]) -> None:
    execute(
        """
        INSERT INTO user_saved_sbar (user_id, selected_note_ids, rows, updated_at)
        VALUES (%s, %s::jsonb, %s::jsonb, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id)
        DO UPDATE SET
            selected_note_ids = EXCLUDED.selected_note_ids,
            rows = EXCLUDED.rows,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, psycopg.types.json.Jsonb(selected_note_ids), psycopg.types.json.Jsonb(rows)),
    )
    get_db().commit()


def delete_user_sbar(user_id: int) -> None:
    execute(
        "DELETE FROM user_saved_sbar WHERE user_id = %s",
        (user_id,),
    )
    get_db().commit()


def update_recurring_tasks(user_id: int) -> None:
    execute(
        """
        UPDATE tasks
        SET is_completed = FALSE, completed_at = NULL
        WHERE user_id = %s
          AND recurrence_hours > 0
          AND is_completed = TRUE
          AND completed_at + (recurrence_hours || ' hours')::interval <= CURRENT_TIMESTAMP
        """,
        (user_id,)
    )
    get_db().commit()


def list_user_tasks(user_id: int) -> list[dict[str, Any]]:
    update_recurring_tasks(user_id)
    return fetch_all(
        """
        SELECT id, note_id, description, urgency, recurrence_hours, is_completed, completed_at, created_at
        FROM tasks
        WHERE user_id = %s
          AND (
              is_completed = FALSE
              OR recurrence_hours > 0
              OR completed_at > CURRENT_TIMESTAMP - interval '16 hours'
          )
        ORDER BY
            is_completed ASC,
            CASE urgency
                WHEN 'alta' THEN 1
                WHEN 'media' THEN 2
                WHEN 'baixa' THEN 3
                ELSE 4
            END,
            created_at DESC
        """,
        (user_id,)
    )


def create_task(user_id: int, note_id: int, description: str, urgency: str, recurrence_hours: int) -> dict[str, Any]:
    cursor = execute(
        """
        INSERT INTO tasks (note_id, user_id, description, urgency, recurrence_hours)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, note_id, description, urgency, recurrence_hours, is_completed, completed_at, created_at
        """,
        (note_id, user_id, description, urgency, recurrence_hours)
    )
    row = cursor.fetchone()
    get_db().commit()
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


def toggle_task(user_id: int, task_id: int, is_completed: bool) -> bool:
    if is_completed:
        cursor = execute(
            """
            UPDATE tasks
            SET is_completed = TRUE, completed_at = CURRENT_TIMESTAMP
            WHERE id = %s AND user_id = %s
            """,
            (task_id, user_id)
        )
    else:
        cursor = execute(
            """
            UPDATE tasks
            SET is_completed = FALSE, completed_at = NULL
            WHERE id = %s AND user_id = %s
            """,
            (task_id, user_id)
        )
    get_db().commit()
    return cursor.rowcount > 0


def delete_task(user_id: int, task_id: int) -> bool:
    cursor = execute(
        "DELETE FROM tasks WHERE id = %s AND user_id = %s",
        (task_id, user_id)
    )
    get_db().commit()
    return cursor.rowcount > 0


# --- Evolutions ---
def list_note_evolutions(note_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT id, note_id, content, created_at FROM evolutions WHERE note_id = %s ORDER BY created_at DESC",
        (note_id,)
    )

def add_evolution(note_id: int, content: str) -> int:
    cursor = execute(
        "INSERT INTO evolutions (note_id, content) VALUES (%s, %s) RETURNING id",
        (note_id, content)
    )
    get_db().commit()
    return int(cursor.fetchone()["id"])


# --- Locations ---
def list_locations(user_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT id, name, created_at FROM locations WHERE user_id = %s ORDER BY name ASC",
        (user_id,)
    )

def create_location(user_id: int, name: str) -> int:
    cursor = execute(
        "INSERT INTO locations (user_id, name) VALUES (%s, %s) RETURNING id",
        (user_id, name)
    )
    get_db().commit()
    return int(cursor.fetchone()["id"])

def delete_location(user_id: int, location_id: int) -> None:
    execute("DELETE FROM locations WHERE id = %s AND user_id = %s", (location_id, user_id))
    get_db().commit()


# --- Note Metadata (Patient Status) ---
def update_note_metadata(user_id: int, note_id: int, location_id: int | None = None, status: str | None = None, is_favorite: bool | None = None) -> bool:
    updates = []
    params = []
    if location_id is not None:
        if location_id == 0:  # 0 means remove location
            updates.append("location_id = NULL")
        else:
            updates.append("location_id = %s")
            params.append(location_id)
    if status is not None:
        updates.append("status = %s")
        params.append(status)
    if is_favorite is not None:
        updates.append("is_favorite = %s")
        params.append(is_favorite)
        
    if not updates:
        return False
        
    query = f"UPDATE notes SET {', '.join(updates)} WHERE id = %s AND user_id = %s"
    params.extend([note_id, user_id])
    
    cursor = execute(query, tuple(params))
    get_db().commit()
    return cursor.rowcount > 0


# --- Quick Notes (Rascunho) ---
def get_quick_note(user_id: int) -> str:
    row = fetch_one("SELECT content FROM quick_notes WHERE user_id = %s", (user_id,))
    return row["content"] if row else ""

def save_quick_note(user_id: int, content: str) -> None:
    execute(
        """
        INSERT INTO quick_notes (user_id, content, updated_at) 
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id) 
        DO UPDATE SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, content)
    )
    get_db().commit()

