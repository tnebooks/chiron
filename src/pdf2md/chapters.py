"""Chapter detection and editing for the publish-to-GitHub wizard.

Splits a whole-subject Docling markdown export into per-chapter pieces on
heading boundaries, and supports the human-review adjustments (rename,
re-slug, reorder, merge, split, edit weight) the wizard's REVIEW_CHAPTERS
step needs. No Streamlit dependency -- unit-testable on its own.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from typing import TYPE_CHECKING

from slugify import slugify

if TYPE_CHECKING:
    from pdf2md.mcq import MCQ

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_CHAPTER_PREFIX_RE = re.compile(r"^chapter\s+\d+\s*[:\-]?\s*", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)


@dataclass
class Chapter:
    id: str
    title: str
    slug: str
    weight: int
    summary: str
    en_markdown: str
    ta_markdown: str | None = None
    images: list[tuple[str, bytes]] = field(default_factory=list)
    mcqs: list["MCQ"] = field(default_factory=list)


def slugify_title(title: str) -> str:
    return slugify(title)


def strip_chapter_number_prefix(heading_text: str) -> str:
    """"Chapter 2: Complex Numbers" -> "Complex Numbers"."""
    return _CHAPTER_PREFIX_RE.sub("", heading_text).strip()


def draft_summary(en_markdown: str, max_chars: int = 280) -> str:
    """First non-heading/image/quote/table paragraph, whitespace-collapsed
    and truncated -- a starting draft, edited by a human afterward."""
    for block in en_markdown.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(("#", "!", ">", "|")):
            continue
        text = " ".join(block.split())
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text
    return ""


def _extract_images(markdown: str) -> tuple[str, list[tuple[str, bytes]]]:
    """Rewrite each image link's path to a bare filename (Docling emits
    absolute paths when exporting with ImageRefMode.REFERENCED) and collect
    the corresponding bytes from wherever Docling actually wrote them."""
    images: list[tuple[str, bytes]] = []
    seen: set[str] = set()

    def _replace(match: re.Match) -> str:
        filename = Path(match.group(1)).name
        if filename not in seen:
            seen.add(filename)
            try:
                images.append((filename, Path(match.group(1)).read_bytes()))
            except OSError:
                pass  # referenced but missing on disk -- skip rather than crash
        return f"![]({filename})"

    return _IMAGE_RE.sub(_replace, markdown), images


def detect_chapters(markdown: str, heading_level: int = 1) -> list[Chapter]:
    """Split a whole-document markdown export into chapters on heading
    boundaries at `heading_level` (default: H1 only, matching the real
    tnebooks convention of one `# Chapter N: Title` heading per chapter).
    """
    heading_re = re.compile(rf"^#{{1,{heading_level}}}\s+(.*)$", re.MULTILINE)
    matches = list(heading_re.finditer(markdown))
    if not matches:
        return []

    chapters: list[Chapter] = []
    for position, match in enumerate(matches):
        start = match.start()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(markdown)
        chunk = markdown[start:end].rstrip() + "\n"
        title = strip_chapter_number_prefix(match.group(1).strip())
        body, images = _extract_images(chunk)
        chapters.append(
            Chapter(
                id=str(uuid.uuid4()),
                title=title,
                slug=slugify_title(title),
                weight=position + 1,
                summary=draft_summary(body),
                en_markdown=body,
                images=images,
            )
        )
    return chapters


def renumber_weights(chapters: list[Chapter]) -> list[Chapter]:
    for i, chapter in enumerate(chapters, start=1):
        chapter.weight = i
    return chapters


def merge_chapters(chapters: list[Chapter], indices: list[int]) -> list[Chapter]:
    """Merge the chapters at `indices` (any order, need not be contiguous)
    into one, placed at the position of the first selected chapter, keeping
    its title/slug. Bodies are concatenated in original order; Tamil is kept
    only if every merged chapter already had a translation, else dropped
    (re-translation needed). Weights are renumbered afterward."""
    if len(indices) < 2:
        return renumber_weights(list(chapters))

    indices_set = set(indices)
    first_idx = min(indices_set)
    to_merge = [chapters[i] for i in sorted(indices_set)]

    merged = to_merge[0]
    merged.en_markdown = "\n\n".join(ch.en_markdown for ch in to_merge)
    merged.ta_markdown = (
        "\n\n".join(ch.ta_markdown for ch in to_merge)
        if all(ch.ta_markdown for ch in to_merge)
        else None
    )
    merged.mcqs = []  # stale once the underlying text changes -- regenerate
    seen = {name for name, _ in merged.images}
    for ch in to_merge[1:]:
        for name, data in ch.images:
            if name not in seen:
                seen.add(name)
                merged.images.append((name, data))

    result: list[Chapter] = []
    for i, chapter in enumerate(chapters):
        if i == first_idx:
            result.append(merged)
        elif i in indices_set:
            continue
        else:
            result.append(chapter)
    return renumber_weights(result)


def split_chapter(chapter: Chapter, at_line: int) -> tuple[Chapter, Chapter]:
    """Split a chapter's English markdown at 0-based line index `at_line`
    into two chapters. Images are assigned to whichever half still
    references them. Tamil translation is dropped from both halves (each
    needs re-translating); the caller re-numbers weights afterward via
    `renumber_weights` on the full chapter list."""
    lines = chapter.en_markdown.splitlines()
    first_body = "\n".join(lines[:at_line]).rstrip() + "\n"
    second_body = "\n".join(lines[at_line:]).rstrip() + "\n"

    def _images_used_in(body: str) -> list[tuple[str, bytes]]:
        used = {m.group(1) for m in _IMAGE_RE.finditer(body)}
        return [(name, data) for name, data in chapter.images if name in used]

    second_match = _ANY_HEADING_RE.search(second_body)
    second_title = (
        strip_chapter_number_prefix(second_match.group(1).strip())
        if second_match
        else f"{chapter.title} (continued)"
    )

    first = Chapter(
        id=str(uuid.uuid4()),
        title=chapter.title,
        slug=slugify_title(chapter.title),
        weight=chapter.weight,
        summary=draft_summary(first_body),
        en_markdown=first_body,
        images=_images_used_in(first_body),
    )
    second = Chapter(
        id=str(uuid.uuid4()),
        title=second_title,
        slug=slugify_title(second_title),
        weight=chapter.weight + 1,
        summary=draft_summary(second_body),
        en_markdown=second_body,
        images=_images_used_in(second_body),
    )
    return first, second
