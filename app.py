import os
import re
import unicodedata
import json
import logging
import signal
import threading
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache, wraps
from io import BytesIO
from pathlib import Path

import bcrypt
import stripe
from flask import Flask, flash, redirect, render_template, request, send_file, send_from_directory, session, url_for
from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash

import db

try:
    from google import genai
except ImportError:
    genai = None


BASE_DIR = Path(__file__).resolve().parent
NAMES_CSV_PATH = BASE_DIR / "all-pt-br-names.csv"
IMAGES_DIR = BASE_DIR / "images"
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
logger = logging.getLogger(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-for-production")
app.config["DATABASE_URL"] = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/meu_bloco",
)
app.config["GEMINI_MODEL"] = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
app.config["GEMINI_TEMPERATURE"] = float(os.environ.get("GEMINI_TEMPERATURE", "0.0"))
app.config["GEMINI_TIMEOUT_SECONDS"] = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", "20"))
app.config["SBAR_BATCH_SIZE"] = int(os.environ.get("SBAR_BATCH_SIZE", "3"))
app.config["SBAR_BATCH_DELAY_SECONDS"] = float(os.environ.get("SBAR_BATCH_DELAY_SECONDS", "1.0"))
app.config["MAX_LOGIN_ATTEMPTS"] = 5
app.config["LOGIN_LOCKOUT_MINUTES"] = 15
app.config["STRIPE_SECRET_KEY"] = os.environ.get("STRIPE_SECRET_KEY", "")
app.config["STRIPE_PRICE_LOOKUP_KEY"] = os.environ.get("STRIPE_PRICE_LOOKUP_KEY", "")
app.config["GIFT_CARD_OVERRIDE_CODE"] = os.environ.get("GIFT_CARD_OVERRIDE_CODE", "felipe")

stripe.api_key = app.config["STRIPE_SECRET_KEY"]
app.teardown_appcontext(db.close_db)


def ensure_database() -> None:
    with app.app_context():
        db.init_db()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if not session.get("logged_in") or "user_id" not in session:
            session.clear()
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def current_user_id() -> int:
    user_id = session.get("user_id")
    if user_id is None:
        raise KeyError("user_id")
    return int(user_id)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def is_bcrypt_hash(password_hash: str) -> bool:
    return password_hash.startswith(("$2a$", "$2b$", "$2y$"))


def verify_password(password_hash: str, password: str) -> bool:
    if is_bcrypt_hash(password_hash):
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    return check_password_hash(password_hash, password)


def lockout_expiration() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=app.config["LOGIN_LOCKOUT_MINUTES"])


def parse_lockout(locked_until: datetime | None) -> datetime | None:
    if locked_until is None:
        return None
    if locked_until.tzinfo is None:
        return locked_until.replace(tzinfo=timezone.utc)
    return locked_until


def stripe_checkout_ready() -> bool:
    return bool(app.config["STRIPE_SECRET_KEY"] and app.config["STRIPE_PRICE_LOOKUP_KEY"])


def absolute_url(endpoint: str, **values: str) -> str:
    return url_for(endpoint, _external=True, **values)


def safe_download_filename(title: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_title).strip("-").lower()
    return slug or fallback


def get_pending_registration() -> dict[str, str] | None:
    pending = session.get("pending_registration")
    if not isinstance(pending, dict):
        return None
    username = pending.get("username")
    password = pending.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    return {"username": username, "password": password}


def gift_card_override_matches(code: str) -> bool:
    configured_code = app.config["GIFT_CARD_OVERRIDE_CODE"].strip()
    return bool(configured_code and code.strip() == configured_code)


def create_user_account(username: str, password: str) -> None:
    db.create_user(username, hash_password(password))


def get_note_map(notes_list: list[dict]) -> dict[int, dict]:
    return {int(note["id"]): note for note in notes_list}


def get_selected_note_id(notes_list: list[dict]) -> int | None:
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


def get_active_tab() -> str:
    tab = request.args.get("tab", "").strip().lower()
    if tab in {"notes", "sbar"}:
        return tab
    return "notes"


def get_selected_sbar_note_ids(notes_list: list[dict]) -> list[int]:
    available_ids = {int(note["id"]) for note in notes_list}
    stored_ids = session.get("sbar_selected_note_ids", [])
    if not isinstance(stored_ids, list):
        return []
    return [note_id for note_id in stored_ids if isinstance(note_id, int) and note_id in available_ids]


def set_selected_sbar_note_ids(selected_ids: list[int]) -> None:
    session["sbar_selected_note_ids"] = selected_ids


def get_saved_sbar(user_id: int) -> tuple[list[int], list[dict[str, str | int]] | None]:
    saved_sbar = db.get_user_saved_sbar(user_id)
    if saved_sbar is None:
        return [], None

    raw_selected_ids = saved_sbar.get("selected_note_ids")
    selected_note_ids = (
        [int(note_id) for note_id in raw_selected_ids if isinstance(note_id, int)]
        if isinstance(raw_selected_ids, list)
        else []
    )

    raw_rows = saved_sbar.get("rows")
    if not isinstance(raw_rows, list):
        return selected_note_ids, None

    rows: list[dict[str, str | int]] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        note_id = item.get("note_id")
        if not isinstance(note_id, int):
            continue
        rows.append(
            {
                "note_id": note_id,
                "paciente": clean_assistant_text(str(item.get("paciente", ""))),
                "hd": clean_assistant_text(str(item.get("hd", "Nao informado."))) or "Nao informado.",
                "status_hoje": clean_assistant_text(str(item.get("status_hoje", "Nao informado."))) or "Nao informado.",
                "riscos_pendencias": clean_assistant_text(str(item.get("riscos_pendencias", "Nao informado."))) or "Nao informado.",
                "plano": clean_assistant_text(str(item.get("plano", "Nao informado."))) or "Nao informado.",
            }
        )

    return selected_note_ids, rows or None


def get_editing_note_id(notes_list: list[dict]) -> int | None:
    available_ids = {int(note["id"]) for note in notes_list}
    editing_raw = request.args.get("edit", "").strip()
    if editing_raw.isdigit() and int(editing_raw) in available_ids:
        return int(editing_raw)
    return None


def today_key() -> date:
    return date.today()


def build_notes_context(notes_list: list[dict]) -> str:
    if not notes_list:
        return "O usuario ainda nao possui anotacoes."

    formatted_notes = []
    for note in notes_list:
        safe_content, _ = redact_note_content(note["content"])
        title = clean_assistant_text(str(note.get("title", "Sem título"))) or "Sem título"
        formatted_notes.append(f'Titulo: {title}\nAnotacao #{note["id"]}: {safe_content}')
    return "\n".join(formatted_notes)


class GeminiRequestTimeoutError(TimeoutError):
    pass


def _handle_gemini_alarm(_signum, _frame) -> None:
    raise GeminiRequestTimeoutError


def classify_gemini_error(exc: Exception) -> str:
    exc_type_name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "timeout" in exc_type_name or "timeout" in message or "timed out" in message:
        return "request_timeout"
    if "api key" in message or "api_key" in message or "401" in message or "403" in message:
        return "auth_failed"
    if "ssl" in message or "certificate" in message:
        return "ssl_failed"
    if "dns" in message or "name or service not known" in message or "temporary failure in name resolution" in message:
        return "dns_failed"
    return "request_failed"


def raise_gemini_request_error(exc: Exception) -> None:
    error_code = classify_gemini_error(exc)
    logger.exception("Gemini request failed (%s): %s", error_code, exc)
    raise RuntimeError(error_code) from exc


def generate_gemini_content(client, prompt: str):
    timeout_seconds = max(1, int(app.config["GEMINI_TIMEOUT_SECONDS"]))
    if threading.current_thread() is not threading.main_thread():
        try:
            return client.models.generate_content(
                model=app.config["GEMINI_MODEL"],
                contents=prompt,
                config={"temperature": app.config["GEMINI_TEMPERATURE"]},
            )
        except Exception as exc:
            raise_gemini_request_error(exc)

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handle_gemini_alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return client.models.generate_content(
            model=app.config["GEMINI_MODEL"],
            contents=prompt,
            config={"temperature": app.config["GEMINI_TEMPERATURE"]},
        )
    except GeminiRequestTimeoutError:
        raise RuntimeError("request_timeout") from None
    except Exception as exc:
        raise_gemini_request_error(exc)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def ask_gemini_for_medical_review(notes_list: list[dict]) -> str:
    if genai is None:
        raise RuntimeError("sdk_missing")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("api_key_missing")

    client = genai.Client(api_key=api_key)
    notes_context = build_notes_context(notes_list)
    prompt = f"""
Voce e um medico experiente revisando evolucoes clinicas.

Analise apenas as anotacoes fornecidas.

Escreva em portugues do Brasil.
Seja direto, objetivo e pratico.
Use frases curtas e linguagem simples.

REGRAS GERAIS:
- Nao invente fatos ausentes
- Nao inferir linha do tempo por ordem das anotacoes
- Ignore [REMOVIDO]
- Nao comentar sobre anonimização
- Priorize impacto clinico real
- Foque apenas no que muda conduta ou risco

REGRAS DE OBJETIVIDADE:
- Cada item deve ter no maximo 1 frase curta
- Evite explicacoes fisiopatologicas
- Evite detalhamento excessivo
- Prefira: problema -> acao
- Se puder simplificar, simplifique

QUANDO IDENTIFICAR PROBLEMAS:
- Liste apenas os mais relevantes (maximo 3 por secao)
- Evite generalidades
- Foque em falhas que impactam decisao

INFORMACOES FALTANDO:
- Diga exatamente o que falta
- Use termos amplos quando possivel (ex: “avaliacao de anemia”)

PERGUNTAS EM ABERTO:
- Devem ser objetivas e acionaveis
- Relacionadas a conduta, diagnostico ou risco

SCORES:
- Sugira apenas se claramente aplicaveis
- Maximo 2 scores
- Nao sugerir scores irrelevantes

SUGESTAO DE ESTUDO:
- Maximo 5 palavras
- Tema geral e pratico
- Nao detalhar

REGRA FINAL:
- A resposta deve ser lida em menos de 20 segundos
- Se estiver longa, reduza pela metade mantendo o essencial

Organize a resposta exatamente assim:

Pontos para melhorar a clareza:
- ...

Informacoes faltando:
- ...

Perguntas em aberto:
- ...

Scores sugeridos:
- ...

Sugestao de estudo:
- ...

Se nao houver itens em alguma secao, escreva:
- Nenhum ponto relevante.

Anotacoes do usuario:
{notes_context}
""".strip()

    response = generate_gemini_content(client, prompt)
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("empty_response")
    return clean_assistant_text(text)


def build_sbar_context(notes_list: list[dict]) -> str:
    if not notes_list:
        return "Nenhuma anotacao fornecida."

    formatted_notes = []
    for note in notes_list:
        safe_content, _ = redact_note_content(note["content"])
        formatted_notes.append(
            "\n".join(
                [
                    f"Titulo: {clean_assistant_text(str(note.get('title', 'Sem título'))) or 'Sem título'}",
                    f"Anotacao #{note['id']}",
                    f"Criado em: {note['created_at']}",
                    safe_content,
                ]
            )
        )
    return "\n\n".join(formatted_notes)


def extract_json_payload(text: str) -> str:
    candidate = text.strip()
    fenced_match = re.search(r"```(?:json)?\s*(.*?)```", candidate, flags=re.DOTALL)
    if fenced_match:
        candidate = fenced_match.group(1).strip()

    list_start = candidate.find("[")
    list_end = candidate.rfind("]")
    if list_start != -1 and list_end != -1 and list_end > list_start:
        return candidate[list_start : list_end + 1]

    object_start = candidate.find("{")
    object_end = candidate.rfind("}")
    if object_start != -1 and object_end != -1 and object_end > object_start:
        return candidate[object_start : object_end + 1]
    raise RuntimeError("invalid_json")


def build_sbar_prompt(notes_list: list[dict], current_date: str) -> str:
    expected_note_ids = ", ".join(str(int(note["id"])) for note in notes_list)
    prompt = f"""
Voce e um medico organizando uma passagem de caso para plantao.

A partir da evolucao clinica, extraia apenas informacoes relevantes para tomada de decisao.

Data de hoje: {current_date}

IMPORTANTE:
O texto pode conter excesso de informacoes, multiplos exames e eventos ao longo do tempo.
Sua tarefa e filtrar e organizar apenas o que impacta a conduta atual.
Nao adicione nenhuma informacao que nao esteja escrita no prontuario original
Prefira uma passagem util para o proximo plantonista, mesmo que fique um pouco mais detalhada.

Analise apenas as anotacoes fornecidas.
Escreva em portugues do Brasil.
Retorne exatamente um JSON valido contendo uma lista.
Cada item da lista deve corresponder a uma anotacao de entrada.
Retorne exatamente {len(notes_list)} itens.
Use obrigatoriamente estes note_id, sem alterar ordem nem omitir nenhum: [{expected_note_ids}]
Nao inclua markdown, explicacoes, comentarios ou texto fora do JSON.
Use esta estrutura exata em cada item:
{{
  "note_id": 123,
  "paciente": "...",
  "hd": "...",
  "status_hoje": "...",
  "riscos_pendencias": "...",
  "plano": "..."
}}

Estruture a saida nos seguintes campos:

Paciente:
HD:
Status hoje:
Riscos / Pendencias:
Plano:

DEFINICOES:

Paciente:
- Preencha com dados de identificacao clinicamente uteis do paciente
- Inclua tudo que ajude a identificar o caso, exceto nomes
- Pode incluir sexo, idade, leito, enfermaria, numero do prontuario, procedencia ou servico, se estiverem escritos
- Se estiver disponivel, priorize: idade, comorbidades relevantes, medicacoes de uso continuo, internacoes previas, alergias, historico familiar/social relevante e data de internacao hospitalar
- Sempre sinalize cada dado com rotulo curto e claro, por exemplo: "Idade: 67a. MUC: anlodipino, metformina. DIH: 14/03."
- Use abreviacoes clinicas usuais quando fizer sentido, incluindo "MUC" e "DIH"
- Quando listar comorbidades, use explicitamente o rotulo "Comorbidades:"
- Nao jogue informacoes soltas sem identificar o que cada uma representa
- Nao invente identificadores ausentes
- Nao repetir aqui informacoes que ja serao melhor descritas em HD, Status hoje, Riscos / Pendencias ou Plano
- Esta coluna deve servir para identificar o paciente/caso, nao para resumir a evolucao clinica
- Leito/localizacao podem ser incluidos aqui quando ajudarem na identificacao do caso
- Se houver antecedentes pessoais relevantes e estaveis, prefira mantê-los aqui: AP, MUC, alergias, internacoes previas, historico familiar/social relevante e DIH

HD:
- Diagnostico principal
- Complicacoes associadas relacionadas ao diagnóstico central
- Contexto clinico essencial (ex: tempo de internacao, eventos relevantes)
- Incluir temporalidade quando disponivel (ex: D5 de internacao, evento em 10/03)
- Incluir procedimentos relevantes, culturas positivas, cirurgias ou eventos marcantes quando mudarem entendimento do caso
- Nao incluir lista extensa de exames antigos, mas manter achados objetivos essenciais
- Incluir HMA concisa e cronologica quando ela for necessaria para entender por que o paciente internou
- Resumir a admissao no pronto socorro quando isso for relevante para o caso atual
- Se relevante, incluir na HD: exame fisico de admissao, exames laboratoriais/de imagem de admissao, diagnosticos de admissao e plano inicial
- Se houver intercorrencias relevantes durante a internacao, incorporar a linha geral dessas mudancas aqui

Status hoje:
- Estado atual do paciente (momento da avaliacao mais recente)
- Exame Físico atual (focar nas alteracoes)
- Nivel de consciencia, estabilidade hemodinamica e respiratoria
- Medicacoes importantes em uso (ex: antibiotico com dia de tratamento, anticoagulacao, antiagregantes)
- Dispositivos e suportes atuais quando relevantes (ex: O2, VM, DVA, dreno, SNE, diurese, acesso)
- Exames recentes que mudam a conduta atual, com valor resumido quando houver (ex: Hb 6,8 em 28/03; Cr 2,1 hoje)
- Incluir referencia temporal se relevante (ex: "hoje", "ultimas 24h")
- NAO incluir dados normais sem impacto
- Este campo representa a evolucao do dia e deve responder a conduta do dia anterior
- Incluir sinais de resposta ao tratamento, sinais de melhora/piora clinica e mudancas importantes nas ultimas 24h
- Se alterados ou relevantes, incluir sinais vitais, diurese, evacuacao e outros marcadores objetivos do dia
- Incluir exame fisico objetivo atual e exames complementares relevantes do dia

Riscos / Pendencias:
- Problemas ainda nao resolvidos
- Avaliacoes pendentes (ex: aguardando parecer)
- Riscos reais de deterioracao
- Incluir contexto temporal quando aplicavel (ex: "desde 21/03", "aguardando desde admissao")
- Quando incluir exames, citar data
- Incluir o que precisa ser vigiado no plantao e quais resultados ainda podem mudar conduta
- Incluir avaliacoes complementares importantes pendentes, como pareceres de especialidades e exames que ainda podem redefinir a conduta
- Se houve intercorrencias na internacao que ainda tenham impacto, destacar aqui como risco ou pendencia ativa

Plano:
- Condutas principais (resumidas)
- Incluir acoes futuras relevantes (ex: na alta, apos avaliacao)
- Incluir proximo passo pratico e gatilhos de reavaliacao quando estiverem descritos
- Nao listar checklist longo, mas deixar claro o rumo da conduta
- Incluir as acoes imediatas e pontuais do plantao atual
- Incluir tambem o plano terapeutico de medio/longo prazo, incluindo estrategia de enfermaria e planejamento de alta se estiverem descritos

REGRAS CRITICAS:
- Nao repetir informacao entre campos!
- NAO copiar a evolucao original
- Resumir com densidade informativa alta
- NAO listar exames irrelevantes ou antigos
- Sempre priorizar informacao recente sobre antiga
- Sempre explicitar estabilidade ou instabilidade do paciente
- NAO inventar informacoes ausentes
- Se houver poucos dados, seja breve; se houver dados decisivos, preserve-os
- Prefira frases curtas, mas cada campo deve ficar completo o suficiente para orientar o plantao

REGRAS DE TEMPORALIDADE (ESSENCIAL):
- Sempre que houver datas, utilize-as para organizar o raciocinio clinico
- Priorize eventos mais recentes
- Diferencie claramente:
  -> evento passado relevante
  -> estado atual
  -> plano futuro

HEURISTICAS DE FILTRAGEM:
- Priorize: risco > conduta > estado atual > historico
- Ignore exames que nao mudam conduta atual
- Se multiplos exames: usar apenas o que muda decisao
- Conduta longa -> resumir em intencao clinica

DETALHES QUE VALEM A PENA MANTER:
- Data de internacao, D de tratamento, antibiotico em curso e dia do esquema
- Mudancas recentes importantes nas ultimas 24-72h
- Exames ou imagens anormais que sustentam a decisao atual
- Parecer pendente ou procedimento programado
- Critério de alta, transferência ou necessidade de reabordagem, se estiver descrito
- Leito/localizacao quando ajudarem a identificar o paciente
- Dados relevantes da admissao no pronto socorro quando ainda explicarem o estado atual
- Intercorrencias importantes desde a admissao

Antes de responder, identifique mentalmente:
1. principal problema ativo
2. maior risco atual
3. decisao mais importante do plantao
4. o que pertence a identificacao, ao historico do caso, ao estado atual, ao risco pendente e ao plano

Anotacoes:
{build_sbar_context(notes_list)}
""".strip()
    return prompt


def parse_sbar_item(item: dict, note: dict) -> dict[str, str | int]:
    raw_note_id = item.get("note_id")
    if isinstance(raw_note_id, int):
        note_id = raw_note_id
    elif isinstance(raw_note_id, str) and raw_note_id.strip().isdigit():
        note_id = int(raw_note_id.strip())
    else:
        note_id = None
    if note_id != int(note["id"]):
        raise RuntimeError("invalid_json")

    return {
        "note_id": note_id,
        "paciente": clean_assistant_text(str(item.get("paciente", ""))),
        "hd": clean_assistant_text(str(item.get("hd", "Nao informado."))) or "Nao informado.",
        "status_hoje": clean_assistant_text(str(item.get("status_hoje", "Nao informado."))) or "Nao informado.",
        "riscos_pendencias": clean_assistant_text(str(item.get("riscos_pendencias", "Nao informado."))) or "Nao informado.",
        "plano": clean_assistant_text(str(item.get("plano", "Nao informado."))) or "Nao informado.",
    }


def parse_sbar_response_for_notes(parsed: object, notes_list: list[dict]) -> list[dict[str, str | int]]:
    if isinstance(parsed, dict):
        for key in ("rows", "items", "data", "output", "result"):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                parsed = candidate
                break

    if isinstance(parsed, dict):
        if len(notes_list) != 1:
            raise RuntimeError("invalid_json")
        return [parse_sbar_item(parsed, notes_list[0])]

    if not isinstance(parsed, list) or len(parsed) != len(notes_list):
        raise RuntimeError("invalid_json")

    parsed_by_note_id: dict[int, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            raise RuntimeError("invalid_json")

        raw_note_id = item.get("note_id")
        if isinstance(raw_note_id, int):
            note_id = raw_note_id
        elif isinstance(raw_note_id, str) and raw_note_id.strip().isdigit():
            note_id = int(raw_note_id.strip())
        else:
            raise RuntimeError("invalid_json")

        parsed_by_note_id[note_id] = item

    if len(parsed_by_note_id) != len(notes_list):
        raise RuntimeError("invalid_json")

    rows: list[dict[str, str | int]] = []
    for note in notes_list:
        item = parsed_by_note_id.get(int(note["id"]))
        if item is None:
            raise RuntimeError("invalid_json")
        rows.append(parse_sbar_item(item, note))
    return rows


def ask_gemini_for_sbar_batch_rows(client, notes_list: list[dict], current_date: str) -> list[dict[str, str | int]]:
    prompt = build_sbar_prompt(notes_list, current_date)
    response = generate_gemini_content(client, prompt)
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("empty_response")

    try:
        parsed = json.loads(extract_json_payload(text))
    except (json.JSONDecodeError, RuntimeError):
        raise RuntimeError("invalid_json") from None

    return parse_sbar_response_for_notes(parsed, notes_list)


def ask_gemini_for_sbar_rows(notes_list: list[dict]) -> list[dict[str, str | int]]:
    if genai is None:
        raise RuntimeError("sdk_missing")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("api_key_missing")

    client = genai.Client(api_key=api_key)
    current_date = date.today().isoformat()
    batch_size = max(1, int(app.config["SBAR_BATCH_SIZE"]))
    batch_delay_seconds = max(0.0, float(app.config["SBAR_BATCH_DELAY_SECONDS"]))
    rows: list[dict[str, str | int]] = []
    for index in range(0, len(notes_list), batch_size):
        batch_notes = notes_list[index : index + batch_size]
        rows.extend(ask_gemini_for_sbar_batch_rows(client, batch_notes, current_date))
        if index + batch_size < len(notes_list) and batch_delay_seconds:
            time.sleep(batch_delay_seconds)
    return rows


def get_sbar_rows_from_form() -> list[dict[str, str | int]]:
    note_ids = request.form.getlist("sbar_note_id")
    pacientes = request.form.getlist("sbar_paciente")
    hds = request.form.getlist("sbar_hd")
    status_hoje = request.form.getlist("sbar_status_hoje")
    riscos_pendencias = request.form.getlist("sbar_riscos_pendencias")
    planos = request.form.getlist("sbar_plano")

    row_count = len(note_ids)
    if not row_count or not all(
        len(values) == row_count
        for values in (pacientes, hds, status_hoje, riscos_pendencias, planos)
    ):
        raise RuntimeError("invalid_sbar_form")

    rows: list[dict[str, str | int]] = []
    for index, note_id_raw in enumerate(note_ids):
        if not note_id_raw.isdigit():
            raise RuntimeError("invalid_sbar_form")

        rows.append(
            {
                "note_id": int(note_id_raw),
                "paciente": clean_assistant_text(pacientes[index]),
                "hd": clean_assistant_text(hds[index]) or "Nao informado.",
                "status_hoje": clean_assistant_text(status_hoje[index]) or "Nao informado.",
                "riscos_pendencias": clean_assistant_text(riscos_pendencias[index]) or "Nao informado.",
                "plano": clean_assistant_text(planos[index]) or "Nao informado.",
            }
        )

    return rows


def get_sbar_rows_from_payload(payload: object) -> list[dict[str, str | int]]:
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("invalid_sbar_form")

    rows: list[dict[str, str | int]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("invalid_sbar_form")

        note_id = item.get("note_id")
        if not isinstance(note_id, int):
            raise RuntimeError("invalid_sbar_form")

        rows.append(
            {
                "note_id": note_id,
                "paciente": clean_assistant_text(str(item.get("paciente", ""))),
                "hd": clean_assistant_text(str(item.get("hd", ""))) or "Nao informado.",
                "status_hoje": clean_assistant_text(str(item.get("status_hoje", ""))) or "Nao informado.",
                "riscos_pendencias": clean_assistant_text(str(item.get("riscos_pendencias", ""))) or "Nao informado.",
                "plano": clean_assistant_text(str(item.get("plano", ""))) or "Nao informado.",
            }
        )

    return rows


def clean_assistant_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^[\-\*\u2022]\s+", "- ", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def build_note_pdf(note: dict) -> BytesIO:
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

    title = clean_assistant_text(str(note.get("title", ""))) or f"Anotacao #{note['id']}"
    write_line(title, "Helvetica-Bold", 14)
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
    return render_template("landing.html")


@app.route("/healthz", methods=["GET"])
def healthz():
    db.fetch_one("SELECT 1 AS ok")
    return {"ok": True}, 200


@app.route("/images/<path:filename>", methods=["GET"])
def image_asset(filename: str):
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.find_user_by_username(username)

        if user:
            locked_until = parse_lockout(user["locked_until"])
            now = datetime.now(timezone.utc)
            if locked_until and locked_until > now:
                flash(
                    "Muitas tentativas invalidas. Tente novamente em 15 minutos.",
                    "error",
                )
                return render_template("login.html")

            if verify_password(user["password_hash"], password):
                db.reset_login_failures(int(user["id"]))
                if not is_bcrypt_hash(user["password_hash"]):
                    db.update_user_password(int(user["id"]), hash_password(password))
                else:
                    db.get_db().commit()
                session["logged_in"] = True
                session["user_id"] = int(user["id"])
                session["username"] = user["username"]
                return redirect(url_for("notes"))

            failed_attempts = int(user["failed_login_attempts"]) + 1
            db.register_failed_login(
                int(user["id"]),
                failed_attempts,
                lockout_expiration() if failed_attempts >= app.config["MAX_LOGIN_ATTEMPTS"] else None,
            )
            db.get_db().commit()

            remaining_attempts = app.config["MAX_LOGIN_ATTEMPTS"] - failed_attempts
            if remaining_attempts > 0:
                flash(
                    f"Usuario ou senha invalidos. Restam {remaining_attempts} tentativa(s).",
                    "error",
                )
            else:
                flash(
                    "Limite de 5 tentativas atingido. Tente novamente em 15 minutos.",
                    "error",
                )
            return render_template("login.html")

        flash("Usuario ou senha invalidos.", "error")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        gift_card_code = request.form.get("gift_card_code", "").strip()

        if len(username) < 3:
            flash("O nome de usuario precisa ter pelo menos 3 caracteres.", "error")
        elif len(password) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "error")
        elif password != confirm_password:
            flash("As senhas nao conferem.", "error")
        else:
            existing_user = db.find_user_by_username(username)
            if existing_user is not None:
                flash("Esse nome de usuario ja esta em uso.", "error")
            elif gift_card_override_matches(gift_card_code):
                create_user_account(username, password)
                session.pop("pending_registration", None)
                flash("Conta criada com codigo de teste. Agora voce pode entrar.", "success")
                return redirect(url_for("login"))
            else:
                session["pending_registration"] = {
                    "username": username,
                    "password": password,
                }
                flash("Finalize o pagamento para concluir a criacao da conta.", "success")
                return redirect(url_for("pricing"))

    return render_template("register.html")


@app.route("/pricing", methods=["GET"])
def pricing():
    pending_registration = get_pending_registration()
    if pending_registration is None:
        flash("Preencha o cadastro antes de seguir para o pagamento.", "error")
        return redirect(url_for("register"))
    return render_template(
        "pricing.html",
        stripe_ready=stripe_checkout_ready(),
        pending_registration=pending_registration,
        stripe_lookup_key=app.config["STRIPE_PRICE_LOOKUP_KEY"],
    )


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    pending_registration = get_pending_registration()
    if pending_registration is None:
        flash("Preencha o cadastro antes de iniciar o checkout.", "error")
        return redirect(url_for("register"))

    lookup_key = request.form.get("lookup_key", "").strip()
    if not stripe_checkout_ready():
        flash("Configure STRIPE_SECRET_KEY e STRIPE_PRICE_LOOKUP_KEY para ativar pagamentos.", "error")
        return redirect(url_for("pricing"))

    if not lookup_key or lookup_key != app.config["STRIPE_PRICE_LOOKUP_KEY"]:
        flash("O plano selecionado e invalido.", "error")
        return redirect(url_for("pricing"))

    try:
        prices = stripe.Price.list(
            lookup_keys=[lookup_key],
            expand=["data.product"],
            limit=1,
        )
        if not prices.data:
            flash("Nenhum preco foi encontrado para esse plano na Stripe.", "error")
            return redirect(url_for("pricing"))

        price = prices.data[0]
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": price.id, "quantity": 1}],
            metadata={"pending_username": pending_registration["username"]},
            success_url=absolute_url("checkout_success") + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=absolute_url("checkout_cancel"),
            locale="pt-BR",
        )
    except Exception:
        flash("Nao foi possivel iniciar o checkout com a Stripe.", "error")
        return redirect(url_for("pricing"))

    return redirect(checkout_session.url, code=303)


@app.route("/checkout/success", methods=["GET"])
def checkout_success():
    session_id = request.args.get("session_id", "")
    checkout_session = None
    pending_registration = get_pending_registration()

    if pending_registration is None:
        flash("Nenhum cadastro pendente foi encontrado.", "error")
        return redirect(url_for("register"))

    if stripe_checkout_ready() and session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
        except Exception:
            checkout_session = None

    if checkout_session is None or checkout_session.payment_status != "paid":
        flash("O pagamento ainda nao foi confirmado.", "error")
        return redirect(url_for("pricing"))

    existing_user = db.find_user_by_username(pending_registration["username"])
    if existing_user is not None:
        session.pop("pending_registration", None)
        flash("Esse nome de usuario ja foi utilizado. Escolha outro para continuar.", "error")
        return redirect(url_for("register"))

    create_user_account(
        pending_registration["username"],
        pending_registration["password"],
    )
    session.pop("pending_registration", None)

    return render_template(
        "checkout_success.html",
        checkout_session=checkout_session,
        created_username=pending_registration["username"],
    )


@app.route("/checkout/cancel", methods=["GET"])
def checkout_cancel():
    if get_pending_registration() is None:
        flash("Nenhum cadastro pendente foi encontrado.", "error")
        return redirect(url_for("register"))
    return render_template("checkout_cancel.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/notes", methods=["GET", "POST"])
@login_required
def notes():
    review_output = session.pop("review_output", None)
    user_id = current_user_id()
    saved_sbar_note_ids, saved_sbar_output = get_saved_sbar(user_id)
    sbar_output = saved_sbar_output
    notes_list = db.list_user_notes(user_id)
    note_map = get_note_map(notes_list)
    selected_note_id = get_selected_note_id(notes_list)
    selected_sbar_note_ids = saved_sbar_note_ids or get_selected_sbar_note_ids(notes_list)
    editing_note_id = get_editing_note_id(notes_list)
    active_tab = get_active_tab()

    if request.method == "POST":
        form_name = request.form.get("form_name")
        active_tab = request.form.get("active_tab", active_tab).strip().lower()
        if active_tab not in {"notes", "sbar"}:
            active_tab = "notes"

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
            elif db.get_note_review_count(selected_note_id, today_key()) >= 4:
                flash("Essa anotacao ja atingiu o limite diario de 4 analises.", "error")
            else:
                selected_notes = [note_map[selected_note_id]]
                try:
                    review_output = ask_gemini_for_medical_review(selected_notes)
                    db.increment_note_review_count(selected_note_id, today_key())
                    session["review_output"] = review_output
                    return redirect(url_for("notes"))
                except RuntimeError as exc:
                    if str(exc) == "sdk_missing":
                        flash("A biblioteca google-genai nao esta instalada no ambiente.", "error")
                    elif str(exc) == "api_key_missing":
                        flash("Defina a variavel GEMINI_API_KEY para ativar o assistente.", "error")
                    elif str(exc) == "request_timeout":
                        flash("O Gemini demorou demais para responder. Tente novamente.", "error")
                    elif str(exc) == "auth_failed":
                        flash("Falha de autenticacao no Gemini. Verifique a GEMINI_API_KEY do ambiente.", "error")
                    elif str(exc) == "ssl_failed":
                        flash("Falha SSL ao conectar com o Gemini no ambiente implantado.", "error")
                    elif str(exc) == "dns_failed":
                        flash("Falha de DNS ao conectar com o Gemini no ambiente implantado.", "error")
                    elif str(exc) == "request_failed":
                        flash("A consulta ao Gemini falhou por erro de rede ou servico.", "error")
                    elif str(exc) == "empty_response":
                        flash("O Gemini nao retornou texto nesta tentativa.", "error")
                    else:
                        flash("Nao foi possivel consultar o Gemini.", "error")
                except Exception:
                    flash("Ocorreu um erro ao consultar o Gemini.", "error")
        elif form_name == "generate_sbar":
            selected_sbar_note_ids = [
                int(note_id)
                for note_id in request.form.getlist("selected_note_ids")
                if note_id.isdigit() and int(note_id) in note_map
            ]
            selected_sbar_note_ids = list(dict.fromkeys(selected_sbar_note_ids))
            set_selected_sbar_note_ids(selected_sbar_note_ids)

            if not selected_sbar_note_ids:
                flash("Selecione pelo menos uma anotacao para montar a tabela SBAR.", "error")
            else:
                selected_notes = [note_map[note_id] for note_id in selected_sbar_note_ids]
                try:
                    sbar_output = ask_gemini_for_sbar_rows(selected_notes)
                    db.save_user_sbar(user_id, selected_sbar_note_ids, sbar_output)
                    flash("Tabela de passagem gerada.", "success")
                    return redirect(url_for("notes", tab="sbar"))
                except RuntimeError as exc:
                    if str(exc) == "sdk_missing":
                        flash("A biblioteca google-genai nao esta instalada no ambiente.", "error")
                    elif str(exc) == "api_key_missing":
                        flash("Defina a variavel GEMINI_API_KEY para ativar o assistente.", "error")
                    elif str(exc) == "request_timeout":
                        flash("O Gemini demorou demais para responder. Tente novamente.", "error")
                    elif str(exc) == "auth_failed":
                        flash("Falha de autenticacao no Gemini. Verifique a GEMINI_API_KEY do ambiente.", "error")
                    elif str(exc) == "ssl_failed":
                        flash("Falha SSL ao conectar com o Gemini no ambiente implantado.", "error")
                    elif str(exc) == "dns_failed":
                        flash("Falha de DNS ao conectar com o Gemini no ambiente implantado.", "error")
                    elif str(exc) == "request_failed":
                        flash("A consulta ao Gemini falhou por erro de rede ou servico.", "error")
                    elif str(exc) == "empty_response":
                        flash("O Gemini nao retornou texto nesta tentativa.", "error")
                    elif str(exc) == "invalid_json":
                        flash("O Gemini retornou um formato invalido para a tabela SBAR.", "error")
                    else:
                        flash("Nao foi possivel consultar o Gemini.", "error")
                except Exception:
                    flash("Ocorreu um erro ao consultar o Gemini.", "error")
        elif form_name == "clear_sbar":
            db.delete_user_sbar(user_id)
            session.pop("sbar_selected_note_ids", None)
            selected_sbar_note_ids = []
            sbar_output = None
            flash("Tabela de passagem removida.", "success")
        elif form_name == "save_sbar":
            try:
                sbar_output = get_sbar_rows_from_form()
                db.save_user_sbar(user_id, selected_sbar_note_ids, sbar_output)
                flash("Tabela de passagem salva.", "success")
            except RuntimeError:
                flash("Nao foi possivel salvar as edicoes da tabela de passagem.", "error")
        elif form_name == "clear_review":
            session.pop("review_output", None)
            review_output = None
            flash("Analise removida.", "success")
        else:
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            if not title:
                flash("A anotacao precisa de um titulo.", "error")
            elif not content:
                flash("A anotacao nao pode ficar vazia.", "error")
            else:
                db.create_note(user_id, title, content)
                session.pop("review_output", None)
                flash("Anotacao salva.", "success")
                return redirect(url_for("notes"))

    selected_note_id = get_selected_note_id(notes_list)
    selected_sbar_note_ids = [
        note_id for note_id in selected_sbar_note_ids if note_id in note_map
    ]
    review_counts = {
        int(note["id"]): db.get_note_review_count(int(note["id"]), today_key())
        for note in notes_list
    }
    return render_template(
        "notes.html",
        notes=notes_list,
        editing_note_id=editing_note_id,
        model_name=app.config["GEMINI_MODEL"],
        active_tab=active_tab,
        selected_note_id=selected_note_id,
        selected_sbar_note_ids=selected_sbar_note_ids,
        review_counts=review_counts,
        review_output=review_output,
        sbar_output=sbar_output,
    )


@app.route("/notes/<int:note_id>/edit", methods=["POST"])
@login_required
def edit_note(note_id: int):
    title = request.form.get("title", "").strip()
    review_output = clean_assistant_text(request.form.get("review_output", ""))
    content = request.form.get("content", "").strip()
    if not title:
        flash("A anotacao precisa de um titulo.", "error")
        return redirect(url_for("notes"))
    if not content:
        flash("A anotacao nao pode ficar vazia.", "error")
        return redirect(url_for("notes", edit=note_id))

    if not db.update_note(current_user_id(), note_id, title, content, review_output):
        flash("Anotacao nao encontrada.", "error")
        return redirect(url_for("notes"))

    session.pop("review_output", None)
    flash("Anotacao atualizada.", "success")
    return redirect(url_for("notes"))


@app.route("/notes/<int:note_id>/autosave", methods=["POST"])
@login_required
def autosave_note(note_id: int):
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    review_output = clean_assistant_text(str(payload.get("review_output", "")))
    content = str(payload.get("content", "")).strip()
    if not title:
        return {"ok": False, "error": "A anotacao precisa de um titulo."}, 400
    if not content:
        return {"ok": False, "error": "A anotacao nao pode ficar vazia."}, 400

    if not db.update_note(current_user_id(), note_id, title, content, review_output):
        return {"ok": False, "error": "Anotacao nao encontrada."}, 404

    session.pop("review_output", None)
    return {"ok": True}, 200


@app.route("/notes/<int:note_id>/review", methods=["POST"])
@login_required
def review_note(note_id: int):
    note = db.get_user_note(current_user_id(), note_id)
    if note is None:
        return {"ok": False, "error": "Anotacao nao encontrada."}, 404

    if db.get_note_review_count(note_id, today_key()) >= 4:
        return {"ok": False, "error": "Essa anotacao ja atingiu o limite diario de 4 analises."}, 400

    try:
        review_output = ask_gemini_for_medical_review([note])
        db.increment_note_review_count(note_id, today_key())
    except RuntimeError as exc:
        if str(exc) == "sdk_missing":
            return {"ok": False, "error": "A biblioteca google-genai nao esta instalada no ambiente."}, 500
        if str(exc) == "api_key_missing":
            return {"ok": False, "error": "Defina a variavel GEMINI_API_KEY para ativar o assistente."}, 500
        if str(exc) == "request_timeout":
            return {"ok": False, "error": "O Gemini demorou demais para responder. Tente novamente."}, 504
        if str(exc) == "auth_failed":
            return {"ok": False, "error": "Falha de autenticacao no Gemini. Verifique a GEMINI_API_KEY do ambiente."}, 502
        if str(exc) == "ssl_failed":
            return {"ok": False, "error": "Falha SSL ao conectar com o Gemini no ambiente implantado."}, 502
        if str(exc) == "dns_failed":
            return {"ok": False, "error": "Falha de DNS ao conectar com o Gemini no ambiente implantado."}, 502
        if str(exc) == "request_failed":
            return {"ok": False, "error": "A consulta ao Gemini falhou por erro de rede ou servico."}, 502
        if str(exc) == "empty_response":
            return {"ok": False, "error": "O Gemini nao retornou texto nesta tentativa."}, 502
        return {"ok": False, "error": "Nao foi possivel consultar o Gemini."}, 502
    except Exception:
        return {"ok": False, "error": "Ocorreu um erro ao consultar o Gemini."}, 500

    return {
        "ok": True,
        "title": clean_assistant_text(str(note.get("title", ""))) or "Sem título",
        "review_output": review_output,
        "review_count": db.get_note_review_count(note_id, today_key()),
    }, 200


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id: int):
    db.delete_note(current_user_id(), note_id)
    session.pop("review_output", None)
    flash("Anotacao removida.", "success")
    return redirect(url_for("notes"))


@app.route("/sbar/autosave", methods=["POST"])
@login_required
def autosave_sbar():
    payload = request.get_json(silent=True) or {}
    selected_note_ids_raw = payload.get("selected_note_ids", [])
    rows_payload = payload.get("rows", [])

    if not isinstance(selected_note_ids_raw, list) or not all(isinstance(note_id, int) for note_id in selected_note_ids_raw):
        return {"ok": False, "error": "Selecao da tabela SBAR invalida."}, 400

    selected_note_ids = list(dict.fromkeys(int(note_id) for note_id in selected_note_ids_raw))

    try:
        rows = get_sbar_rows_from_payload(rows_payload)
    except RuntimeError:
        return {"ok": False, "error": "Nao foi possivel validar a tabela SBAR."}, 400

    if [int(row["note_id"]) for row in rows] != selected_note_ids:
        return {"ok": False, "error": "As linhas da tabela SBAR nao conferem com a selecao atual."}, 400

    user_id = current_user_id()
    note_ids = {int(note["id"]) for note in db.list_user_notes(user_id)}
    if any(note_id not in note_ids for note_id in selected_note_ids):
        return {"ok": False, "error": "A tabela SBAR referencia uma anotacao invalida."}, 400

    db.save_user_sbar(user_id, selected_note_ids, rows)
    return {"ok": True}, 200


@app.route("/notes/<int:note_id>/pdf", methods=["GET"])
@login_required
def download_note_pdf(note_id: int):
    note = db.get_user_note(current_user_id(), note_id)
    if note is None:
        flash("Anotacao nao encontrada.", "error")
        return redirect(url_for("notes"))

    title = clean_assistant_text(str(note.get("title", ""))).strip()
    filename = safe_download_filename(title, f"anotacao-{note_id}")

    return send_file(
        build_note_pdf(note),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{filename}.pdf",
    )


ensure_database()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
