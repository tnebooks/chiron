import json

import responses

from pdf2md.chapters import Chapter
from pdf2md.converter import ConfidenceScores
from pdf2md.quality_gate import LLM_MIN_SCORE, OLLAMA_HOST, compute_quality_report


def _good_chapter(chapter_id: str = "1", weight: int = 1) -> Chapter:
    return Chapter(
        id=chapter_id, title="Electrostatics", slug="electrostatics", weight=weight,
        summary="Summary",
        en_markdown="# Chapter 1: Electrostatics\n\n" + ("Coulomb's law describes the force. " * 10),
    )


def _good_confidence() -> ConfidenceScores:
    return ConfidenceScores(mean_score=0.95, low_score=0.9, mean_grade="excellent", low_grade="excellent")


def _mock_judge(score: int, issues: list[str] | None = None):
    responses.add(
        responses.POST,
        f"{OLLAMA_HOST}/api/generate",
        json={"response": json.dumps({"score": score, "issues": issues or []})},
        status=200,
    )


@responses.activate
def test_clean_chapter_with_good_confidence_passes():
    _mock_judge(95)
    report = compute_quality_report([_good_chapter()], confidence=_good_confidence())

    assert report.passed is True
    assert report.chapters[0].passed is True


def test_poor_docling_confidence_hard_blocks_even_without_llm_check():
    poor_confidence = ConfidenceScores(mean_score=0.4, low_score=0.2, mean_grade="poor", low_grade="poor")
    report = compute_quality_report([_good_chapter()], confidence=poor_confidence, run_llm_check=False)

    assert report.parsing_fidelity_passed is False
    assert report.passed is False
    assert any(issue.severity == "critical" for issue in report.document_issues)


def test_empty_chapter_body_is_a_critical_structural_issue():
    empty_chapter = Chapter(
        id="1", title="Empty", slug="empty", weight=1, summary="", en_markdown="# Chapter 1: Empty\n",
    )
    report = compute_quality_report([empty_chapter], confidence=_good_confidence(), run_llm_check=False)

    assert report.chapters[0].passed is False
    assert any(issue.severity == "critical" for issue in report.chapters[0].issues)


def test_missing_image_reference_is_a_warning_not_critical():
    chapter = _good_chapter()
    chapter.en_markdown += "\n\n![](missing.png)\n"
    report = compute_quality_report([chapter], confidence=_good_confidence(), run_llm_check=False)

    issues = report.chapters[0].issues
    assert any("image" in issue.message for issue in issues)
    assert all(issue.severity == "warning" for issue in issues)


def test_duplicate_weights_are_a_document_level_critical_issue():
    chapters = [_good_chapter("1", weight=1), _good_chapter("2", weight=1)]
    report = compute_quality_report(chapters, confidence=_good_confidence(), run_llm_check=False)

    assert report.passed is False
    assert any(issue.severity == "critical" for issue in report.document_issues)


@responses.activate
def test_low_llm_score_fails_that_chapter():
    _mock_judge(int(LLM_MIN_SCORE) - 10, ["Sentence cuts off mid-thought."])
    report = compute_quality_report([_good_chapter()], confidence=_good_confidence())

    assert report.chapters[0].passed is False
    assert report.passed is False


@responses.activate
def test_llm_check_unreachable_does_not_fail_the_chapter_by_itself():
    import requests

    responses.add(
        responses.POST,
        f"{OLLAMA_HOST}/api/generate",
        body=requests.ConnectionError("connection refused"),
    )
    report = compute_quality_report([_good_chapter()], confidence=_good_confidence())

    assert report.chapters[0].llm_score is None
    assert report.chapters[0].passed is True  # structural checks alone still pass
