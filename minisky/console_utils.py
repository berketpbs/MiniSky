"""
Shared console setup for any MiniSky entry point (CLI, or a standalone
subprocess like autostop_runner.py) that prints Rich output with
unicode (emoji, box-drawing, spinner) characters.
"""

import sys


def ensure_utf8_console() -> None:
    """
    Reconfigure stdout/stderr to UTF-8 on Windows.

    Non-UTF8 Windows console codepages (e.g. cp1254) can't encode the
    unicode characters Rich/emoji-using code writes (spinners, checkmarks,
    warning signs). Without this, printing one raises UnicodeEncodeError -
    which is a subclass of ValueError, so it can silently vanish into any
    `except (ValueError, ...)` clause nearby and look like nothing
    happened at all, instead of a visible crash.

    Must be called before any Console()/print() output happens - so at
    the top of any module that can run as its own entry point, not just
    inside cli.py (which every other in-process import already benefits
    from once cli.py itself has run this).
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
