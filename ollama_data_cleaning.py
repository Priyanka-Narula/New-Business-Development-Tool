#!/usr/bin/env python3
"""Compatibility CLI for evidence-backed CSV extraction."""

from __future__ import annotations

import argparse
from pathlib import Path

from aambridge.extract.service import clean_csv
from aambridge.logging_config import configure_logging
from aambridge.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--model", default="llama3.1")
    parser.add_argument("--workers", type=int, default=2)  # retained compatibility
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    settings = Settings()
    configure_logging(settings)
    output = args.output or args.input_csv.with_name(
        f"{args.input_csv.stem}_extracted.csv"
    )
    clean_csv(
        args.input_csv,
        output,
        model=args.model,
        timeout=args.timeout,
        max_chars=settings.extraction_max_chars,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
