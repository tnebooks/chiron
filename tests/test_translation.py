import re

import requests
import responses

from pdf2md.translation import OLLAMA_HOST, translate_chapter_to_tamil


def _mock_generate(response_text: str = "TRANSLATED"):
    responses.add(
        responses.POST,
        f"{OLLAMA_HOST}/api/generate",
        json={"response": response_text},
        status=200,
    )


@responses.activate
def test_translates_prose_blocks_and_passes_through_others():
    for _ in range(2):  # two prose blocks in the fixture below
        _mock_generate("TRANSLATED")

    markdown = (
        "# Chapter 1: Complex Numbers\n\n"
        "This is prose that should be translated.\n\n"
        "![](fig.png)\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "Another prose paragraph here.\n"
    )

    outcome = translate_chapter_to_tamil(markdown)

    assert outcome.ok is True
    assert outcome.error is None
    blocks = outcome.ta_markdown.split("\n\n")
    assert blocks[0] == "TRANSLATED"  # heading block translated
    assert blocks[1] == "TRANSLATED"  # first prose paragraph
    assert blocks[2] == "![](fig.png)"  # image-only block untouched
    assert "| a | b |" in blocks[3]  # table untouched
    assert blocks[4] == "TRANSLATED"  # second prose paragraph

    # Exactly the two non-heading prose blocks + heading = 3 calls expected,
    # but heading counts as translatable prose too -- verify call count.
    assert len(responses.calls) == 3


@responses.activate
def test_progress_callback_invoked_per_block():
    _mock_generate()
    seen = []
    translate_chapter_to_tamil("Just one prose block.", on_block_done=lambda i, n: seen.append((i, n)))
    assert seen == [(1, 1)]


@responses.activate
def test_connection_error_returns_clean_outcome_not_exception():
    responses.add(
        responses.POST,
        f"{OLLAMA_HOST}/api/generate",
        body=requests.ConnectionError("connection refused"),
    )
    outcome = translate_chapter_to_tamil("Some prose to translate.")

    assert outcome.ok is False
    assert outcome.ta_markdown is None
    assert outcome.error


@responses.activate
def test_malformed_response_returns_clean_outcome_not_exception():
    responses.add(
        responses.POST,
        f"{OLLAMA_HOST}/api/generate",
        json={"unexpected_key": "oops"},
        status=200,
    )
    outcome = translate_chapter_to_tamil("Some prose to translate.")

    assert outcome.ok is False
    assert "Unexpected Ollama response" in outcome.error


@responses.activate
def test_pure_math_and_blank_blocks_are_not_sent_to_model():
    markdown = "$i^2 = -1$\n\n\n\n123456"
    outcome = translate_chapter_to_tamil(markdown)

    assert outcome.ok is True
    assert len(responses.calls) == 0
    assert re.sub(r"\n+", "\n\n", outcome.ta_markdown).strip() == "$i^2 = -1$\n\n123456"
