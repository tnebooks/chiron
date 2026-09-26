"""Docling-backed PDF -> Markdown conversion.

All Docling-specific setup lives here, isolated from the Streamlit UI.
Phase 1 only touches OCR / table-structure options. Phase 2 (a future,
optional local-LLM enrichment pass via Ollama) can add new fields to
ConversionOptions and a new branch in build_pipeline_options without
changing convert_pdf_to_markdown's signature or any caller.
"""

from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Union

from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    AcceleratorOptions,
    PdfPipelineOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ImageRefMode
from docling_core.types.io import DocumentStream

logger = logging.getLogger(__name__)

# Logical core count on this machine; Docling's default (4) leaves cores idle
# on most modern laptops/desktops.
_DEFAULT_NUM_THREADS = max(1, os.cpu_count() or 4)


@dataclass(frozen=True)
class ConversionOptions:
    # OCR only matters for scanned/image-only pages -- born-digital PDFs
    # already have an embedded text layer, so leaving OCR on for them just
    # burns time. Default off; the UI lets users flip it on for scans.
    do_ocr: bool = False
    do_table_structure: bool = True
    # FAST trades some table-cell accuracy for a large speed win; ACCURATE
    # is still available for documents where table fidelity matters most.
    table_mode: TableFormerMode = TableFormerMode.FAST
    num_threads: int = _DEFAULT_NUM_THREADS
    max_num_pages: int = 500
    max_file_size_bytes: int = 100 * 1024 * 1024
    # Off by default -- the plain Phase 1 app never needs figures extracted.
    # The publish-to-GitHub wizard turns this on so chapter markdown can
    # reference real image files instead of placeholders.
    generate_picture_images: bool = False


@dataclass
class ConversionOutcome:
    ok: bool
    markdown: str | None
    error: str | None
    filename: str
    started_at: str
    ended_at: str
    duration_seconds: float


def build_pipeline_options(opts: ConversionOptions) -> PdfPipelineOptions:
    pipeline_options = PdfPipelineOptions(
        do_ocr=opts.do_ocr,
        do_table_structure=opts.do_table_structure,
    )
    pipeline_options.table_structure_options.mode = opts.table_mode
    pipeline_options.generate_picture_images = opts.generate_picture_images
    pipeline_options.accelerator_options = AcceleratorOptions(
        num_threads=opts.num_threads,
        device=AcceleratorDevice.AUTO,  # picks CUDA/MPS over CPU when available
    )
    return pipeline_options


def get_converter(opts: ConversionOptions) -> DocumentConverter:
    pipeline_options = build_pipeline_options(opts)
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def _minimal_pdf_bytes() -> bytes:
    """A tiny valid one-page PDF, used only to force model loading and the
    one-time device warm-up (e.g. MPS kernel compilation) ahead of time."""
    content = b"BT /F1 12 Tf 72 720 Td (warm up) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref_offset = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objects) + 1,
        xref_offset,
    )
    return pdf


_WARMUP_PDF_BYTES = _minimal_pdf_bytes()


def warm_up(converter: DocumentConverter) -> float:
    """Force model loading and the first-forward-pass device warm-up (torch/MPS
    kernel compilation is much slower on the very first call) to happen now,
    against a throwaway page, instead of during a user's first real conversion.
    Returns the warm-up duration in seconds.
    """
    start = time.perf_counter()
    converter.initialize_pipeline(InputFormat.PDF)
    converter.convert(
        DocumentStream(name="warmup.pdf", stream=io.BytesIO(_WARMUP_PDF_BYTES)),
        raises_on_error=False,
    )
    duration = time.perf_counter() - start
    logger.info("warm_up duration_seconds=%.3f", duration)
    return duration


def _is_success(result: ConversionResult) -> bool:
    return result.status.name in ("SUCCESS", "PARTIAL_SUCCESS")


def convert_pdf_to_markdown(
    converter: DocumentConverter,
    source: Union[Path, BinaryIO, DocumentStream],
    filename: str,
    opts: ConversionOptions,
    image_dir: Path | None = None,
) -> ConversionOutcome:
    """Convert a single PDF to markdown. Never raises for expected failure
    modes (corrupt/unsupported PDF, oversized file) -- returns a structured
    outcome instead so the UI can render a friendly error per file.

    If `image_dir` is given (requires `opts.generate_picture_images=True`),
    figures are written into that directory and referenced from the markdown
    via relative links (`![](filename.png)`) instead of the default
    `<!-- image -->` placeholder.
    """
    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()

    def _finish(**kwargs) -> ConversionOutcome:
        ended_at = datetime.now(timezone.utc)
        duration = time.perf_counter() - start
        logger.info(
            "convert filename=%s ok=%s started_at=%s ended_at=%s duration_seconds=%.3f",
            filename, kwargs.get("ok"), started_at.isoformat(), ended_at.isoformat(), duration,
        )
        return ConversionOutcome(
            filename=filename,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            duration_seconds=duration,
            **kwargs,
        )

    try:
        result = converter.convert(
            source,
            raises_on_error=False,
            max_num_pages=opts.max_num_pages,
            max_file_size=opts.max_file_size_bytes,
        )
    except Exception as exc:  # unexpected library-level failure
        return _finish(ok=False, markdown=None, error=str(exc))

    if not _is_success(result):
        error_msg = "; ".join(str(e) for e in result.errors) or f"Conversion status: {result.status}"
        return _finish(ok=False, markdown=None, error=error_msg)

    if image_dir is not None:
        image_dir.mkdir(parents=True, exist_ok=True)
        markdown = result.document.export_to_markdown(
            image_mode=ImageRefMode.REFERENCED, image_dir=image_dir,
        )
    else:
        markdown = result.document.export_to_markdown()

    return _finish(ok=True, markdown=markdown, error=None)
