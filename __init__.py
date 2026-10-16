"""NinjaPrompt: a small chat + agent-loop sidebar that glues an OpenAI-format model to
Binary Ninja's built-in MCP server."""
try:
    import binaryninja

    _HAS_UI = binaryninja.core_ui_enabled()
except Exception:  # imported outside Binary Ninja (tests)
    _HAS_UI = False

if _HAS_UI:
    from . import ui

    ui.register()
