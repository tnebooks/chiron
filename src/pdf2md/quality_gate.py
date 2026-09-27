"""Pre-publish quality gate for the wizard's QUALITY_CHECK step.

Combines three independent signals into a single pass/fail per chapter,
and an overall pass/fail that gates the CREATE_PR step (hard block, no
override -- see the plan this implements):

1. Parsing fidelity -- Docling's own `ConfidenceScores` (see converter.py),
   computed once for the whole document during PDF conversion.
2. Structural completeness -- cheap heuristics here, no LLM: empty/near-
   empty bodies, image references that don't resolve to an extracted image,
   leftover Unicode replacement characters, duplicate/out-of-order chapter
   weights.
3. LLM-judged content quality -- the same local Ollama model used for
   translation/MCQ generation, asked to flag incoherence, abrupt cut-offs,
   repeated/looping boilerplate (a known Docling failure mode), or
   OCR-garbled-looking text. Judges the markdown for internal coherence
   only, not against a separate ground-truth extraction of the PDF.

Never raises for expected failure modes (Ollama unreachable, malformed
response) -- a chapter simply gets no LLM score and a note that the check
couldn't run, rather than failing the whole gate over an infrastructure
hiccup unrelated to parse quality.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import requests

from pdf2md.converter import ConfidenceScores
from pdf2md.translation import DEFAULT_OLLAMA_MODEL, OLLAMA_HOST

if TYPE_CHECKING:
    from pdf2md.chapters import Chapter

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 120

# Below these, a chapter fails the gate even with no critical issues.
STRUCTURAL_MIN_SCORE = 60.0
LLM_MIN_SCORE = 60.0
MIN_BODY_CHARS = 40  # excluding the heading line itself

_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_REPLACEMENT_CHAR = "\N{REPLACEMENT CHARACTER}"

_JUDGE_PROMPT = (
    "You are checking a textbook chapter that was auto-extracted from a PDF "
    "for quality problems introduced by the extraction itself -- NOT for "
    "the accuracy of the subject matter.\n"
    "Look for: sentences that cut off mid-thought, paragraphs or phrases "
    "that repeat/loop (a known PDF-extraction artifact), text that reads as "
    "garbled OCR noise, or headings/sections that don't make sense in "
    "context.\n\n"
    "Respond with ONLY a JSON object, no commentary, no markdown code "
    "fences, with exactly these keys:\n"
    '  "score": an integer 0-100, 100 meaning no extraction problems found\n'
    '  "issues": a JSON array of short strings, one per specific problem found (empty array if none)\n\n'
    "Chapter text:\n{chapter_text}"
)


@dataclass(frozen=True)
class QualityIssue:
    severity: str  # "critical" | "warning"
    message: str


@dataclass
class ChapterQualityResult:
    chapter_id: str
    chapter_title: str
    structural_score: float
    llm_score: float | None
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if any(issue.severity == "critical" for issue in self.issues):
            return False
        if self.structural_score < STRUCTURAL_MIN_SCORE:
            return False
        if self.llm_score is not None and self.llm_score < LLM_MIN_SCORE:
            return False
        return True


@dataclass
class QualityReport:
    confidence: ConfidenceScores | None
    parsing_fidelity_passed: bool
    document_issues: list[QualityIssue]
    chapters: list[ChapterQualityResult]

    @property
    def passed(self) -> bool:
        if not self.parsing_fidelity_passed:
            return False
        if any(issue.severity == "critical" for issue in self.document_issues):
            return False
        return all(chapter.passed for chapter in self.chapters)


def _check_parsing_fidelity(confidence: ConfidenceScores | None) -> tuple[bool, QualityIssue | None]:
    if confidence is None:
        return True, QualityIssue("warning", "No parsing-confidence signal was available for this file.")
    if confidence.mean_grade == "poor" or confidence.low_grade == "poor":
        return False, QualityIssue(
            "critical",
            f"Docling rated the extraction quality as poor "
            f"(overall: {confidence.mean_grade}, worst page: {confidence.low_grade}). "
            "Try re-scanning or re-exporting the source PDF at a higher quality.",
        )
    return True, None


def _check_weights(chapters: list["Chapter"]) -> list[QualityIssue]:
    weights = [c.weight for c in chapters]
    if len(set(weights)) != len(weights):
        return [QualityIssue("critical", "Two or more chapters share the same order number.")]
    return []


def _structural_check(chapter: "Chapter") -> tuple[float, list[QualityIssue]]:
    issues: list[QualityIssue] = []
    body_without_heading = re.sub(r"^#{1,6}\s+.*$", "", chapter.en_markdown, count=1, flags=re.MULTILINE)

    if len(body_without_heading.strip()) < MIN_BODY_CHARS:
        issues.append(QualityIssue("critical", "This chapter has little or no body text."))

    if _REPLACEMENT_CHAR in chapter.en_markdown:
        issues.append(QualityIssue("critical", "This chapter contains unreadable characters (extraction artifact)."))

    image_names = {name for name, _ in chapter.images}
    referenced = {ref for ref in _IMAGE_REF_RE.findall(chapter.en_markdown) if ref}
    missing = {ref for ref in referenced if _basename(ref) not in image_names}
    if missing:
        issues.append(
            QualityIssue("warning", f"{len(missing)} image reference(s) don't match an extracted picture.")
        )

    score = 100.0
    for issue in issues:
        score -= 60.0 if issue.severity == "critical" else 15.0
    return max(score, 0.0), issues


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _call_ollama_judge(chapter_text: str, model: str, host: str) -> dict:
    response = requests.post(
        f"{host}/api/generate",
        json={
            "model": model,
            "prompt": _JUDGE_PROMPT.format(chapter_text=chapter_text),
            "stream": False,
            "format": "json",
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return json.loads(response.json()["response"].strip())


def _llm_check(chapter: "Chapter", model: str, host: str) -> tuple[float | None, list[QualityIssue]]:
    try:
        result = _call_ollama_judge(chapter.en_markdown, model, host)
        score = float(result["score"])
        issues = [QualityIssue("warning", str(msg)) for msg in result.get("issues", [])]
        return score, issues
    except requests.RequestException as exc:
        logger.info("quality_gate llm check unreachable chapter=%s error=%s", chapter.title, exc)
        return None, [QualityIssue("warning", "The content-quality check couldn't run (model unreachable).")]
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.info("quality_gate llm check bad response chapter=%s error=%s", chapter.title, exc)
        return None, [QualityIssue("warning", "The content-quality check returned an unexpected response.")]


def compute_quality_report(
    chapters: list["Chapter"],
    confidence: ConfidenceScores | None,
    model: str = DEFAULT_OLLAMA_MODEL,
    host: str = OLLAMA_HOST,
    run_llm_check: bool = True,
) -> QualityReport:
    start = time.perf_counter()
    parsing_fidelity_passed, fidelity_issue = _check_parsing_fidelity(confidence)
    document_issues = _check_weights(chapters)
    if fidelity_issue is not None:
        document_issues.append(fidelity_issue)

    chapter_results: list[ChapterQualityResult] = []
    for chapter in chapters:
        structural_score, structural_issues = _structural_check(chapter)
        llm_score: float | None = None
        llm_issues: list[QualityIssue] = []
        if run_llm_check:
            llm_score, llm_issues = _llm_check(chapter, model, host)
        chapter_results.append(
            ChapterQualityResult(
                chapter_id=chapter.id,
                chapter_title=chapter.title,
                structural_score=structural_score,
                llm_score=llm_score,
                issues=structural_issues + llm_issues,
            )
        )

    logger.info("compute_quality_report duration_seconds=%.3f chapters=%d", time.perf_counter() - start, len(chapters))
    return QualityReport(
        confidence=confidence,
        parsing_fidelity_passed=parsing_fidelity_passed,
        document_issues=document_issues,
        chapters=chapter_results,
    )
