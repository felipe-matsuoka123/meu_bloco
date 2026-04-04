def build_medical_review_prompt(notes_context: str) -> str:
    return f"""
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


def build_sbar_prompt_fast(notes_context, current_date, expected_note_ids, note_count):
    return f"""
Voce e um medico organizando uma passagem de plantao.

Data: {current_date}

Extraia apenas informacoes relevantes para conduta atual a partir das anotacoes.

Regras:
- Nao inventar informacoes
- Priorizar dados recentes e clinicamente relevantes
- Nao repetir informacao entre campos
- Ignorar exames sem impacto na conduta
- Resposta deve ser objetiva e util para o proximo plantonista

Formato de saida:
- JSON valido estrito
- Lista com {note_count} itens
- Use exatamente estes note_id (mesma ordem): [{expected_note_ids}]
- Sem texto fora do JSON

Estrutura:
{{
  "note_id": 123,
  "paciente": "...",
  "hd": "...",
  "status_hoje": "...",
  "riscos_pendencias": "...",
  "plano": "..."
}}

Campos:

Paciente:
Identificacao clinica relevante (idade, comorbidades, MUC, DIH, etc). Sem nomes.

HD:
Diagnostico principal + contexto clinico essencial + eventos relevantes.

Status hoje:
Estado atual, exame fisico alterado, suportes, exames recentes relevantes.

Riscos / Pendencias:
Problemas nao resolvidos, exames/pareceres pendentes, risco de deterioracao.

Plano:
Condutas atuais e proximos passos.

Anotacoes:
{notes_context}
""".strip()


def build_sbar_prompt(notes_context, current_date, expected_note_ids, note_count):
    return build_sbar_prompt_fast(notes_context, current_date, expected_note_ids, note_count)
