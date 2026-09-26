from pdf2md.chapters import Chapter
from pdf2md.wizard_state import WizardState, WizardStep, next_step


def test_next_step_advances_through_all_steps():
    step = WizardStep.SELECT_REPO
    seen = [step]
    for _ in range(len(WizardStep)):
        step = next_step(step)
        seen.append(step)
    assert seen[-1] == WizardStep.DONE


def test_next_step_stays_at_done():
    assert next_step(WizardStep.DONE) == WizardStep.DONE


def test_reset_clears_progress():
    state = WizardState()
    state.step = WizardStep.CREATE_PR
    state.pdf_name = "book.pdf"
    state.chapters = [Chapter(id="1", title="X", slug="x", weight=1, summary="", en_markdown="")]
    state.active_chapter_idx = 1

    state.reset()

    assert state.step == WizardStep.SELECT_REPO
    assert state.pdf_name is None
    assert state.chapters == []
    assert state.active_chapter_idx == 0
