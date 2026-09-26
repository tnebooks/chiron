# pdf2md

A single-flow Streamlit app for onboarding a new subject textbook: upload a
PDF, check the auto-detected chapters, check the Tamil translation, then
send it to the team for review — which under the hood raises a GitHub PR
into the matching `tnebooks/<grade>-<subject>` repo (e.g.
`tnebooks/12th-maths`).

**The app's own UI is deliberately non-technical** — the audience is
non-technical staff, so it never says "GitHub", "PR", "repo", "branch",
"commit", "OCR", "Ollama", "markdown", or "slug" anywhere a user can see it.
Those are exactly what's happening underneath, and that's spelled out in
this README and in code comments for developer/maintainer sanity — just not
in the UI. See `app.py`'s module docstring for the same note in context.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# edit .env: set GITHUB_TOKEN to a PAT (repo scope) for a bot/team account
# with direct push access to tnebooks org repos (no forking needed).
```

Optional, for the Tamil-translation step: run [Ollama](https://ollama.com)
locally and `ollama pull llama3.1` (or set `PDF2MD_OLLAMA_MODEL` to a
different model). The app works without Ollama running — that step just
shows a friendly message and a human can type the Tamil text in directly
instead.

## Run

```bash
streamlit run app.py
```

> **Note:** the first PDF processed in a fresh environment downloads
> Docling's layout-detection and table-structure model weights from Hugging
> Face (a few hundred MB, needs internet once; cached under
> `~/.cache/huggingface` after that).

## Flow

Choose subject → upload the book (PDF) → the app reads it and finds
chapters → check/adjust chapters (rename, reorder, combine, split) → check
the Tamil translation per chapter (auto-drafted via Ollama, editable) →
send to the team for review. A **preview** option on the last step computes
exactly what would be added (which chapters, English/Tamil, how many
pictures) without sending anything — use it before ever sending for real. A
real send requires typing `SEND` to confirm.

## Look & feel

Streamlit's default chrome (header bar, Deploy/menu buttons, default
Source Sans font) is deliberately overridden — see the `_CUSTOM_CSS` block
near the top of `app.py`: a fixed light theme (`.streamlit/config.toml`
`[theme]`), the Inter webfont, a constrained centered column instead of a
full-width dashboard, softer rounded corners/shadows on buttons/inputs/
cards, and a custom horizontal step indicator (`_render_stepper`) in place
of a sidebar. One gotcha if you touch that CSS: Streamlit's expander/
toolbar arrows are ligature text (e.g. `"keyboard_arrow_right"`) rendered
through the `Material Symbols Rounded` icon font — a blanket `font-family`
override will turn them into literal visible text unless
`[data-testid="stIconMaterial"]` is explicitly excluded (it already is).

## Architecture

- `app.py` — the entire UI (single page, no Streamlit multipage nav — there
  used to be a separate "convert only" page and a separate "publish"
  wizard; they're merged into one flow now so there's only one thing for a
  user to find and follow).
- `src/pdf2md/converter.py` — Docling wrapper: `ConversionOptions`,
  `get_converter`/`warm_up` (model loading + device warm-up done eagerly,
  not on the first real document), `convert_pdf_to_markdown` (optionally
  exporting extracted figures via `image_dir`).
- `src/pdf2md/chapters.py` — splits a whole-document markdown export into
  chapters on heading boundaries (tries a few heading levels automatically,
  since Docling's assigned level for a "chapter title" varies per document
  — observed both H1 and H2 depending on source PDF formatting), plus
  merge/split/renumber for the review step. Image links are rewritten from
  Docling's absolute paths to bare filenames, with bytes captured
  alongside.
- `src/pdf2md/translation.py` — calls a local Ollama model to translate one
  chapter's body to Tamil, block by block. Image-only, table, and math-only
  blocks are passed through byte-identical and never sent to the model —
  this is what protects structure from being "helpfully" reworded.
- `src/pdf2md/hugo_frontmatter.py` — builds each chapter's `_index.md`
  front matter. Verified **byte-exact** against a real file fetched live
  from `tnebooks/12th-maths` (see `tests/fixtures/real_index_en.md`).
- `src/pdf2md/github_pr.py` — lists/filters tnebooks org repos (regex
  `^\d{1,2}(st|nd|rd|th)-[a-z]+$`, verified against the live org's actual
  18-repo listing — see `tests/fixtures/tnebooks_repos.json`), and raises
  one atomic multi-file commit + PR via PyGithub's Git Data API
  (blob→tree→commit→ref→PR) rather than the Contents API (one commit per
  file, non-atomic) or shelling out to `git`. `dry_run=True` computes
  everything and returns before any write call — the code path behind the
  UI's "preview" option.
- `src/pdf2md/wizard_state.py` — the step machine (`WizardStep` enum +
  `WizardState` dataclass), no Streamlit dependency, unit-tested on its
  own. `app.py` maps each step to a *separate*, non-technical display label
  for the sidebar — the enum names themselves stay technical on purpose.

## Testing

```bash
pytest
```

34 tests cover every module above except `app.py` itself (Streamlit UI —
verified manually in a browser; see the "Known limitations" note on
`create_chapter_pr`'s real-send path below). Notable choices:
- `test_github_pr.py` mocks PyGithub's `Repository`/`Github` objects
  directly with `unittest.mock.MagicMock` rather than mocking HTTP —
  PyGithub doesn't use `requests` internally, so the `responses` library
  (used for `translation.py`'s tests) can't intercept its calls.
- `test_hugo_frontmatter.py` asserts byte-for-byte against a real fetched
  fixture, not a guessed format.

**Real-send verification**: a real (non-dry-run, non-preview) send pushes a
branch and opens a real PR against a live, public educational org's repo —
this was deliberately never exercised end-to-end against the real
`tnebooks` org (only the preview/dry-run path was), since real chapter
weights are already fully occupied in at least one sampled repo and a
stray real send has real consequences. Before relying on the real-send
path in production, do one manual pass against a disposable personal test
repo seeded with the same `content.en`/`content.ta`/`config.toml` skeleton.

## Performance

Fixed, not exposed as UI toggles (a non-technical user has no basis to
choose them) — see `_PARSE_OPTS` in `app.py`:
- OCR is off (assumes born-digital source PDFs, the common case for this
  org); table recognition uses Docling's `TableFormerMode.FAST`.
- `AcceleratorDevice.AUTO` (picks Apple MPS / NVIDIA CUDA over CPU when
  available) and `num_threads` set to the machine's logical core count.
- The `DocumentConverter` is built and warmed up once via `st.cache_resource`
  (`_cached_converter`), not per upload — Docling loads model weights
  lazily on first use and pays a one-time device warm-up cost (e.g. Apple
  MPS kernel compilation, measured ~4s on first touch vs ~0.25s after) that
  would otherwise land inside a user's first real upload.

## Known limitations

- No persistence: a server restart mid-flow loses progress (in-memory
  `st.session_state` only).
- Chapter-order collisions against the *live* target repo aren't
  auto-detected — a real duplicate/overlapping chapter order needs to be
  caught by a human reviewer on the PR itself.
- Images are reused byte-identical across the English and Tamil trees
  (real tnebooks repos don't follow any consistent naming convention
  between the two, so this is simplest and always correct).
- OCR quality/language coverage depends on the bundled RapidOCR engine; can
  be swapped for EasyOCR/Tesseract later via `converter.py`.
