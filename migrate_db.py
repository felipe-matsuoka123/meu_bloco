import os
from pathlib import Path
from datetime import datetime
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"

# Load environment variables
load_dotenv(BASE_DIR / ".env")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/meu_bloco")

def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def run_migration():
    print(f"Connecting to database: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")
    db = get_db()
    
    # 1. Apply schema.sql
    print("Applying schema...")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = [statement.strip() for statement in schema.split(";") if statement.strip()]
    for statement in statements:
        try:
            db.execute(statement)
        except Exception as e:
            print(f"Notice during schema apply: {e}")
            db.rollback()
            continue
    db.commit()
    print("Schema applied.")
    
    # 2. Migrate existing notes content to evolutions table
    print("Migrating existing note contents to evolutions...")
    cursor = db.execute("SELECT id, content, created_at FROM notes")
    notes = cursor.fetchall()
    
    for note in notes:
        existing = db.execute("SELECT id FROM evolutions WHERE note_id = %s LIMIT 1", (note["id"],)).fetchone()
        if not existing and note["content"].strip():
            db.execute(
                "INSERT INTO evolutions (note_id, content, created_at) VALUES (%s, %s, %s)",
                (note["id"], note["content"], note["created_at"])
            )
            
    db.commit()
    print("Migration complete!")
    db.close()

if __name__ == "__main__":
    run_migration()
