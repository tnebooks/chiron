from pathlib import Path

from pdf2md.chapters import (
    Chapter,
    detect_chapters,
    draft_summary,
    merge_chapters,
    renumber_weights,
    slugify_title,
    split_chapter,
    strip_chapter_number_prefix,
)


def test_strip_chapter_number_prefix():
    assert strip_chapter_number_prefix("Chapter 2: Complex Numbers") == "Complex Numbers"
    assert strip_chapter_number_prefix("Chapter 10 - Discrete Mathematics") == "Discrete Mathematics"
    assert strip_chapter_number_prefix("Probability Distributions") == "Probability Distributions"


def test_slugify_title():
    assert slugify_title("Complex Numbers") == "complex-numbers"


def test_draft_summary_skips_non_prose_blocks_and_truncates():
    markdown = "# Chapter 1: Intro\n\n> a quote\n\n![](fig.png)\n\nThis is the real first paragraph.\n\nSecond."
    assert draft_summary(markdown) == "This is the real first paragraph."

    long_para = "word " * 100
    truncated = draft_summary(f"# Title\n\n{long_para}", max_chars=20)
    assert len(truncated) <= 20
    assert truncated.endswith("…")


def test_detect_chapters_splits_on_h1(tmp_path):
    markdown = (
        "# Chapter 1: Complex Numbers\n\nIntro text.\n\n"
        "## 1.1 Section\n\nMore text.\n\n"
        "# Chapter 2: Discrete Mathematics\n\nOther text.\n"
    )
    chapters = detect_chapters(markdown)

    assert [c.title for c in chapters] == ["Complex Numbers", "Discrete Mathematics"]
    assert [c.slug for c in chapters] == ["complex-numbers", "discrete-mathematics"]
    assert [c.weight for c in chapters] == [1, 2]
    assert "1.1 Section" in chapters[0].en_markdown
    assert "Chapter 2" not in chapters[0].en_markdown


def test_detect_chapters_no_headings_returns_empty():
    assert detect_chapters("just some text, no headings") == []


def test_detect_chapters_extracts_and_rewrites_images(tmp_path):
    image_path = tmp_path / "image_000000_abcdef.png"
    image_path.write_bytes(b"fake-png-bytes")

    markdown = f"# Chapter 1: Figures\n\n![Image]({image_path})\n\nSome caption text.\n"
    chapters = detect_chapters(markdown)

    assert len(chapters) == 1
    chapter = chapters[0]
    assert chapter.images == [("image_000000_abcdef.png", b"fake-png-bytes")]
    assert "![](image_000000_abcdef.png)" in chapter.en_markdown
    assert str(image_path) not in chapter.en_markdown


def test_detect_chapters_missing_image_file_is_skipped_not_crashed():
    markdown = "# Chapter 1: Missing\n\n![Image](/no/such/file.png)\n\nText.\n"
    chapters = detect_chapters(markdown)

    assert chapters[0].images == []
    assert "![](file.png)" in chapters[0].en_markdown


def _make_chapter(title: str, weight: int, ta: str | None = None) -> Chapter:
    return Chapter(
        id=title,
        title=title,
        slug=slugify_title(title),
        weight=weight,
        summary="",
        en_markdown=f"# Chapter {weight}: {title}\n\nBody of {title}.\n",
        ta_markdown=ta,
    )


def test_renumber_weights():
    chapters = [_make_chapter("B", 5), _make_chapter("A", 1)]
    renumber_weights(chapters)
    assert [c.weight for c in chapters] == [1, 2]


def test_merge_chapters_concatenates_and_keeps_first_title():
    chapters = [_make_chapter("A", 1), _make_chapter("B", 2), _make_chapter("C", 3)]
    result = merge_chapters(chapters, [0, 1])

    assert [c.title for c in result] == ["A", "C"]
    assert "Body of A." in result[0].en_markdown
    assert "Body of B." in result[0].en_markdown
    assert result[0].weight == 1
    assert result[1].weight == 2


def test_merge_chapters_drops_tamil_if_any_missing():
    chapters = [_make_chapter("A", 1, ta="தமிழ் A"), _make_chapter("B", 2, ta=None)]
    result = merge_chapters(chapters, [0, 1])
    assert result[0].ta_markdown is None


def test_merge_chapters_keeps_tamil_if_all_present():
    chapters = [_make_chapter("A", 1, ta="தமிழ் A"), _make_chapter("B", 2, ta="தமிழ் B")]
    result = merge_chapters(chapters, [0, 1])
    assert result[0].ta_markdown == "தமிழ் A\n\nதமிழ் B"


def test_merge_chapters_drops_mcqs_since_underlying_text_changed():
    from pdf2md.mcq import MCQ

    chapters = [_make_chapter("A", 1), _make_chapter("B", 2)]
    chapters[0].mcqs = [MCQ(id="q-1", question="Q?", choices=["a"], answers=["b"], explanation="e")]
    result = merge_chapters(chapters, [0, 1])
    assert result[0].mcqs == []


def test_split_chapter_starts_with_no_mcqs():
    chapter = _make_chapter("Combined", 1)
    chapter.en_markdown = (
        "# Chapter 1: Combined\n\nFirst part text.\n\n"
        "# Chapter 2: Second Part\n\nSecond part text.\n"
    )
    first, second = split_chapter(chapter, 3)
    assert first.mcqs == []
    assert second.mcqs == []


def test_split_chapter_finds_next_heading_for_second_title():
    chapter = _make_chapter("Combined", 1)
    chapter.en_markdown = (
        "# Chapter 1: Combined\n\nFirst part text.\n\n"
        "# Chapter 2: Second Part\n\nSecond part text.\n"
    )
    lines = chapter.en_markdown.splitlines()
    split_at = next(i for i, line in enumerate(lines) if line.startswith("# Chapter 2"))

    first, second = split_chapter(chapter, split_at)

    assert first.title == "Combined"
    assert second.title == "Second Part"
    assert "First part text." in first.en_markdown
    assert "Second part text." in second.en_markdown
    assert second.weight == first.weight + 1
