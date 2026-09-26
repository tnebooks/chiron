"""Small pure helpers for the Streamlit UI."""

from __future__ import annotations

import io

from docling_core.types.io import DocumentStream


def to_document_stream(uploaded_file) -> DocumentStream:
    """Wrap a Streamlit UploadedFile as a Docling DocumentStream, in-memory."""
    return DocumentStream(name=uploaded_file.name, stream=io.BytesIO(uploaded_file.getvalue()))


def markdown_filename(source_filename: str) -> str:
    stem = source_filename.rsplit(".", 1)[0] if "." in source_filename else source_filename
    return f"{stem}.md"
