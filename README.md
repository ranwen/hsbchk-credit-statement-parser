# HSBC HK Credit Statement Parser

A strict (fast-fail) parser for **HSBC Hong Kong credit card eStatements (PDF)**.
It extracts statement data into structured JSON for downstream processing.

## AI Authorship Notice

**This codebase was 100% written by AI.**
You should treat outputs as machine-generated and perform human review before using in production, accounting, compliance, or legal workflows.

## What It Does

- Parses HSBC HK statement PDFs into JSON.
- Supports multi-card account statements (primary + supplementary cards).
- Supports dual-currency and single-account statement layouts.
- Extracts transaction-level fields, including:
- posted amount and signed amount (`CR` handled as negative)
- payment method (`APPLE_PAY`, `UNIONPAY_QR`, or `null`)
- 2-letter region code (when present)
- transaction currency and currency amount (always populated)
- exchange rate (for cross-currency transactions)
- Groups transactions under each card:
- `sub_accounts[].cards[].transactions[]`

## Design Principle: Fast-Fail

The parser is intentionally strict:

- If expected patterns do not match, it raises an error.
- If summaries conflict with parsed transactions, it raises an error.
- If cross-currency transaction metadata is incomplete (for example missing exchange rate), it raises an error.

This is to prevent silent mis-parsing.

## Project Structure

- `hsbc_hk_statement_parser.py`: importable parser library.
- `parse_cli.py`: command-line interface.
- `tests/`: local tests (ignored by default in `.gitignore` due potential sensitive data).

## Installation

Python 3.10+ recommended.

```bash
python3 -m pip install pypdf==5.9.0
```

Important: use `pypdf==5.9.0`. Newer versions may fail or hang on some HSBC statement templates.

## CLI Usage

```bash
python3 parse_cli.py /path/to/statement.pdf -o output.json --pretty
```

Without `-o`, JSON is printed to stdout.

## Library Usage

```python
from pathlib import Path
from hsbc_hk_statement_parser import parse_statement, ParseError

try:
    data = parse_statement(Path("eStatementFile.pdf"))
except ParseError as e:
    print(f"Parse failed: {e}")
```

## Output Shape (High Level)

```json
{
  "statement_product": "EXAMPLE_STATEMENT_PRODUCT",
  "statement_date": "YYYY-MM-DD",
  "sub_accounts": [
    {
      "account_number": "...",
      "sub_account_currency": "HKD",
      "amount_currency": "HKD",
      "summary": {...},
      "cards": [
        {
          "card_number": "...",
          "cardholder_name": "...",
          "transactions": [
            {
              "post_date": "...",
              "transaction_date": "...",
              "description": "...",
              "amount": "...",
              "signed_amount": "...",
              "payment_method": "APPLE_PAY",
              "region_code_alpha2": "...",
              "currency": "...",
              "currency_amount": "...",
              "exchange_rate": "...",
              "notes": []
            }
          ]
        }
      ]
    }
  ]
}
```

## Privacy and Safety

Real statements contain personal and financial data.

- Keep real PDFs and exports out of version control.
- Review `.gitignore` before committing.
- Remove/mask sensitive samples before sharing.

## Run Tests

```bash
python3 -m unittest discover -s tests -v
```
