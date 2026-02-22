#!/usr/bin/env python3
"""CLI for HSBC HK credit card statement parser."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from hsbc_hk_statement_parser import ParseError, parse_statement


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse HSBC HK credit card statement PDF into JSON."
    )
    parser.add_argument("pdf_path", type=Path, help="Path to statement PDF")
    parser.add_argument("-o", "--output", type=Path, help="Output JSON file path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args(argv)

    try:
        result = parse_statement(args.pdf_path)
    except ParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    indent = 2 if args.pretty or args.output else None
    rendered = json.dumps(result, ensure_ascii=False, indent=indent)

    if args.output:
        args.output.write_text(rendered + ("\n" if indent is not None else ""), encoding="utf-8")
    else:
        print(rendered)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
