"""Hugo front-matter builder for tnebooks chapter `_index.md` files.

Format verified against a real, live `_index.md` fetched from
tnebooks/12th-maths (content.en/docs/complex-numbers/_index.md) rather than
guessed: YAML front matter delimited by `---`, `categories` is always a
single-element list equal to the chapter's own slug, `title`/`summary` are
double-quoted. Hand-rolled instead of `yaml.safe_dump` for exact, stable,
byte-level control (PyYAML needs `allow_unicode=True` to avoid escaping
Tamil as `\\uXXXX`, and its quoting/indentation choices aren't guaranteed
stable across versions -- a template is simpler to test against real bytes).
"""

from __future__ import annotations

from pdf2md.chapters import Chapter


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_front_matter(title: str, categories: list[str], weight: int, summary: str) -> str:
    lines = ["---", f"title: {_quote(title)}", "categories:"]
    lines += [f"- {category}" for category in categories]
    lines += [f"weight: {weight}", f"summary: {_quote(summary)}", "---"]
    return "\n".join(lines) + "\n"


def build_chapter_file(chapter: Chapter, language: str) -> str:
    """language in {"en", "ta"}. Body is the chapter's markdown verbatim
    (already includes its own leading `# Chapter N: Title` heading, produced
    during chapter detection/review) -- this module owns only the header."""
    if language == "en":
        title, body = chapter.title, chapter.en_markdown
    elif language == "ta":
        title, body = chapter.title, chapter.ta_markdown or ""
    else:
        raise ValueError(f"language must be 'en' or 'ta', got {language!r}")

    front_matter = build_front_matter(
        title=title, categories=[chapter.slug], weight=chapter.weight, summary=chapter.summary,
    )
    return front_matter + "\n" + body
