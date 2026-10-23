"""Recognizes a scanned PDF into a text file for the knowledge base.

python -m tools.ocr_pdf "scan.pdf" [out.txt] [--from-page 3]    (by default data/kb/<name>.txt)
"""

import argparse
import re
from collections import Counter
from pathlib import Path

import pymupdf

from app.config import cfg
from app.knowledge import ocr_page, tidy_ocr

# the stamp table on top of every page of a company regulation: "Лист 9", "Издание № 1", "№ измен/дата"
STAMP = re.compile(r"лист|издани|измен", re.IGNORECASE)
NUMBERED = re.compile(r"\d+(\.\d+)*\.\s|\d+(\.\d+)+\s")


def line_key(line: str) -> str:
    return re.sub(r"[^а-яёa-z]", "", line.lower())


def repeated_lines(pages: list[str]) -> set[str]:
    """Lines found at the top of many pages, the running header."""
    counts = Counter()
    for page in pages:
        counts.update({line_key(line) for line in page.splitlines()[:15] if len(line_key(line)) >= 5})
    return {key for key, n in counts.items() if n >= max(3, len(pages) // 3)}


def cut_header(page: str, repeated: set[str]) -> str:
    lines = page.splitlines()
    end = 0
    for i, line in enumerate(lines[:15]):
        line = line.strip()
        # the header is a few short lines, body text starts with a long line or a clause number
        if len(line) > 60 or NUMBERED.match(line):
            break
        key = line_key(line)
        # OCR adds noise to header lines ("распорядка тие вт"), so a known start is enough
        if STAMP.search(line) or (key and any(key.startswith(r) for r in repeated)):
            end = i + 1
    return "\n".join(lines[end:])


def join_pages(pages: list[str]) -> str:
    text = ""
    for page in pages:
        if not page:
            continue
        if text and not re.search(r"[.;:!?]$", text) and page[:1].islower():
            text += " " + page
        elif text:
            text += "\n\n" + page
        else:
            text = page
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Распознать скан PDF в текст для базы знаний")
    parser.add_argument("pdf")
    parser.add_argument("out", nargs="?")
    # the cover and the table of contents only add noise to search
    parser.add_argument("--from-page", type=int, default=1)
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    out = Path(args.out) if args.out else Path(cfg.DATA_DIR) / "kb" / f"{pdf_path.stem}.txt"
    raw = []
    with pymupdf.open(pdf_path) as pdf:
        for number, page in enumerate(pdf, 1):
            if number < args.from_page:
                continue
            print(f"страница {number} из {pdf.page_count}", flush=True)
            try:
                raw.append(ocr_page(page))
            except ValueError as e:
                raise SystemExit(str(e))

    repeated = repeated_lines(raw)
    text = join_pages([tidy_ocr(cut_header(page, repeated)) for page in raw])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(f"готово: {out}, {len(text)} знаков")


if __name__ == "__main__":
    main()
