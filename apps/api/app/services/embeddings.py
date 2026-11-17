"""Optional semantic embeddings via a local Ollama model (default: nomic-embed-text, 768-d).

If Ollama isn't running or the model isn't pulled, embed_item() returns None: the report is
still saved (embedding NULL) and matching renormalises the remaining weights.
"""

from __future__ import annotations

import logging

from app.agent.llm import OllamaClient, OllamaError
from app.config import settings

log = logging.getLogger(__name__)

SCHEMA_DIM = 768  # must equal vector(768) in db/schema.sql


def build_embedding_text(category: str, item_name: str, color: str | None, brand: str | None,
                         description: str) -> str:
    """Built the same way every time (§14)."""
    return (f"category: {category} | item: {item_name} | color: {color or 'unknown'} | "
            f"brand: {brand or 'unknown'} | details: {description}")


def embed_item(category: str, item_name: str, color: str | None, brand: str | None,
               description: str, client: OllamaClient | None = None) -> list[float] | None:
    if settings.EMBEDDING_PROVIDER.lower() != "ollama":
        return None
    text = build_embedding_text(category, item_name, color, brand, description)
    try:
        vector = (client or OllamaClient(timeout=20)).embed(text, settings.OLLAMA_EMBED_MODEL)
    except OllamaError as exc:
        log.info("Embedding skipped (%s); matching will use structured signals only.", exc)
        return None
    if len(vector) != SCHEMA_DIM:
        log.warning("Embedding has %d dims but the schema expects %d; skipped.", len(vector), SCHEMA_DIM)
        return None
    return vector


def to_pgvector(vector: list[float] | None) -> str | None:
    """'[0.1,0.2,…]' text literal, cast with ::vector in SQL."""
    if vector is None:
        return None
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
