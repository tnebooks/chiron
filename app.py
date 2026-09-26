"""Streamlit UI for the PDF -> Markdown converter.

UI-only: no Docling internals here beyond calling into pdf2md.converter.
"""

import logging

import streamlit as st

from pdf2md.converter import (
    ConversionOptions,
    TableFormerMode,
    convert_pdf_to_markdown,
    get_converter,
    warm_up,
)
from pdf2md.ui_helpers import markdown_filename, to_document_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024

st.set_page_config(page_title="PDF to Markdown", layout="wide")
st.title("PDF → Markdown Converter")
st.caption("Powered by Docling — runs fully locally, no LLM or cloud calls.")

with st.sidebar:
    st.subheader("Options")
    do_ocr = st.checkbox(
        "Enable OCR (for scanned PDFs)",
        value=False,
        help="Leave off for born-digital PDFs (already have embedded text) — OCR is slow "
        "and only needed for scanned/image-only pages.",
    )
    do_table_structure = st.checkbox("Detect table structure", value=True)
    fast_tables = st.checkbox(
        "Fast mode for tables",
        value=True,
        help="Trades some table-cell accuracy for a large speed win. Turn off for max "
        "table fidelity on documents where that matters most.",
        disabled=not do_table_structure,
    )
    st.checkbox("Enrich with local LLM (Ollama) — coming soon", value=False, disabled=True)

opts = ConversionOptions(
    do_ocr=do_ocr,
    do_table_structure=do_table_structure,
    table_mode=TableFormerMode.FAST if fast_tables else TableFormerMode.ACCURATE,
)


@st.cache_resource(show_spinner="Warming up local models…")
def cached_converter(opts: ConversionOptions):
    converter = get_converter(opts)
    warm_up(converter)
    return converter


# Trigger (and cache) model warm-up as soon as the options are known, rather
# than waiting for the user's first upload+click — that first conversion
# used to eat several extra seconds of one-time model-load / device
# (e.g. MPS kernel compile) cost that has nothing to do with the document
# itself.
converter = cached_converter(opts)

uploaded_files = st.file_uploader(
    "Upload PDF(s)", type=["pdf"], accept_multiple_files=True,
)

if uploaded_files and st.button("Convert", type="primary"):
    for uf in uploaded_files:
        st.divider()
        st.subheader(uf.name)

        if uf.size > MAX_FILE_SIZE_BYTES:
            st.warning(
                f"Skipped: file is {uf.size / (1024 * 1024):.1f} MB, "
                f"exceeds the {MAX_FILE_SIZE_BYTES / (1024 * 1024):.0f} MB limit."
            )
            continue

        with st.spinner(f"Converting {uf.name}..."):
            source = to_document_stream(uf)
            outcome = convert_pdf_to_markdown(converter, source, uf.name, opts)

        st.caption(f"Parsed in {outcome.duration_seconds:.2f}s ({outcome.started_at} → {outcome.ended_at})")

        if outcome.ok:
            with st.expander("Markdown preview", expanded=True):
                st.markdown(outcome.markdown)
            st.download_button(
                label=f"Download {markdown_filename(uf.name)}",
                data=outcome.markdown,
                file_name=markdown_filename(uf.name),
                mime="text/markdown",
                key=f"dl-{uf.name}",
            )
        else:
            st.error(f"Failed to convert {uf.name}: {outcome.error}")
