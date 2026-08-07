"""uk-regs-export — static context export for IDE assistants."""

from uk_regs_export.renderers import (
    render_all,
    render_claude_md,
    render_consolidated_md,
    render_cursor_mdc,
    render_json,
)

__all__ = [
    "render_all",
    "render_claude_md",
    "render_consolidated_md",
    "render_cursor_mdc",
    "render_json",
]

__version__ = "0.1.0"