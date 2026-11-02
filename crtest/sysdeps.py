"""System programs the generators shell out to.

Poppler is not a pip package. A missing `pdftoppm` otherwise shows up as
FileNotFoundError after a case PDF has already been written. A binary that
is on PATH but cannot start (a Homebrew bottle built for a newer macOS) is
the same failure, so the check runs the tool instead of only searching PATH.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

POPPLER_INSTALL = """\
Poppler provides pdftotext and pdftoppm.
  macOS:          brew install poppler
  Debian/Ubuntu:  sudo apt-get install poppler-utils

Tesseract is only needed for OCR (make text, extract_llm --ocr):
  macOS:          brew install tesseract
  Debian/Ubuntu:  sudo apt-get install tesseract-ocr
"""


def missing(names: list[str]) -> list[str]:
    return [name for name in names if shutil.which(name) is None]


def probe(name: str) -> str | None:
    """None when `name -v` runs. Otherwise a one-line reason."""
    path = shutil.which(name)
    if path is None:
        return f"{name}: not on PATH"
    try:
        result = subprocess.run([path, "-v"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{name}: found at {path} but failed to run ({exc})"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {result.returncode}"
        return f"{name}: found at {path} but failed to run ({tail})"
    return None


def require_tools(*names: str) -> None:
    problems = [message for name in names if (message := probe(name))]
    if not problems:
        return
    raise SystemExit("Cannot use system tool(s):\n  " + "\n  ".join(problems) + "\n" + POPPLER_INSTALL)


def main(argv: list[str] | None = None) -> None:
    names = list(argv if argv is not None else sys.argv[1:]) or ["pdftoppm", "pdftotext"]
    require_tools(*names)
    print("found " + ", ".join(names))


if __name__ == "__main__":
    main()
