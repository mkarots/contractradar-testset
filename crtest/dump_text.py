"""Write cases/<id>/text.txt: the `pdftotext -layout` dump (tesseract OCR for image-only PDFs) that the text-mode
extractors and the MCP/HTTP backend hand to models. The web host has no pdftotext, so the dumps are committed.

Run after `make cases`:  python -m crtest.dump_text --cases cases
"""
from __future__ import annotations

import argparse
import json
import os

from .extract_llm import pdf_text
from .sysdeps import require_tools


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="cases")
    args = ap.parse_args(argv)
    require_tools("pdftotext", "tesseract")
    with open(os.path.join(args.cases, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    for c in manifest["cases"]:
        text = pdf_text(os.path.join(args.cases, c["document"]), ocr=True)
        with open(os.path.join(args.cases, c["case_id"], "text.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        print(c["case_id"], len(text), "chars")


if __name__ == "__main__":
    main()
