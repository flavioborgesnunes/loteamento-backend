import os
from django.conf import settings


def load_rag_context(max_chars: int = 12000) -> str:
    """
    Carrega conteúdo de RAG de uma pasta configurada em
    settings.IAPARCELAMENTO_RAG_DIR.

    - Lê arquivos .txt e .md
    - Concatena até max_chars
    - Se não houver nada, devolve um texto padrão.
    """
    rag_dir = getattr(settings, "IAPARCELAMENTO_RAG_DIR", None)
    if not rag_dir or not os.path.isdir(rag_dir):
        return (
            "Contexto padrão de parcelamento urbano: lotes residenciais "
            "com frente mínima de 10 m, profundidade mínima de 25 m, "
            "ruas locais com largura entre 10 e 14 m, preferência por "
            "quarteirões relativamente regulares, evitando lotes muito "
            "estreitos ou muito profundos."
        )

    parts = []
    total = 0

    for fname in os.listdir(rag_dir):
        if not (fname.lower().endswith(".txt") or fname.lower().endswith(".md")):
            continue
        fpath = os.path.join(rag_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            continue
        if not txt:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(txt) > remaining:
            txt = txt[:remaining]
        parts.append(f"# Documento: {fname}\n\n{txt.strip()}\n\n")
        total += len(txt)
    if not parts:
        return (
            "Contexto padrão de parcelamento urbano: lotes residenciais "
            "com frente mínima de 10 m, profundidade mínima de 25 m, "
            "ruas locais com largura entre 10 e 14 m, preferência por "
            "quarteirões regulares."
        )
    return "\n".join(parts)
