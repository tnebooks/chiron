import json

import responses

from pdf2md.chapters import Chapter
from pdf2md.mcq import OLLAMA_HOST, generate_mcqs_for_chapter


def _make_chapter() -> Chapter:
    return Chapter(
        id="1", title="Electrostatics", slug="electrostatics", weight=1,
        summary="Summary", en_markdown="# Chapter 1: Electrostatics\n\nCoulomb's law body text.\n",
    )


def _mock_generate(payload: dict | str):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    responses.add(
        responses.POST,
        f"{OLLAMA_HOST}/api/generate",
        json={"response": body},
        status=200,
    )


@responses.activate
def test_generates_mcqs_from_valid_model_response():
    _mock_generate(
        [
            {
                "question": "What is Coulomb's constant?",
                "options": ["9x10^9", "6.6x10^-11", "3x10^8", "1.6x10^-19"],
                "correct_index": 0,
                "explanation": "Coulomb's constant is 9x10^9 Nm^2/C^2.",
                "complexity": "E",
            }
        ]
    )

    outcome = generate_mcqs_for_chapter(_make_chapter(), count=1)

    assert outcome.ok is True
    assert len(outcome.mcqs) == 1
    mcq = outcome.mcqs[0]
    assert mcq.question == "What is Coulomb's constant?"
    assert mcq.answers == ["9x10^9"]
    assert set(mcq.choices) == {"6.6x10^-11", "3x10^8", "1.6x10^-19"}
    assert mcq.complexity == "E"
    assert mcq.id.startswith("q-")


@responses.activate
def test_strips_code_fence_wrapping():
    _mock_generate(
        '```json\n[{"question": "Q?", "options": ["a", "b"], "correct_index": 1, '
        '"explanation": "e", "complexity": "M"}]\n```'
    )

    outcome = generate_mcqs_for_chapter(_make_chapter(), count=1)

    assert outcome.ok is True
    assert outcome.mcqs[0].answers == ["b"]
    assert outcome.mcqs[0].choices == ["a"]


@responses.activate
def test_retries_once_on_malformed_json_then_succeeds():
    _mock_generate("not json at all")
    _mock_generate(
        [{"question": "Q?", "options": ["a", "b"], "correct_index": 0, "explanation": "e"}]
    )

    outcome = generate_mcqs_for_chapter(_make_chapter(), count=1)

    assert outcome.ok is True
    assert len(responses.calls) == 2


@responses.activate
def test_malformed_json_on_both_attempts_returns_clean_outcome():
    _mock_generate("garbage")
    _mock_generate("still garbage")

    outcome = generate_mcqs_for_chapter(_make_chapter(), count=1)

    assert outcome.ok is False
    assert outcome.mcqs == []
    assert outcome.error


@responses.activate
def test_connection_error_returns_clean_outcome_not_exception():
    import requests

    responses.add(
        responses.POST,
        f"{OLLAMA_HOST}/api/generate",
        body=requests.ConnectionError("connection refused"),
    )

    outcome = generate_mcqs_for_chapter(_make_chapter(), count=1)

    assert outcome.ok is False
    assert outcome.mcqs == []
    assert outcome.error
