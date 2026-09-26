from pathlib import Path

from pdf2md.chapters import Chapter
from pdf2md.hugo_frontmatter import build_chapter_file, build_front_matter

FIXTURES = Path(__file__).parent / "fixtures"

# Ground truth: content.en/docs/complex-numbers/_index.md, fetched live from
# tnebooks/12th-maths this session. Our builder's quoting style (double
# quotes, unindented `- ` list items) was chosen to match this real file
# byte-for-byte. NOTE: the *Tamil* twin of this same real file uses a
# different, inconsistent quoting style (single-quoted title, indented list)
# -- a deliberate product decision (see plan) is to emit one consistent
# style for both languages rather than replicate that inconsistency.
REAL_SUMMARY = (
    "This chapter introduces complex numbers as numbers of the form a + ib, where i is the "
    "imaginary unit (√−1), extending the real number system to solve equations that "
    "have no real solutions. It covers the algebraic operations on complex numbers, including "
    "addition, subtraction, multiplication, division, and the concept of conjugates and modulus, "
    "along with the geometric representation of complex numbers in the Argand plane. The chapter "
    "also discusses the polar and exponential forms of complex numbers, De Moivre's theorem, and "
    "its applications in finding powers and roots of complex numbers, as well as solving "
    "polynomial equations."
)


def test_build_front_matter_matches_real_tnebooks_format():
    front_matter = build_front_matter(
        title="Complex Numbers", categories=["complex-numbers"], weight=2, summary=REAL_SUMMARY,
    )
    real = FIXTURES / "real_index_en.md"
    real_front_matter = real.read_text().split("---\n\n", 1)[0] + "---\n"

    assert front_matter == real_front_matter


def test_build_chapter_file_en():
    chapter = Chapter(
        id="1", title="Complex Numbers", slug="complex-numbers", weight=2,
        summary=REAL_SUMMARY, en_markdown="# Chapter 2: Complex Numbers\n\nBody text.\n",
    )
    result = build_chapter_file(chapter, "en")

    assert result.startswith('---\ntitle: "Complex Numbers"\n')
    assert "categories:\n- complex-numbers\n" in result
    assert "weight: 2\n" in result
    assert result.endswith("# Chapter 2: Complex Numbers\n\nBody text.\n")


def test_build_chapter_file_ta_uses_ta_markdown():
    chapter = Chapter(
        id="1", title="Complex Numbers", slug="complex-numbers", weight=2,
        summary="Summary", en_markdown="# EN body\n", ta_markdown="# TA body\n",
    )
    result = build_chapter_file(chapter, "ta")
    assert result.endswith("# TA body\n")
    assert "EN body" not in result


def test_build_chapter_file_rejects_unknown_language():
    chapter = Chapter(id="1", title="X", slug="x", weight=1, summary="", en_markdown="")
    try:
        build_chapter_file(chapter, "fr")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_quotes_are_escaped():
    front_matter = build_front_matter(
        title='Say "Hi"', categories=["x"], weight=1, summary='A "quoted" summary',
    )
    assert 'title: "Say \\"Hi\\""' in front_matter
    assert 'summary: "A \\"quoted\\" summary"' in front_matter
