"""Chapter-wise multiple-choice question generation via the same local
Ollama model already used for translation (see `translation.py`). Never
raises for expected failure modes (Ollama not running, timeout, malformed
JSON) -- returns a structured outcome instead so the UI can show a friendly
error and let the user retry.

Output schema mirrors the real tnebooks `_questions` repo format, verified
against a live file (`tnebooks/12th-physics_questions`, e.g.
`questions/science/physics/electrostatics/electric-dipole.md`): a question
string, a list of incorrect `choices`, a list of correct `answers` (usually
one), plus optional `complexity` ("E"/"M"/"H") and `tags`.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import requests

from pdf2md.translation import DEFAULT_OLLAMA_MODEL, OLLAMA_HOST

if TYPE_CHECKING:
    from pdf2md.chapters import Chapter

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 180

_GENERATE_PROMPT = (
    "You are writing exam-style multiple-choice questions for a school "
    "textbook chapter. Read the chapter text below and write {count} "
    "distinct multiple-choice questions that test understanding of it.\n\n"
    "Respond with ONLY a JSON array, no commentary, no markdown code fences. "
    "Each element must be an object with exactly these keys:\n"
    '  "question": the question text (string)\n'
    '  "options": exactly 4 answer option strings, in any order\n'
    '  "correct_index": the 0-based index into "options" of the correct answer\n'
    '  "explanation": a short worked explanation of why that answer is correct\n'
    '  "complexity": one of "E" (easy), "M" (medium), "H" (hard)\n\n'
    "Chapter text:\n{chapter_text}"
)


@dataclass
class MCQ:
    id: str
    question: str
    choices: list[str]  # incorrect options
    answers: list[str]  # correct option(s), usually len 1
    explanation: str
    complexity: str | None = None
    tags: list[str] = field(default_factory=list)
    ta_question: str | None = None
    ta_choices: list[str] | None = None
    ta_answers: list[str] | None = None
    ta_explanation: str | None = None


@dataclass
class McqGenerationOutcome:
    ok: bool
    mcqs: list[MCQ]
    error: str | None
    duration_seconds: float


def _mcq_id() -> str:
    """Random, not content-derived -- two questions with the same or
    similar text (across chapters, or even within one generation batch)
    must not collide, since this id is both a Streamlit widget key and a
    git filename (see `github_pr.build_mcq_files`)."""
    return f"q-{uuid.uuid4().hex[:8]}"


def _call_ollama(prompt: str, model: str, host: str) -> str:
    response = requests.post(
        f"{host}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["response"].strip()


def _strip_code_fence(text: str) -> str:
    """Some local models wrap JSON in ```json ... ``` even when asked not
    to -- strip a single leading/trailing fence if present rather than
    failing the whole generation over cosmetic model behavior."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _parse_mcq_items(raw: str) -> list[MCQ]:
    items = json.loads(_strip_code_fence(raw))
    if not isinstance(items, list):
        raise ValueError("expected a JSON array of questions")

    mcqs: list[MCQ] = []
    for item in items:
        question = str(item["question"]).strip()
        options = [str(o).strip() for o in item["options"]]
        correct_index = int(item["correct_index"])
        if not (0 <= correct_index < len(options)):
            raise ValueError(f"correct_index {correct_index} out of range for {len(options)} options")
        answer = options.pop(correct_index)
        complexity = item.get("complexity")
        mcqs.append(
            MCQ(
                id=_mcq_id(),
                question=question,
                choices=options,
                answers=[answer],
                explanation=str(item.get("explanation", "")).strip(),
                complexity=str(complexity).strip() if complexity else None,
            )
        )
    return mcqs


def generate_mcqs_for_chapter(
    chapter: Chapter,
    count: int = 5,
    model: str = DEFAULT_OLLAMA_MODEL,
    host: str = OLLAMA_HOST,
) -> McqGenerationOutcome:
    """Generate `count` MCQs from `chapter.en_markdown`. Retries the parse
    once against a fresh model call if the first response isn't valid JSON
    in the expected shape -- local models occasionally deviate from the
    requested format on a single call."""
    start = time.perf_counter()
    prompt = _GENERATE_PROMPT.format(count=count, chapter_text=chapter.en_markdown)

    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            raw = _call_ollama(prompt, model, host)
            mcqs = _parse_mcq_items(raw)
            duration = time.perf_counter() - start
            logger.info(
                "generate_mcqs_for_chapter ok chapter=%s count=%d duration_seconds=%.3f",
                chapter.title, len(mcqs), duration,
            )
            return McqGenerationOutcome(ok=True, mcqs=mcqs, error=None, duration_seconds=duration)
        except requests.RequestException as exc:
            duration = time.perf_counter() - start
            logger.info("generate_mcqs_for_chapter failed chapter=%s error=%s", chapter.title, exc)
            return McqGenerationOutcome(ok=False, mcqs=[], error=str(exc), duration_seconds=duration)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.info("generate_mcqs_for_chapter bad response chapter=%s attempt=%d error=%s", chapter.title, _attempt, exc)
            continue

    duration = time.perf_counter() - start
    return McqGenerationOutcome(
        ok=False, mcqs=[], error=f"Unexpected model response: {last_error}", duration_seconds=duration,
    )
