# pdf2md

A simple local Streamlit app that converts PDFs to Markdown using [Docling](https://docling-project.github.io/docling).

**Phase 1 (this version):** fully local, no LLM or cloud calls. Docling's native
layout analysis, OCR, and table-structure recognition models do all the work.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
streamlit run app.py
```

Then open the URL Streamlit prints (usually http://localhost:8501).

> **Note:** the first conversion in a fresh environment downloads Docling's
> layout-detection and table-structure model weights from Hugging Face
> (a few hundred MB). This requires internet access once; after that the
> models are cached locally (`~/.cache/huggingface`) and conversion works
> offline.

## Usage

1. Upload one or more PDFs.
2. Toggle OCR / table-structure detection in the sidebar if needed.
3. Click **Convert**.
4. Preview the Markdown output and download the `.md` file per document.
5. Each result shows how long it took to parse ("Parsed in Xs"); the same
   timing is also logged to the terminal running `streamlit run` (start/end
   timestamps + duration), so you can track performance over time or across
   machines.

## Performance

Defaults are tuned for speed on the common case (born-digital PDFs, not
scans):

- **OCR is off by default.** OCR is only needed for scanned/image-only
  pages — PDFs that already have an embedded text layer get that text for
  free without it, so leaving OCR on for them just costs time for nothing.
  Turn it on in the sidebar for scanned documents.
- **Table recognition defaults to "Fast mode"** (`TableFormerMode.FAST`),
  trading a bit of cell-detection accuracy for a large speed win. Turn it
  off for maximum table fidelity.
- **Hardware acceleration and threading are auto-tuned**: `accelerator_options`
  uses `AcceleratorDevice.AUTO` (picks Apple MPS / NVIDIA CUDA over CPU when
  available) and `num_threads` defaults to the machine's logical core count
  instead of Docling's default of 4.
- The `DocumentConverter` (and its loaded models) is cached per settings
  combination via `st.cache_resource`, so only the *first* build after a
  settings change pays model-loading cost — repeat conversions with the
  same settings reuse the already-loaded models.
- **Model loading is warmed up eagerly**, not on the first real document.
  Docling loads model weights lazily on first use, and the very first
  inference call also pays a one-time device warm-up cost (e.g. Apple MPS
  kernel compilation — measured ~4s on first touch, vs ~0.25s on every call
  after that). `pdf2md.converter.warm_up()` forces both of these to happen
  against a tiny throwaway page as soon as the app loads or an option
  changes (shown as a "Warming up local models…" spinner), so the "Parsed
  in Xs" time on your actual document reflects real processing time, not a
  one-time setup tax. On this machine that took the *first* real document's
  reported time from ~6s down to ~0.3s.

## Testing

```bash
pytest
```

`tests/test_converter.py` exercises the core conversion function directly
(no Streamlit involved) against a small PDF fixture. Since this is
fundamentally a UI app, also do a manual pass: run the app, upload a real
PDF (including a scanned one and one with a table), and confirm the preview,
OCR, table rendering, and download button all work as expected.

## Roadmap: Phase 2 (not yet implemented)

A future phase will add optional enrichment via a free **local LLM through
Ollama** — e.g. picture description, or a VLM-based parsing pipeline as an
alternative to the default layout pipeline. The conversion module
(`src/pdf2md/converter.py`) is structured so this can be added as new,
defaulted fields on `ConversionOptions` plus a new branch in
`build_pipeline_options`, without changing the Streamlit UI's call sites or
altering Phase 1 behavior.

## Known limitations

- OCR quality/language coverage depends on the bundled RapidOCR engine
  (Docling's default). It can be swapped for EasyOCR or Tesseract later if
  needed — a one-line change in `converter.py`.
- Large or heavily scanned PDFs will be slow on CPU-only machines, since
  Docling's layout/table models run via PyTorch.
