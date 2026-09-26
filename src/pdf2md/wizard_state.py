"""Plain state schema for the publish-to-GitHub wizard. No Streamlit
dependency, so the step-transition logic is unit-testable on its own; the
Streamlit page just holds one `WizardState` instance in `st.session_state`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pdf2md.chapters import Chapter
from pdf2md.github_pr import PrResult, RepoRef


class WizardStep(str, Enum):
    SELECT_REPO = "select_repo"
    UPLOAD = "upload"
    PARSE = "parse"
    REVIEW_CHAPTERS = "review_chapters"
    TRANSLATE_REVIEW = "translate_review"
    CREATE_PR = "create_pr"
    DONE = "done"


_STEP_ORDER = list(WizardStep)


def next_step(step: WizardStep) -> WizardStep:
    index = _STEP_ORDER.index(step)
    if index + 1 >= len(_STEP_ORDER):
        return step
    return _STEP_ORDER[index + 1]


@dataclass
class WizardState:
    step: WizardStep = WizardStep.SELECT_REPO
    selected_repo: RepoRef | None = None
    pdf_name: str | None = None
    parsed: bool = False
    parse_error: str | None = None
    chapters: list[Chapter] = field(default_factory=list)
    active_chapter_idx: int = 0
    pr_dry_run: bool = True
    pr_result: PrResult | None = None

    def reset(self) -> None:
        """Start over -- used by the wizard's "Start over" control."""
        self.step = WizardStep.SELECT_REPO
        self.selected_repo = None
        self.pdf_name = None
        self.parsed = False
        self.parse_error = None
        self.chapters = []
        self.active_chapter_idx = 0
        self.pr_dry_run = True
        self.pr_result = None
