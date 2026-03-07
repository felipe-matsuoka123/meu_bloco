import os
import re
import sqlite3
import unicodedata
from io import BytesIO
from datetime import date
from functools import lru_cache, wraps
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, send_file, session, url_for
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from google import genai
except ImportError:
    genai = None


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "notes.db")
NAMES_CSV_PATH = Path(BASE_DIR) / "all-pt-br-names.csv"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-for-production")
app.config["DATABASE"] = os.environ.get("DATABASE_PATH", DATABASE_PATH)
app.config["GEMINI_MODEL"] = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exception: Exception | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS note_review_usage (
            note_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (note_id, usage_date),
            FOREIGN KEY (note_id) REFERENCES notes (id)
        )
        """
    )
    migrate_notes_table(db)
    db.commit()


def migrate_notes_table(db: sqlite3.Connection) -> None:
    columns = [row["name"] for row in db.execute("PRAGMA table_info(notes)").fetchall()]
    if "user_id" in columns:
        return

    db.execute("ALTER TABLE notes RENAME TO notes_legacy")
    db.execute(
        """
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """
    )

    legacy_user = db.execute(
        "SELECT id FROM users WHERE username = ?",
        ("legacy",),
    ).fetchone()
    if legacy_user is None:
        cursor = db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("legacy", generate_password_hash(os.environ.get("LEGACY_PASSWORD", "change-this"))),
        )
        legacy_user_id = cursor.lastrowid
    else:
        legacy_user_id = legacy_user["id"]

    db.execute(
        """
        INSERT INTO notes (id, user_id, content, created_at)
        SELECT id, ?, content, created_at
        FROM notes_legacy
        """,
        (legacy_user_id,),
    )
    db.execute("DROP TABLE notes_legacy")


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if not session.get("logged_in") or "user_id" not in session:
            session.clear()
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


@app.before_request
def ensure_database() -> None:
    init_db()


def current_user_id() -> int:
    user_id = session.get("user_id")
    if user_id is None:
        raise KeyError("user_id")
    return int(user_id)


def get_user_notes(user_id: int) -> list[sqlite3.Row]:
    db = get_db()
    return db.execute(
        """
        SELECT id, content, created_at
        FROM notes
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,),
    ).fetchall()


def get_user_note(user_id: int, note_id: int) -> sqlite3.Row | None:
    db = get_db()
    return db.execute(
        """
        SELECT id, content, created_at
        FROM notes
        WHERE user_id = ? AND id = ?
        """,
        (user_id, note_id),
    ).fetchone()


def get_note_map(notes_list: list[sqlite3.Row]) -> dict[int, sqlite3.Row]:
    return {int(note["id"]): note for note in notes_list}


def get_selected_note_id(notes_list: list[sqlite3.Row]) -> int | None:
    available_ids = [int(note["id"]) for note in notes_list]
    stored_id = session.get("assistant_selected_note_id")
    if isinstance(stored_id, int) and stored_id in available_ids:
        return stored_id
    return available_ids[0] if available_ids else None


def set_selected_note_id(selected_id: int | None) -> None:
    if selected_id is None:
        session.pop("assistant_selected_note_id", None)
    else:
        session["assistant_selected_note_id"] = selected_id


def today_key() -> str:
    return date.today().isoformat()


def get_note_review_count(note_id: int) -> int:
    db = get_db()
    row = db.execute(
        """
        SELECT request_count
        FROM note_review_usage
        WHERE note_id = ? AND usage_date = ?
        """,
        (note_id, today_key()),
    ).fetchone()
    return int(row["request_count"]) if row else 0


def increment_note_review_count(note_id: int) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO note_review_usage (note_id, usage_date, request_count)
        VALUES (?, ?, 1)
        ON CONFLICT(note_id, usage_date)
        DO UPDATE SET request_count = request_count + 1
        """,
        (note_id, today_key()),
    )
    db.commit()


def build_notes_context(notes_list: list[sqlite3.Row]) -> str:
    if not notes_list:
        return "O usuario ainda nao possui anotacoes."

    formatted_notes = []
    for note in notes_list:
        safe_content, _ = redact_note_content(note["content"])
        formatted_notes.append(f'Anotacao #{note["id"]}: {safe_content}')
    return "\n".join(formatted_notes)


def ask_gemini_for_medical_review(notes_list: list[sqlite3.Row]) -> str:
    if genai is None:
        raise RuntimeError("sdk_missing")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("api_key_missing")

    client = genai.Client(api_key=api_key)
    notes_context = build_notes_context(notes_list)
    prompt = f"""
Voce e um medico experiente revisando o historico clinico de um paciente.
Analise apenas as anotacoes fornecidas.
Escreva em portugues do Brasil.
Seja direto, objetivo e pratico.
Nao use markdown, negrito, tabelas ou introducoes longas.
Nao invente fatos ausentes.
Nao use data, horario de criacao ou ordem das anotacoes para inferir linha do tempo.
Considere apenas o conteudo textual de cada anotacao.
As partes marcadas como [REMOVIDO] sao resultado de anonimização e nao devem ser tratadas como problema do texto.
Nao comente sobre [REMOVIDO], nao liste isso como falha e nao faca perguntas por causa dessas marcacoes.
Quando algo estiver faltando, diga explicitamente o que falta e por que importa.
Quando houver mencao a avaliacao, exame, conduta ou diagnostico sem detalhes suficientes,
aponte isso como pergunta em aberto (Ex: Voce verificou a evolucao da neurologia?, Verificou foi o resultado do exame comentado?, Por que a paciente esta com esse dispositivo (sonda, acesso diferente) ?).
Seja efetivo: destaque apenas as deficiencias mais importantes.
Ignore problemas menores de redacao que nao mudem a compreensao clinica.
Prefira poucos itens, com maior impacto pratico.
Mantenha a resposta curta. Use no maximo 3 itens por secao.

Organize a resposta exatamente nesta estrutura:
Pontos para melhorar a clareza:
- ...

Informacoes faltando:
- ...

Perguntas em aberto:
- ...

Se nao houver itens em alguma secao, escreva "- Nenhum ponto relevante."

Anotacoes do usuario:
{notes_context}
""".strip()

    response = client.models.generate_content(
        model=app.config["GEMINI_MODEL"],
        contents=prompt,
    )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("empty_response")
    return clean_assistant_text(text)


def clean_assistant_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^[\-\*\u2022]\s+", "- ", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def build_note_pdf(note: sqlite3.Row) -> BytesIO:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left_margin = 50
    top_margin = height - 50
    max_width = width - (left_margin * 2)
    line_height = 16
    y_position = top_margin

    def write_line(text: str, font_name: str = "Helvetica", font_size: int = 11) -> None:
        nonlocal y_position
        if y_position < 60:
            pdf.showPage()
            y_position = top_margin
        pdf.setFont(font_name, font_size)
        pdf.drawString(left_margin, y_position, text)
        y_position -= line_height

    def wrap_text(text: str, font_name: str = "Helvetica", font_size: int = 11) -> list[str]:
        words = text.split()
        if not words:
            return [""]

        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if stringWidth(trial, font_name, font_size) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    write_line(f"Anotacao #{note['id']}", "Helvetica-Bold", 14)
    write_line(f"Criado em: {note['created_at']}", "Helvetica", 10)
    y_position -= 8

    for paragraph in note["content"].splitlines() or [""]:
        for line in wrap_text(paragraph):
            write_line(line)
        y_position -= 4

    pdf.save()
    buffer.seek(0)
    return buffer


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return normalized.upper().strip()


@lru_cache(maxsize=1)
def load_redaction_names() -> set[str]:
    if not NAMES_CSV_PATH.exists():
        return set()

    names = set()
    with NAMES_CSV_PATH.open(encoding="utf-8", newline="") as csv_file:
        for line in csv_file:
            parts = line.strip().split(",", 1)
            if len(parts) != 2:
                continue
            raw_name = parts[1].strip()
            if not raw_name or raw_name == "nomes-pt-br":
                continue
            normalized_name = normalize_name(raw_name)
            if len(normalized_name) < 4:
                continue
            names.add(normalized_name)
    return names


def redact_note_content(content: str) -> tuple[str, bool]:
    names = load_redaction_names()
    if not names:
        return content, False

    has_redaction = False

    def replace_match(match: re.Match[str]) -> str:
        nonlocal has_redaction
        token = match.group(0)
        if normalize_name(token) in names:
            has_redaction = True
            return "[REMOVIDO]"
        return token

    redacted = re.sub(r"[A-Za-zÀ-ÿ]+", replace_match, content)
    return redacted, has_redaction


@app.route("/", methods=["GET"])
def index():
    if session.get("logged_in"):
        return redirect(url_for("notes"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["logged_in"] = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("notes"))

        flash("Usuario ou senha invalidos.", "error")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        db = get_db()

        if len(username) < 3:
            flash("O nome de usuario precisa ter pelo menos 3 caracteres.", "error")
        elif len(password) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "error")
        elif password != confirm_password:
            flash("As senhas nao conferem.", "error")
        else:
            existing_user = db.execute(
                "SELECT id FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if existing_user is not None:
                flash("Esse nome de usuario ja esta em uso.", "error")
            else:
                db.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, generate_password_hash(password)),
                )
                db.commit()
                flash("Conta criada com sucesso. Agora voce pode entrar.", "success")
                return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/notes", methods=["GET", "POST"])
@login_required
def notes():
    review_output = session.pop("review_output", None)
    notes_list = get_user_notes(current_user_id())
    note_map = get_note_map(notes_list)
    selected_note_id = get_selected_note_id(notes_list)

    if request.method == "POST":
        form_name = request.form.get("form_name")

        if form_name == "review":
            selected_note_raw = request.form.get("selected_note_id", "").strip()
            selected_note_id = (
                int(selected_note_raw)
                if selected_note_raw.isdigit() and int(selected_note_raw) in note_map
                else None
            )
            set_selected_note_id(selected_note_id)

            if selected_note_id is None:
                flash("Selecione uma anotacao para usar como contexto.", "error")
            elif get_note_review_count(selected_note_id) >= 4:
                flash("Essa anotacao ja atingiu o limite diario de 4 analises.", "error")
            else:
                selected_notes = [note_map[selected_note_id]]
                try:
                    review_output = ask_gemini_for_medical_review(selected_notes)
                    increment_note_review_count(selected_note_id)
                    session["review_output"] = review_output
                    return redirect(url_for("notes"))
                except RuntimeError as exc:
                    if str(exc) == "sdk_missing":
                        flash("A biblioteca google-genai nao esta instalada no ambiente.", "error")
                    elif str(exc) == "api_key_missing":
                        flash("Defina a variavel GEMINI_API_KEY para ativar o assistente.", "error")
                    elif str(exc) == "empty_response":
                        flash("O Gemini nao retornou texto nesta tentativa.", "error")
                    else:
                        flash("Nao foi possivel consultar o Gemini.", "error")
                except Exception:
                    flash("Ocorreu um erro ao consultar o Gemini.", "error")
        elif form_name == "clear_review":
            session.pop("review_output", None)
            review_output = None
            flash("Analise removida.", "success")
        else:
            content = request.form.get("content", "").strip()
            if not content:
                flash("A anotacao nao pode ficar vazia.", "error")
            else:
                db = get_db()
                db.execute(
                    "INSERT INTO notes (user_id, content) VALUES (?, ?)",
                    (current_user_id(), content),
                )
                db.commit()
                flash("Anotacao salva.", "success")
                return redirect(url_for("notes"))

    selected_note_id = get_selected_note_id(notes_list)
    review_counts = {int(note["id"]): get_note_review_count(int(note["id"])) for note in notes_list}
    return render_template(
        "notes.html",
        notes=notes_list,
        model_name=app.config["GEMINI_MODEL"],
        selected_note_id=selected_note_id,
        review_counts=review_counts,
        review_output=review_output,
    )


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id: int):
    db = get_db()
    db.execute(
        "DELETE FROM notes WHERE id = ? AND user_id = ?",
        (note_id, current_user_id()),
    )
    db.commit()
    flash("Anotacao removida.", "success")
    return redirect(url_for("notes"))


@app.route("/notes/<int:note_id>/pdf", methods=["GET"])
@login_required
def download_note_pdf(note_id: int):
    note = get_user_note(current_user_id(), note_id)
    if note is None:
        flash("Anotacao nao encontrada.", "error")
        return redirect(url_for("notes"))

    return send_file(
        build_note_pdf(note),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"anotacao-{note_id}.pdf",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
