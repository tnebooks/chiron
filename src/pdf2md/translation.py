"""English -> Tamil chapter translation via a local Ollama model.

Translates block-by-block rather than the whole document at once: pure
image references, tables, and math-only lines are passed through
byte-identical (never sent to the model), so a local LLM can never mangle
Markdown/LaTeX/image-link structure while "helpfully" translating prose.
The result is a draft -- a human edits it in the wizard's review step.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_MODEL = os.environ.get("PDF2MD_OLLAMA_MODEL", "llama3.1")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
_REQUEST_TIMEOUT_SECONDS = 120

_IMAGE_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_MATH_SPAN_RE = re.compile(r"\$\$.*?\$\$|\$[^$]*\$", re.DOTALL)
_PROSE_CHAR_RE = re.compile(r"[A-Za-z]")

_TRANSLATE_PROMPT = (
    "Translate the following Markdown block from English to Tamil.\n"
    "Preserve ALL Markdown syntax (headings, lists, emphasis), any LaTeX "
    "delimited by $ or $$, and any image links exactly as-is.\n"
    "Translate only the natural-language prose and headings.\n"
    "Return only the translated block, with no commentary, no preamble, "
    "and no surrounding quotes.\n\n"
    "Block:\n{block}"
)


@dataclass
class TranslationOutcome:
    ok: bool
    ta_markdown: str | None
    error: str | None
    duration_seconds: float


def _split_blocks(markdown: str) -> list[str]:
    return markdown.split("\n\n")


def _is_table_block(block: str) -> bool:
    lines = [line for line in block.splitlines() if line.strip()]
    return bool(lines) and all(
        _TABLE_ROW_RE.match(line) or _TABLE_SEPARATOR_RE.match(line) for line in lines
    )


def _should_translate(block: str) -> bool:
    stripped = block.strip()
    if not stripped:
        return False
    if _IMAGE_ONLY_RE.match(stripped):
        return False
    if _is_table_block(stripped):
        return False
    text_outside_math = _MATH_SPAN_RE.sub("", stripped)
    if not _PROSE_CHAR_RE.search(text_outside_math):
        return False  # pure math (letters only inside $...$)/numbers/punctuation
    return True


def _translate_block(block: str, model: str, host: str) -> str:
    response = requests.post(
        f"{host}/api/generate",
        json={
            "model": model,
            "prompt": _TRANSLATE_PROMPT.format(block=block),
            "stream": False,
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["response"].strip()


def translate_chapter_to_tamil(
    en_markdown: str,
    model: str = DEFAULT_OLLAMA_MODEL,
    host: str = OLLAMA_HOST,
    on_block_done: Callable[[int, int], None] | None = None,
) -> TranslationOutcome:
    """Translate one chapter's English markdown body to a Tamil draft.
    Never raises for expected failures (Ollama not running, timeout, bad
    response shape) -- returns a structured outcome instead.
    """
    start = time.perf_counter()
    blocks = _split_blocks(en_markdown)
    translated_blocks: list[str] = []

    try:
        for i, block in enumerate(blocks):
            if _should_translate(block):
                translated_blocks.append(_translate_block(block, model, host))
            else:
                translated_blocks.append(block)
            if on_block_done is not None:
                on_block_done(i + 1, len(blocks))
    except requests.RequestException as exc:
        duration = time.perf_counter() - start
        logger.info("translate_chapter_to_tamil failed duration_seconds=%.3f error=%s", duration, exc)
        return TranslationOutcome(ok=False, ta_markdown=None, error=str(exc), duration_seconds=duration)
    except (KeyError, ValueError) as exc:  # malformed Ollama response body
        duration = time.perf_counter() - start
        logger.info("translate_chapter_to_tamil bad response duration_seconds=%.3f error=%s", duration, exc)
        return TranslationOutcome(
            ok=False, ta_markdown=None, error=f"Unexpected Ollama response: {exc}", duration_seconds=duration
        )

    duration = time.perf_counter() - start
    logger.info("translate_chapter_to_tamil ok duration_seconds=%.3f blocks=%d", duration, len(blocks))
    return TranslationOutcome(
        ok=True, ta_markdown="\n\n".join(translated_blocks), error=None, duration_seconds=duration,
    )
