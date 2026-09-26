from pathlib import Path

from pdf2md.converter import ConversionOptions, convert_pdf_to_markdown, get_converter, warm_up

FIXTURES = Path(__file__).parent / "fixtures"


def test_warm_up_then_convert_is_fast():
    opts = ConversionOptions()
    converter = get_converter(opts)
    warm_up(converter)

    outcome = convert_pdf_to_markdown(converter, FIXTURES / "sample.pdf", "sample.pdf", opts)

    assert outcome.ok is True
    assert "Hello Docling Test" in outcome.markdown
    # Warmed-up conversion of a tiny one-page PDF should be well under a
    # second; a regression here likely means warm_up() stopped doing its job.
    assert outcome.duration_seconds < 2.0


def test_convert_text_pdf_succeeds():
    opts = ConversionOptions()
    converter = get_converter(opts)
    outcome = convert_pdf_to_markdown(converter, FIXTURES / "sample.pdf", "sample.pdf", opts)

    assert outcome.ok is True
    assert outcome.error is None
    assert "Hello Docling Test" in outcome.markdown
    assert outcome.duration_seconds > 0
    assert outcome.started_at < outcome.ended_at


def test_convert_corrupt_pdf_returns_error_without_raising(tmp_path):
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4\nnot a real pdf body")

    opts = ConversionOptions()
    converter = get_converter(opts)
    outcome = convert_pdf_to_markdown(converter, corrupt, "corrupt.pdf", opts)

    assert outcome.ok is False
    assert outcome.markdown is None
    assert outcome.error
    assert outcome.duration_seconds >= 0
