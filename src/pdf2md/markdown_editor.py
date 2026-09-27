"""Thin Python wrapper for the `markdown_editor` custom Streamlit component
(see `components/markdown_editor/index.html`) -- a CDN-embedded EasyMDE
live-preview markdown editor. Markdown stays the literal source of truth:
there is no markdown<->HTML conversion, so what comes back from the editor
is exactly what will be committed.
"""

from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

_COMPONENT_DIR = Path(__file__).parent / "components" / "markdown_editor"
_markdown_editor = components.declare_component("markdown_editor", path=str(_COMPONENT_DIR))


def markdown_editor(value: str, key: str, height: int = 400) -> str:
    """Render a live-preview markdown editor seeded with `value`. Returns
    the current editor content (debounced ~400ms after the last edit) --
    `value` itself on the very first render, before the component has sent
    anything back."""
    result = _markdown_editor(value=value, height=height, key=key, default=value)
    return result if result is not None else value
