"""Single-flow content-onboarding app: upload a subject textbook PDF,
review the auto-detected chapters, check the Tamil translation, then send
the book to the team for review.

UI copy is deliberately non-technical (the audience is non-technical staff)
-- e.g. no "GitHub", "PR", "repo", "branch", "commit", "OCR", "Ollama", or
"markdown" anywhere a user can see it. Those are exactly what's happening
underneath (a GitHub pull request built via converter.py/chapters.py/
translation.py/github_pr.py), and that's spelled out here in comments and
in the module names for developer/maintainer sanity -- just not in any
st.* string literal below.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
from pathlib import Path

import streamlit as st
from docling_core.types.io import DocumentStream
from dotenv import load_dotenv
from github import Auth, Github

import requests

from pdf2md.chapters import (
    detect_chapters,
    merge_chapters,
    renumber_weights,
    slugify_title,
    split_chapter,
)
from pdf2md.converter import ConversionOptions, convert_pdf_to_markdown, get_converter, warm_up
from pdf2md.github_pr import (
    create_chapter_pr,
    create_questions_pr,
    discover_questions_path_convention,
    list_textbook_repos,
    resolve_mcq_path_prefix,
    resolve_questions_repo,
)
from pdf2md.markdown_editor import markdown_editor
from pdf2md.mcq import generate_mcqs_for_chapter
from pdf2md.quality_gate import compute_quality_report
from pdf2md.translation import translate_chapter_to_tamil, translate_text
from pdf2md.wizard_state import WizardState, WizardStep, next_step

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Add New Subject", page_icon="📘", layout="wide")

# Visual polish only -- no behavior here. Streamlit's default chrome (the
# header bar with its Deploy/menu buttons) is hidden and replaced with a
# plain title + a custom step indicator (see _render_stepper), a real
# webfont is loaded, and buttons/inputs/cards get softer corners and
# shadows so this doesn't read as a generic Streamlit dashboard.
_CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class^="st-"], [class*=" st-"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}
/* Streamlit's expander/toolbar arrow glyphs are ligature text ("keyboard_
   arrow_right" etc.) rendered via this icon font -- the blanket font-family
   override above would otherwise turn them into literal visible text. */
[data-testid="stIconMaterial"] {
    font-family: 'Material Symbols Rounded' !important;
}

/* Hide Streamlit's default app chrome (Deploy button, hamburger menu, the
   header bar itself -- there's no sidebar to collapse anymore). */
[data-testid="stHeader"],
[data-testid="stAppDeployButton"],
[data-testid="stMainMenu"] {
    display: none;
}

/* A focused, form-like column instead of a full-width dashboard. */
[data-testid="stMainBlockContainer"] {
    max-width: 900px;
    margin: 0 auto;
    padding-top: 2.5rem;
    padding-bottom: 4rem;
}

h1 { font-weight: 700 !important; letter-spacing: -0.01em; }
h2 { font-weight: 600 !important; }

[data-testid="stBaseButton-primary"] {
    border-radius: 10px;
    font-weight: 600;
    padding: 0.5rem 1.4rem;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.08);
    transition: transform 0.05s ease, box-shadow 0.15s ease;
}
[data-testid="stBaseButton-primary"]:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(79, 70, 229, 0.28);
}
[data-testid="stBaseButton-secondary"] {
    border-radius: 10px;
    font-weight: 500;
}

textarea, input[type="text"], [data-baseweb="select"] > div, [data-baseweb="base-input"] {
    border-radius: 10px !important;
}

/* Chapter review cards (st.container(border=True)). */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 14px !important;
    border-color: #E5E7EB !important;
    box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06);
}

.pdf2md-stepper { display: flex; align-items: center; margin: 0.25rem 0 0.5rem; }
.pdf2md-step-dot {
    width: 28px; height: 28px; min-width: 28px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.75rem; font-weight: 700; background: #E5E7EB; color: #9CA3AF;
}
.pdf2md-step-dot.done { background: #4F46E5; color: #fff; }
.pdf2md-step-dot.active { background: #fff; color: #4F46E5; border: 2px solid #4F46E5; }
.pdf2md-step-line { flex: 1; height: 2px; background: #E5E7EB; margin: 0 4px; }
.pdf2md-step-line.done { background: #4F46E5; }
.pdf2md-step-caption { color: #6B7280; font-size: 0.95rem; margin-bottom: 1.75rem; }
.pdf2md-step-caption b { color: #111827; }
</style>
"""
st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)

# Fixed parsing settings -- not exposed in the UI since a non-technical user
# has no meaningful basis to choose them. do_ocr=False assumes born-digital
# source PDFs (the common case for this org); table_mode=FAST trades a
# little table-cell accuracy for speed. generate_picture_images=True is
# required so figures can be pulled out and included alongside each chapter.
_PARSE_OPTS = ConversionOptions(
    do_ocr=False, do_table_structure=True, generate_picture_images=True,
)

# Friendly, non-technical labels for the step indicator. Kept separate from
# WizardStep's own (code-facing) names on purpose.
_STEP_LABELS = {
    WizardStep.SELECT_REPO: "Choose subject",
    WizardStep.UPLOAD: "Upload book",
    WizardStep.PARSE: "Reading book",
    WizardStep.REVIEW_CHAPTERS: "Review chapters",
    WizardStep.TRANSLATE_REVIEW: "Tamil translation",
    WizardStep.GENERATE_MCQS: "Practice questions",
    WizardStep.QUALITY_CHECK: "Quality check",
    WizardStep.CREATE_PR: "Send for review",
    WizardStep.DONE: "Done",
}

_GRADE_SUBJECT_RE = re.compile(r"^(\d{1,2}(?:st|nd|rd|th))-(.+)$")


def _pretty_subject_label(repo_name: str) -> str:
    """"12th-maths" -> "12th Std - Maths". Falls back to the raw name for
    anything that doesn't match the grade-subject convention."""
    match = _GRADE_SUBJECT_RE.match(repo_name)
    if not match:
        return repo_name
    grade, subject = match.groups()
    return f"{grade} Std - {subject.replace('-', ' ').title()}"


if "wizard" not in st.session_state:
    st.session_state.wizard = WizardState()
state: WizardState = st.session_state.wizard

_title_col, _reset_col = st.columns([5, 1])
with _title_col:
    st.title("Add a New Subject Book")
    st.caption(
        "Upload a textbook PDF. We'll help you check the chapters and the Tamil "
        "translation before sending it to your team."
    )
with _reset_col:
    st.write("")
    st.write("")
    if st.button("Start over", use_container_width=True):
        state.reset()
        st.rerun()


def _render_stepper() -> None:
    steps = list(WizardStep)
    current_index = steps.index(state.step)
    dots = []
    for index, step in enumerate(steps):
        css_class = "done" if index < current_index else ("active" if index == current_index else "")
        dots.append(f'<div class="pdf2md-step-dot {css_class}">{"✓" if index < current_index else index + 1}</div>')
        if index < len(steps) - 1:
            line_class = "done" if index < current_index else ""
            dots.append(f'<div class="pdf2md-step-line {line_class}"></div>')
    st.markdown(f'<div class="pdf2md-stepper">{"".join(dots)}</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="pdf2md-step-caption">Step {current_index + 1} of {len(steps)} '
        f"— <b>{_STEP_LABELS[state.step]}</b></div>",
        unsafe_allow_html=True,
    )


_render_stepper()


def _github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN")


@st.cache_resource(show_spinner=False)
def _github_client(token: str) -> Github:
    return Github(auth=Auth.Token(token))


@st.cache_data(ttl=3600, show_spinner="Loading subject list…")
def _cached_repo_list(token: str):
    return list_textbook_repos(_github_client(token))


@st.cache_resource(show_spinner=False)
def _cached_converter(opts: ConversionOptions):
    converter = get_converter(opts)
    warm_up(converter)
    return converter


def _advance() -> None:
    state.step = next_step(state.step)
    st.rerun()


def _render_select_repo() -> None:
    st.header("Which subject is this?")
    token = _github_token()
    if not token:
        logger.error("GITHUB_TOKEN is not set -- cannot list subjects or send submissions.")
        st.error("This tool isn't fully set up yet. Please contact your administrator.")
        st.stop()

    repos = _cached_repo_list(token)
    if not repos:
        st.error("We couldn't load the subject list right now. Please try again shortly.")
        st.stop()

    labels_by_name = {r.name: _pretty_subject_label(r.name) for r in repos}
    names = [r.name for r in repos]
    default_index = names.index(state.selected_repo.name) if state.selected_repo and state.selected_repo.name in names else 0
    choice = st.selectbox("Subject", names, index=default_index, format_func=lambda n: labels_by_name[n])
    selected = next(r for r in repos if r.name == choice)

    if st.button("↻ Refresh list", type="secondary"):
        _cached_repo_list.clear()
        st.rerun()

    if st.button("Next", type="primary"):
        state.selected_repo = selected
        _advance()


def _render_upload() -> None:
    st.header("Upload the book")
    st.caption(f"Subject: {_pretty_subject_label(state.selected_repo.name)}")

    uploaded = st.file_uploader("Textbook PDF", type=["pdf"])
    if uploaded is not None:
        st.session_state["wizard_pdf_bytes"] = uploaded.getvalue()
        state.pdf_name = uploaded.name

    ready = "wizard_pdf_bytes" in st.session_state
    if st.button("Next", type="primary", disabled=not ready):
        _advance()


def _run_parse() -> None:
    converter = _cached_converter(_PARSE_OPTS)

    with tempfile.TemporaryDirectory(prefix="pdf2md_wizard_") as image_dir:
        pdf_bytes = st.session_state["wizard_pdf_bytes"]
        source = DocumentStream(name=state.pdf_name, stream=io.BytesIO(pdf_bytes))
        outcome = convert_pdf_to_markdown(converter, source, state.pdf_name, _PARSE_OPTS, image_dir=Path(image_dir))

        if not outcome.ok:
            logger.error("parse failed for %s: %s", state.pdf_name, outcome.error)
            state.parse_error = "We couldn't read that file. Please check it's a valid PDF and try again."
            state.parsed = True
            return

        st.session_state["wizard_full_markdown"] = outcome.markdown
        state.conversion_confidence = outcome.confidence
        # Docling's heading level for a chapter title varies per document
        # (observed both H1 and H2 for a document's biggest heading,
        # depending on font/layout) -- try a few levels rather than assume.
        for level in (1, 2, 3):
            chapters = detect_chapters(outcome.markdown, heading_level=level)
            if chapters:
                st.session_state["wizard_heading_level"] = level
                break
        else:
            chapters = []
            st.session_state["wizard_heading_level"] = 1

        state.chapters = chapters
        state.parsed = True
        state.parse_error = None


def _render_parse() -> None:
    st.header("Reading the book")

    if not state.parsed:
        with st.spinner("Reading the book and finding chapters (this can take a while for a whole textbook)…"):
            _run_parse()

    if state.parse_error:
        st.error(state.parse_error)
        if st.button("Try again"):
            state.parsed = False
            state.parse_error = None
            st.rerun()
        return

    if not state.chapters:
        st.warning(
            "We couldn't automatically find chapters in this file. You can still continue and "
            "adjust how chapters are found in the next step, or go back and check the file."
        )
    else:
        st.success(f"Found {len(state.chapters)} chapter(s).")
        for chapter in state.chapters:
            picture_note = f", {len(chapter.images)} picture(s)" if chapter.images else ""
            st.write(f"- **{chapter.title}**{picture_note}")

    if st.button("Next", type="primary"):
        _advance()


def _render_review_chapters() -> None:
    st.header("Check the chapters")
    st.caption("Rename, reorder, combine, or split chapters before continuing.")

    with st.expander(
        "Chapters look wrong? Try finding them differently",
        expanded=not state.chapters,
    ):
        level = st.slider(
            "How the book's headings are read as chapter breaks", 1, 6,
            st.session_state.get("wizard_heading_level", 1),
        )
        if st.button("Look again"):
            full_markdown = st.session_state.get("wizard_full_markdown", "")
            state.chapters = detect_chapters(full_markdown, heading_level=level)
            st.rerun()

    merge_selection: list[int] = []
    for idx, chapter in enumerate(state.chapters):
        with st.container(border=True):
            cols = st.columns([3, 1, 2])
            chapter.title = cols[0].text_input("Chapter name", chapter.title, key=f"title-{chapter.id}")
            chapter.slug = slugify_title(chapter.title)  # kept in sync silently, never shown
            chapter.weight = cols[1].number_input("Order", value=chapter.weight, step=1, key=f"weight-{chapter.id}")
            if cols[2].checkbox("Combine with next", key=f"merge-{chapter.id}"):
                merge_selection.append(idx)
            chapter.summary = st.text_area("Short description", chapter.summary, key=f"summary-{chapter.id}", height=80)
            st.caption(f"{len(chapter.images)} picture(s)")

            num_lines = len(chapter.en_markdown.splitlines())
            with st.expander("Split this chapter into two"):
                split_line = st.number_input(
                    "Split before line",
                    min_value=1, max_value=max(num_lines - 1, 1), value=min(2, max(num_lines - 1, 1)),
                    key=f"split-line-{chapter.id}",
                )
                if st.button("Split here", key=f"split-btn-{chapter.id}"):
                    first, second = split_chapter(chapter, split_line)
                    state.chapters = state.chapters[:idx] + [first, second] + state.chapters[idx + 1 :]
                    renumber_weights(state.chapters)
                    st.rerun()

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Combine selected chapters", disabled=len(merge_selection) < 2):
            state.chapters = merge_chapters(state.chapters, merge_selection)
            st.rerun()
    with col2:
        if st.button("Fix chapter order (1, 2, 3…)"):
            renumber_weights(state.chapters)
            st.rerun()

    if st.button("Next", type="primary", disabled=not state.chapters):
        state.active_chapter_idx = 0
        _advance()


def _render_translate_review() -> None:
    st.header("Check the Tamil translation")
    total = len(state.chapters)
    idx = state.active_chapter_idx
    chapter = state.chapters[idx]
    st.caption(f"Chapter {idx + 1} of {total}: **{chapter.title}**")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("English")
        chapter.en_markdown = markdown_editor(chapter.en_markdown, key=f"en-{chapter.id}", height=400)
    with col2:
        st.subheader("Tamil (check and fix if needed)")
        if st.button("Auto-translate this chapter", key=f"translate-{chapter.id}"):
            progress = st.progress(0.0)

            def _on_block_done(done: int, total_blocks: int) -> None:
                progress.progress(done / max(total_blocks, 1))

            with st.spinner("Translating…"):
                outcome = translate_chapter_to_tamil(chapter.en_markdown, on_block_done=_on_block_done)
            if outcome.ok:
                chapter.ta_markdown = outcome.ta_markdown
                # The text_area below already has widget state under this
                # key once rendered once; setting `value=` alone wouldn't
                # update it on rerun -- update the widget's own state, then
                # rerun.
                st.session_state[f"ta-{chapter.id}"] = outcome.ta_markdown
                st.rerun()
            else:
                logger.error("translation failed for chapter %s: %s", chapter.title, outcome.error)
                st.error("Auto-translation didn't work this time. You can type the Tamil text in below instead.")

        chapter.ta_markdown = st.text_area(
            "Tamil text",
            chapter.ta_markdown or "",
            height=360,
            key=f"ta-{chapter.id}",
        )

    nav_cols = st.columns(3)
    with nav_cols[0]:
        if st.button("← Previous chapter", disabled=idx == 0):
            state.active_chapter_idx -= 1
            st.rerun()
    with nav_cols[1]:
        if st.button("Next chapter →", disabled=idx >= total - 1):
            state.active_chapter_idx += 1
            st.rerun()
    with nav_cols[2]:
        if st.button("Continue", type="primary"):
            _advance()


def _render_generate_mcqs() -> None:
    st.header("Practice questions")
    st.caption(
        "We can draft multiple-choice practice questions for each chapter. "
        "Review and edit them (or skip a chapter) before continuing."
    )

    for chapter in state.chapters:
        with st.container(border=True):
            st.subheader(chapter.title)
            count = st.slider(
                "How many questions?", min_value=3, max_value=10, value=5, key=f"mcq-count-{chapter.id}",
            )
            button_label = "Generate questions" if not chapter.mcqs else "Regenerate questions (replaces the ones below)"
            if st.button(button_label, key=f"mcq-gen-{chapter.id}"):
                with st.spinner("Writing questions…"):
                    outcome = generate_mcqs_for_chapter(chapter, count=count)
                if outcome.ok:
                    chapter.mcqs = outcome.mcqs
                    st.rerun()
                else:
                    logger.error("mcq generation failed for chapter %s: %s", chapter.title, outcome.error)
                    st.error("Couldn't write questions this time. Please try again.")

            remove_idx: int | None = None
            for q_idx, mcq in enumerate(chapter.mcqs):
                widget_prefix = f"{chapter.id}-{mcq.id}"
                with st.expander(f"Q{q_idx + 1}: {mcq.question[:70]}", expanded=False):
                    mcq.question = st.text_area("Question", mcq.question, key=f"mcq-q-{widget_prefix}")

                    all_options = mcq.choices + mcq.answers
                    edited_options = [
                        st.text_input(f"Option {i + 1}", opt, key=f"mcq-opt-{widget_prefix}-{i}")
                        for i, opt in enumerate(all_options)
                    ]
                    correct_idx = st.radio(
                        "Correct answer",
                        options=list(range(len(edited_options))),
                        index=len(mcq.choices),
                        format_func=lambda i: edited_options[i] or f"Option {i + 1}",
                        key=f"mcq-correct-{widget_prefix}",
                        horizontal=True,
                    )
                    mcq.answers = [edited_options[correct_idx]]
                    mcq.choices = [opt for i, opt in enumerate(edited_options) if i != correct_idx]

                    mcq.explanation = st.text_area("Explanation", mcq.explanation, key=f"mcq-exp-{widget_prefix}")
                    mcq.complexity = st.selectbox(
                        "Difficulty", ["E", "M", "H"],
                        index=["E", "M", "H"].index(mcq.complexity) if mcq.complexity in ("E", "M", "H") else 1,
                        key=f"mcq-complexity-{widget_prefix}",
                    )

                    if mcq.ta_question:
                        st.caption(f"Tamil: {mcq.ta_question}")
                    if st.button("Translate to Tamil", key=f"mcq-translate-{widget_prefix}"):
                        try:
                            with st.spinner("Translating…"):
                                mcq.ta_question = translate_text(mcq.question)
                                mcq.ta_choices = [translate_text(c) for c in mcq.choices]
                                mcq.ta_answers = [translate_text(a) for a in mcq.answers]
                                mcq.ta_explanation = translate_text(mcq.explanation)
                            st.rerun()
                        except requests.RequestException as exc:
                            logger.error("mcq translation failed for %s: %s", mcq.id, exc)
                            st.error("Translation didn't work this time. Please try again.")

                    if st.button("Remove this question", key=f"mcq-remove-{widget_prefix}"):
                        remove_idx = q_idx

            if remove_idx is not None:
                chapter.mcqs.pop(remove_idx)
                st.rerun()

    if st.button("Next", type="primary"):
        _advance()


def _run_quality_check() -> None:
    with st.spinner("Checking quality…"):
        state.quality_report = compute_quality_report(state.chapters, state.conversion_confidence)


def _render_quality_check() -> None:
    st.header("Quality check")
    st.caption(
        "We check the extracted text and translations before this is sent to your team. "
        "Problems shown here must be fixed before you can continue."
    )

    if state.quality_report is None:
        _run_quality_check()
    report = state.quality_report

    if report.passed:
        st.success("Everything looks good.")
    else:
        st.error("We found some problems that need fixing before this can be sent.")

    for issue in report.document_issues:
        (st.error if issue.severity == "critical" else st.warning)(issue.message)

    for chapter_result in report.chapters:
        with st.container(border=True):
            status = "✅" if chapter_result.passed else "❌"
            st.write(f"{status} **{chapter_result.chapter_title}**")
            if chapter_result.issues:
                for issue in chapter_result.issues:
                    (st.error if issue.severity == "critical" else st.warning)(issue.message)
            else:
                st.caption("No problems found.")

    if st.button("Re-check"):
        state.quality_report = None
        st.rerun()

    if st.button("Next", type="primary", disabled=not report.passed):
        _advance()


def _ensure_questions_repo_resolved(client, repo_ref) -> None:
    """Looked up once per session (not on every rerun): the sibling
    `_questions` repo may not exist for every subject, and discovering its
    existing file-layout convention costs a couple of extra API calls."""
    if state.questions_repo_checked:
        return
    state.questions_repo_checked = True
    state.questions_repo = resolve_questions_repo(client, repo_ref)
    if state.questions_repo is not None:
        questions_repo_obj = client.get_repo(state.questions_repo.full_name)
        discovered = discover_questions_path_convention(questions_repo_obj)
        prefix, used_fallback = resolve_mcq_path_prefix(discovered, repo_ref.name)
        state.questions_path_prefix = prefix
        state.questions_path_is_fallback = used_fallback


def _render_create_pr() -> None:
    st.header("Send to your team")
    repo_ref = state.selected_repo
    pretty = _pretty_subject_label(repo_ref.name)
    st.caption(f"Subject: {pretty}")

    missing_ta = [c.title for c in state.chapters if not c.ta_markdown]
    if missing_ta:
        st.warning(f"These chapters don't have a Tamil version yet: {', '.join(missing_ta)}")

    has_mcqs = any(chapter.mcqs for chapter in state.chapters)
    if has_mcqs:
        client = _github_client(_github_token())
        _ensure_questions_repo_resolved(client, repo_ref)
        if state.questions_repo is None:
            st.info("We couldn't find a practice-questions repo for this subject -- only the book content will be sent.")
        elif state.questions_path_is_fallback:
            st.info(
                "This subject's practice-questions repo doesn't have an established layout yet -- "
                "we'll use a default one, and flag it for your reviewers to double check."
            )

    submission_title = st.text_input("Title for your submission", value=f"New chapters for {pretty}")
    notes = st.text_area(
        "Notes for your reviewers (optional)",
        value="\n".join(f"- {c.title}" for c in state.chapters),
        height=120,
    )

    quality_gate_passed = state.quality_report is not None and state.quality_report.passed

    preview_only = st.checkbox("Just show me a preview (don't send yet)", value=state.pr_dry_run)
    state.pr_dry_run = preview_only

    confirm_text = ""
    if not preview_only:
        st.warning("This will really send the book to your team for review.")
        confirm_text = st.text_input('Type "SEND" to confirm', value="")

    can_submit = quality_gate_passed and (preview_only or confirm_text.strip().upper() == "SEND")
    button_label = "Show me a preview" if preview_only else "Send for review"
    if st.button(button_label, type="primary", disabled=not can_submit):
        client = _github_client(_github_token())
        repo = client.get_repo(repo_ref.full_name)
        with st.spinner("Working on it…"):
            result = create_chapter_pr(
                repo, state.chapters, pr_title=submission_title, pr_body=notes, dry_run=preview_only,
            )
            questions_result = None
            if has_mcqs and state.questions_repo is not None:
                questions_repo = client.get_repo(state.questions_repo.full_name)
                questions_body = notes
                if state.questions_path_is_fallback:
                    questions_body += "\n\n(Used a default file layout -- please check placement.)"
                questions_result = create_questions_pr(
                    questions_repo, state.chapters, state.questions_path_prefix,
                    pr_title=f"{submission_title} — practice questions", pr_body=questions_body,
                    dry_run=preview_only,
                )
        state.pr_result = result
        state.questions_pr_result = questions_result
        if not preview_only and result.ok and (questions_result is None or questions_result.ok):
            _advance()

    if state.pr_result and state.pr_result.dry_run:
        result = state.pr_result
        st.divider()
        st.subheader("Preview")
        if not result.ok:
            st.error("Couldn't build a preview right now. Please try again.")
        else:
            st.write(f"For **{pretty}**, this will add {len(state.chapters)} chapter(s):")
            for chapter in state.chapters:
                bits = ["English", "Tamil" if chapter.ta_markdown else "Tamil (missing)"]
                if chapter.images:
                    bits.append(f"{len(chapter.images)} picture(s)")
                if chapter.mcqs:
                    bits.append(f"{len(chapter.mcqs)} practice question(s)")
                st.write(f"- **{chapter.title}** — " + ", ".join(bits))
            if state.questions_pr_result and state.questions_pr_result.ok:
                st.caption(f"Practice questions will go to a separate submission ({len(state.questions_pr_result.tree_entries)} file(s)).")
            st.caption('Nothing has been sent yet. Uncheck the preview box above and type "SEND" to actually send it.')


def _render_done() -> None:
    st.header("All done!")
    result = state.pr_result
    if result is None or not result.ok:
        if result and result.error:
            logger.error("submission failed: %s", result.error)
        st.error("Something went wrong sending this book. Please try again or contact your administrator.")
    else:
        st.success("Your book has been sent to the team for review.")
        if result.pr_url:
            st.markdown(f"[Track this submission]({result.pr_url})")
        questions_result = state.questions_pr_result
        if questions_result is not None:
            if questions_result.ok and questions_result.pr_url:
                st.markdown(f"[Track the practice questions]({questions_result.pr_url})")
            elif not questions_result.ok:
                logger.error("questions submission failed: %s", questions_result.error)
                st.warning("The book was sent, but the practice questions couldn't be sent. Please contact your administrator.")

    if st.button("Start over"):
        state.reset()
        st.rerun()


_RENDERERS = {
    WizardStep.SELECT_REPO: _render_select_repo,
    WizardStep.UPLOAD: _render_upload,
    WizardStep.PARSE: _render_parse,
    WizardStep.REVIEW_CHAPTERS: _render_review_chapters,
    WizardStep.TRANSLATE_REVIEW: _render_translate_review,
    WizardStep.GENERATE_MCQS: _render_generate_mcqs,
    WizardStep.QUALITY_CHECK: _render_quality_check,
    WizardStep.CREATE_PR: _render_create_pr,
    WizardStep.DONE: _render_done,
}
_RENDERERS[state.step]()
