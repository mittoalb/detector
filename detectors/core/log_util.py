"""
Color-aware console logging shared by the driver process (run_ioc.py)
and the GUI subprocess (qt_gui.py). ANSI color codes only when stderr
is a TTY — piping to a file (`tee /tmp/ioc.log`) stays plain-text.
"""

import logging
import os
import sys


_ANSI = {
    "DEBUG":    "\033[38;5;244m",  # dim gray
    "INFO":     "\033[38;5;39m",   # cyan
    "WARNING":  "\033[38;5;220m",  # amber
    "ERROR":    "\033[38;5;196m",  # red
    "CRITICAL": "\033[1;38;5;201m",  # bold magenta
    "NAME":     "\033[38;5;108m",   # muted green
    "RESET":    "\033[0m",
}


class ColorFormatter(logging.Formatter):
    """Colorize levelname and logger name for terminal readability."""

    def __init__(self, use_color: bool):
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S")
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if not self._use_color:
            return super().format(record)
        lvl = _ANSI.get(record.levelname, "")
        reset = _ANSI["RESET"]
        orig_level = record.levelname
        orig_name = record.name
        record.levelname = f"{lvl}{orig_level}{reset}"
        record.name = f"{_ANSI['NAME']}{orig_name}{reset}"
        try:
            return super().format(record)
        finally:
            record.levelname = orig_level
            record.name = orig_name


def _want_color() -> bool:
    """Decide whether to emit ANSI colors.

    Order of precedence:
      1. NO_COLOR set (any value) → disable       (https://no-color.org)
      2. FORCE_COLOR / CLICOLOR_FORCE set → enable  (used when piping to tee)
      3. TERM=dumb → disable
      4. Otherwise: enable iff stderr is a TTY
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    if os.environ.get("TERM", "") == "dumb":
        return False
    return sys.stderr.isatty()


def install_color_logging(level: int) -> None:
    """Attach a color-aware handler to the root logger.

    Replaces any pre-existing handlers so we don't double-emit when
    something already called logging.basicConfig.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColorFormatter(use_color=_want_color()))
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
