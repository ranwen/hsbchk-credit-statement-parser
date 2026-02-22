#!/usr/bin/env python3
"""Parse HSBC HK credit card statements from PDF to JSON.

This parser is intentionally strict (fast-fail): if transaction-like lines or key summary
lines cannot be matched, parsing stops with an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from pypdf import PdfReader


MONEY_Q = Decimal("0.01")
MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


ACCOUNT_HEADER_RE = re.compile(
    r"Account\s*number\s+((?:\d{4}\s*){4})\s*(HKD|RMB)\s*Sub-account\s+"
    r"Statement\s*balance\s+(HKD|CNY)\s*([0-9,]+\.\d{2})",
    re.IGNORECASE,
)
SINGLE_BALANCE_RE = re.compile(
    r"Statement\s*date\s+Statement\s*balance\s+(\d{2})\s+([A-Z]{3})\s+(\d{4})\s+"
    r"(HKD|CNY|RMB)\s*([0-9,]+\.\d{2})",
    re.IGNORECASE,
)
STATEMENT_DATE_RE = re.compile(r"Statement\s*date\s+(\d{2})\s+([A-Z]{3})\s+(\d{4})", re.IGNORECASE)
CARD_NUMBER_ANYWHERE_RE = re.compile(r"(?<!\d)\d{4}(?:\s+\d{4}){3}(?!\d)")
AMOUNT_HEADER_RE = re.compile(r"Amount\s*\((HKD|CNY|RMB)\)", re.IGNORECASE)
CARD_HOLDER_RE = re.compile(r"^((?:\d{4}\s+){3}\d{4})\s+([A-Za-z][A-Za-z .,'()/-]{1,48})$")
PREVIOUS_BALANCE_RE = re.compile(r"^PREVIOUS BALANCE\s+([0-9][0-9,]*\.\d{2})$")
TRANSACTION_RE = re.compile(
    r"^(\d{2}[A-Z]{3})\s+(\d{2}[A-Z]{3})\s+(.+?)\s+([0-9][0-9,]*\.\d{2}(?:CR)?)$"
)
STATEMENT_BALANCE_RE = re.compile(r"^STATEMENT BALANCE\s+([0-9][0-9,]*\.\d{2})$")
SUMMARY_CREDIT_RE = re.compile(r"^CREDIT/PAYMENT\s*:\s*([0-9][0-9,]*\.\d{2}(?:CR)?)$")
SUMMARY_PURCHASE_RE = re.compile(
    r"^PURCHASES AND INSTALMENTS\s*:\s*([0-9][0-9,]*\.\d{2})$"
)
SUMMARY_TOTAL_RE = re.compile(r"^TOTAL ACCOUNT BALANCE\s*:\s*([0-9][0-9,]*\.\d{2})$")
CONTINUATION_RE = re.compile(
    r"^(?:APPLE\s*PAY-MOBILE:\d{4}|UNIONPAY\s*QR|\*EXCHANGE\s*RATE:\s*[0-9.]+)$",
    re.IGNORECASE,
)
APPLE_PAY_RE = re.compile(r"^APPLE\s*PAY-MOBILE:(\d{4})$", re.IGNORECASE)
UNIONPAY_QR_RE = re.compile(r"^UNIONPAY\s*QR$", re.IGNORECASE)
PLAIN_AMOUNT_RE = re.compile(r"^[0-9][0-9,]*\.\d{2}$")
ALPHA2_RE = re.compile(r"^[A-Z]{2}$")
ALPHA3_RE = re.compile(r"^[A-Z]{3}$")
EXCHANGE_RATE_RE = re.compile(r"^\*EXCHANGE\s*RATE:\s*([0-9]+(?:\.[0-9]+)?)$", re.IGNORECASE)
HEADER_DATE_TOKEN_RE = re.compile(r"(\d{2})\s*([A-Z]{3})\s*(\d{4})", re.IGNORECASE)


class ParseError(RuntimeError):
    pass


@dataclass
class PageBlock:
    page_number: int
    lines: List[str]


@dataclass
class Transaction:
    post_date: str
    transaction_date: str
    description: str
    amount: Decimal
    signed_amount: Decimal
    is_credit: bool
    kind: str
    card_number: str
    cardholder_name: str
    payment_method: Optional[str] = None
    region_code_alpha2: Optional[str] = None
    currency: Optional[str] = None
    currency_amount: Optional[Decimal] = None
    exchange_rate: Optional[Decimal] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class SubAccount:
    account_number: str
    sub_account_currency: Optional[str]
    amount_currency: Optional[str]
    statement_balance_header: Optional[Decimal]
    pages: List[PageBlock] = field(default_factory=list)

    cards: Dict[str, str] = field(default_factory=dict)
    previous_balance: Optional[Decimal] = None
    statement_balance_summary: Optional[Decimal] = None
    summary_credit_payment: Optional[Decimal] = None
    summary_purchases_and_instalments: Optional[Decimal] = None
    summary_total_account_balance: Optional[Decimal] = None
    transactions: List[Transaction] = field(default_factory=list)


def squeeze_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def to_card_number(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 16:
        raise ParseError(f"Invalid card/account number format: {raw!r}")
    return digits


def parse_money(raw: str, context: str) -> Tuple[Decimal, Decimal, bool]:
    token = raw.replace(" ", "")
    is_credit = token.endswith("CR")
    if is_credit:
        token = token[:-2]
    token = token.replace(",", "")
    if not re.fullmatch(r"\d+\.\d{2}", token):
        raise ParseError(f"Invalid money token {raw!r} at {context}")
    amount = Decimal(token).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    signed = -amount if is_credit else amount
    return amount, signed, is_credit


def parse_plain_amount(raw: str, context: str) -> Decimal:
    token = raw.replace(",", "")
    if not re.fullmatch(r"\d+\.\d{2}", token):
        raise ParseError(f"Invalid plain amount token {raw!r} at {context}")
    return Decimal(token).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def canonical_base_currency(amount_currency: str) -> str:
    return "CNY" if amount_currency.upper() in {"CNY", "RMB"} else amount_currency.upper()


def split_description_details(
    description_raw: str, context: str
) -> Tuple[str, Optional[str], Optional[str], Optional[Decimal]]:
    tokens = squeeze_ws(description_raw).split(" ")
    if not tokens:
        raise ParseError(f"Empty transaction description at {context}")

    currency: Optional[str] = None
    currency_amount: Optional[Decimal] = None
    if len(tokens) >= 3 and ALPHA3_RE.fullmatch(tokens[-2]) and PLAIN_AMOUNT_RE.fullmatch(tokens[-1]):
        currency = tokens[-2]
        currency_amount = parse_plain_amount(tokens[-1], context)
        tokens = tokens[:-2]

    region_code_alpha2: Optional[str] = None
    if len(tokens) >= 2 and ALPHA2_RE.fullmatch(tokens[-1]):
        region_code_alpha2 = tokens[-1]
        tokens = tokens[:-1]

    description = squeeze_ws(" ".join(tokens))
    if not description:
        raise ParseError(f"Merchant description became empty after parsing details at {context}")

    return description, region_code_alpha2, currency, currency_amount


def parse_ddmon(token: str, statement_year: int, statement_month: int, context: str) -> str:
    if not re.fullmatch(r"\d{2}[A-Z]{3}", token):
        raise ParseError(f"Invalid date token {token!r} at {context}")
    day = int(token[:2])
    mon_txt = token[2:]
    month = MONTHS.get(mon_txt)
    if month is None:
        raise ParseError(f"Unknown month {mon_txt!r} at {context}")
    year = statement_year - 1 if month > statement_month else statement_year
    try:
        parsed = date(year, month, day)
    except ValueError as exc:
        raise ParseError(f"Invalid calendar date {token!r} at {context}") from exc
    return parsed.isoformat()


def normalize_currency(raw: str, context: str) -> str:
    ccy = raw.upper()
    if ccy not in {"HKD", "CNY", "RMB"}:
        raise ParseError(f"Unsupported currency {raw!r} at {context}")
    return ccy


def sub_currency_from_amount_currency(amount_currency: str) -> str:
    return "RMB" if amount_currency in {"CNY", "RMB"} else "HKD"


def extract_unique_card_numbers(page_text: str) -> Set[str]:
    return {to_card_number(m.group(0)) for m in CARD_NUMBER_ANYWHERE_RE.finditer(page_text)}


def normalize_statement_product(raw: str) -> str:
    product = squeeze_ws(raw).upper()
    # Some plain-text extracts collapse spaces in known suffixes.
    product = re.sub(r"(?<!\s)(DUALCURRENCY)\b", r" \1", product)
    product = re.sub(r"(?<!\s)(CREDITCARD)\b", r" CREDIT CARD", product)
    product = re.sub(r"\s+", " ", product).strip()
    return product


def infer_statement_product(reader: PdfReader) -> str:
    for page in reader.pages:
        plain_text = page.extract_text() or ""
        plain_lines = [squeeze_ws(ln) for ln in plain_text.splitlines() if squeeze_ws(ln)]
        for idx, line in enumerate(plain_lines):
            normalized = line.upper().replace(" ", "")
            if normalized not in {"CARDTYPE", "CARDTYPECREDITLIMIT"} and "CARDTYPE" not in normalized:
                continue
            for j in range(idx + 1, min(idx + 8, len(plain_lines))):
                candidate = plain_lines[j]
                upper = candidate.upper()
                if any(
                    bad in upper
                    for bad in (
                        "STATEMENTDATE",
                        "ACCOUNTNUMBER",
                        "CREDITLIMIT",
                        "PAGE",
                        "POST DATE",
                        "POSTDATE",
                    )
                ):
                    continue
                m_amount = re.match(r"^([A-Z][A-Z0-9 &/-]{2,}?)\s*HKD[0-9,]+\.\d{2}\*?$", candidate)
                if m_amount:
                    return normalize_statement_product(m_amount.group(1))
                m_code = re.match(r"^(?:\d{8,}\s+)?([A-Z][A-Z0-9 &/-]{2,})$", upper)
                if m_code:
                    product = normalize_statement_product(m_code.group(1))
                    if (
                        product not in {"O", "CHINA"}
                        and not any(ch.isdigit() for ch in product)
                        and len(product.split()) <= 5
                    ):
                        return product

    raise ParseError("Could not infer statement product from PDF")


def is_probable_cardholder(name: str) -> bool:
    candidate = squeeze_ws(name).upper()
    if any(ch.isdigit() for ch in candidate):
        return False
    blocked_words = {
        "PULSE",
        "DUALCURRENCY",
        "CARDTYPE",
        "STATEMENTDATE",
        "CREDITLIMIT",
        "ACCOUNTNUMBER",
    }
    if any(word in candidate for word in blocked_words):
        return False
    words = [w for w in candidate.split(" ") if w]
    if len(words) < 1:
        return False
    if len(words) > 6:
        return False
    return True


def money_to_json(amount: Decimal) -> str:
    return format(amount.quantize(MONEY_Q, rounding=ROUND_HALF_UP), "f")


def decimal_to_json(value: Decimal) -> str:
    return format(value, "f")


def set_once_or_same(current: Optional[Decimal], new: Decimal, label: str, context: str) -> Decimal:
    if current is None:
        return new
    if current != new:
        raise ParseError(
            f"Conflicting {label}: existing {money_to_json(current)} vs {money_to_json(new)} at {context}"
        )
    return current


def extract_header_statement_date(layout_text: str, plain_text: str) -> Optional[Tuple[str, str, str]]:
    """Extract statement date from title/header region (supports 12JAN2026 or 12 JAN 2026)."""

    def from_lines(raw_lines: List[str]) -> Optional[Tuple[str, str, str]]:
        lines = [squeeze_ws(ln) for ln in raw_lines if squeeze_ws(ln)]
        # Limit to top section to reduce false-positive dates in body text.
        top = lines[:80]
        for idx, line in enumerate(top):
            normalized = re.sub(r"\s+", "", line).upper()
            if "STATEMENTDATE" not in normalized:
                continue
            window = top[idx : min(idx + 4, len(top))]
            for candidate in window:
                m = HEADER_DATE_TOKEN_RE.search(candidate.upper())
                if m:
                    day, mon, year = m.groups()
                    return day, mon, year
        return None

    # Prefer layout lines (better header geometry), then fallback to plain.
    got = from_lines(layout_text.splitlines())
    if got is not None:
        return got
    return from_lines(plain_text.splitlines())


def parse_statement(pdf_path: Path) -> dict:
    reader = PdfReader(str(pdf_path))
    if not reader.pages:
        raise ParseError("PDF has no pages")
    statement_product = infer_statement_product(reader)

    sub_accounts: Dict[str, SubAccount] = {}
    statement_date_global: Optional[date] = None

    def record_statement_date(day_raw: str, mon_raw: str, year_raw: str, context: str) -> None:
        nonlocal statement_date_global
        mon_txt = mon_raw.upper()
        month = MONTHS.get(mon_txt)
        if month is None:
            raise ParseError(f"Unsupported statement month {mon_raw!r} at {context}")
        parsed_stmt_date = date(int(year_raw), month, int(day_raw))
        if statement_date_global is None:
            statement_date_global = parsed_stmt_date
        elif parsed_stmt_date != statement_date_global:
            raise ParseError(
                f"Statement date mismatch: {statement_date_global.isoformat()} vs "
                f"{parsed_stmt_date.isoformat()} at {context}"
            )

    def upsert_account_header(
        account_number: str,
        sub_currency: str,
        amount_currency: str,
        statement_balance: Optional[Decimal],
        page_idx: int,
    ) -> SubAccount:
        existing = sub_accounts.get(account_number)
        if existing is None:
            existing = SubAccount(
                account_number=account_number,
                sub_account_currency=sub_currency,
                amount_currency=amount_currency,
                statement_balance_header=statement_balance,
            )
            sub_accounts[account_number] = existing
            return existing

        if existing.sub_account_currency and existing.sub_account_currency != sub_currency:
            raise ParseError(
                f"Sub-account currency changed for {account_number} on page {page_idx}: "
                f"{existing.sub_account_currency} vs {sub_currency}"
            )
        if existing.amount_currency and existing.amount_currency != amount_currency:
            raise ParseError(
                f"Amount currency changed for {account_number} on page {page_idx}: "
                f"{existing.amount_currency} vs {amount_currency}"
            )
        existing.sub_account_currency = existing.sub_account_currency or sub_currency
        existing.amount_currency = existing.amount_currency or amount_currency

        if statement_balance is not None:
            if existing.statement_balance_header is None:
                existing.statement_balance_header = statement_balance
            elif existing.statement_balance_header != statement_balance:
                raise ParseError(
                    f"Header statement balance changed for {account_number}: "
                    f"{money_to_json(existing.statement_balance_header)} vs {money_to_json(statement_balance)}"
                )
        return existing

    for page_idx, page in enumerate(reader.pages, start=1):
        # Keep body parsing on layout extraction for stable transaction rows.
        text_layout = page.extract_text(extraction_mode="layout") or ""
        # Header/title region is often more reliable with non-layout extraction.
        text_plain = page.extract_text() or ""

        text = text_layout
        lines = text_layout.splitlines() if text_layout else text_plain.splitlines()
        compact_layout = squeeze_ws(text_layout)
        compact_plain = squeeze_ws(text_plain)
        context = f"page {page_idx}"

        # For statement date, prefer plain extraction to avoid layout column interleaving.
        date_match = STATEMENT_DATE_RE.search(compact_plain) or STATEMENT_DATE_RE.search(compact_layout)
        if date_match:
            d, mon_txt, y = date_match.groups()
            record_statement_date(d, mon_txt, y, context)
        elif statement_date_global is None:
            header_date = extract_header_statement_date(layout_text=text_layout, plain_text=text_plain)
            if header_date is not None:
                d, mon_txt, y = header_date
                record_statement_date(d, mon_txt, y, context)

        page_amount_match = AMOUNT_HEADER_RE.search(compact_plain) or AMOUNT_HEADER_RE.search(compact_layout)
        page_amount_currency = (
            normalize_currency(page_amount_match.group(1), context) if page_amount_match else None
        )

        # Header summary blocks are also from title region; plain extraction first.
        header_match = ACCOUNT_HEADER_RE.search(compact_plain) or ACCOUNT_HEADER_RE.search(compact_layout)
        if header_match:
            raw_account, sub_ccy_raw, amount_ccy_raw, stmt_balance_raw = header_match.groups()
            account_number = to_card_number(raw_account)
            sub_ccy = normalize_currency(sub_ccy_raw, context)
            amount_ccy = normalize_currency(amount_ccy_raw, context)
            statement_balance = Decimal(stmt_balance_raw.replace(",", "")).quantize(
                MONEY_Q, rounding=ROUND_HALF_UP
            )

            if sub_ccy == "HKD" and amount_ccy != "HKD":
                raise ParseError(
                    f"Currency mismatch for account {account_number} on page {page_idx}: {sub_ccy=} {amount_ccy=}"
                )
            if sub_ccy == "RMB" and amount_ccy not in {"CNY", "RMB"}:
                raise ParseError(
                    f"Currency mismatch for account {account_number} on page {page_idx}: {sub_ccy=} {amount_ccy=}"
                )

            existing = upsert_account_header(
                account_number=account_number,
                sub_currency=sub_ccy,
                amount_currency=amount_ccy,
                statement_balance=statement_balance,
                page_idx=page_idx,
            )
            existing.pages.append(PageBlock(page_number=page_idx, lines=lines))
            continue

        page_accounts = extract_unique_card_numbers(text)
        if not page_accounts:
            page_accounts = extract_unique_card_numbers(text_plain)
        single_balance_match = SINGLE_BALANCE_RE.search(compact_plain) or SINGLE_BALANCE_RE.search(
            compact_layout
        )
        if single_balance_match:
            d, mon_txt, y, amount_ccy_raw, stmt_balance_raw = single_balance_match.groups()
            record_statement_date(d, mon_txt, y, context)
            amount_ccy = normalize_currency(amount_ccy_raw, context)
            statement_balance = Decimal(stmt_balance_raw.replace(",", "")).quantize(
                MONEY_Q, rounding=ROUND_HALF_UP
            )
            if len(page_accounts) != 1:
                raise ParseError(
                    f"Single-account balance header found but could not map unique account number on page {page_idx}"
                )
            account_number = next(iter(page_accounts))
            sub_ccy = sub_currency_from_amount_currency(amount_ccy)
            existing = upsert_account_header(
                account_number=account_number,
                sub_currency=sub_ccy,
                amount_currency=amount_ccy,
                statement_balance=statement_balance,
                page_idx=page_idx,
            )
            existing.pages.append(PageBlock(page_number=page_idx, lines=lines))
            continue

        if len(page_accounts) == 1:
            account_number = next(iter(page_accounts))
            existing = sub_accounts.get(account_number)
            if existing is None:
                sub_ccy = (
                    sub_currency_from_amount_currency(page_amount_currency)
                    if page_amount_currency is not None
                    else None
                )
                existing = SubAccount(
                    account_number=account_number,
                    sub_account_currency=sub_ccy,
                    amount_currency=page_amount_currency,
                    statement_balance_header=None,
                )
                sub_accounts[account_number] = existing
            else:
                if page_amount_currency is not None:
                    if existing.amount_currency and existing.amount_currency != page_amount_currency:
                        raise ParseError(
                            f"Amount currency changed for {account_number} on page {page_idx}: "
                            f"{existing.amount_currency} vs {page_amount_currency}"
                        )
                    expected_sub = sub_currency_from_amount_currency(page_amount_currency)
                    if existing.sub_account_currency and existing.sub_account_currency != expected_sub:
                        raise ParseError(
                            f"Sub-account currency changed for {account_number} on page {page_idx}: "
                            f"{existing.sub_account_currency} vs {expected_sub}"
                        )
                    existing.amount_currency = existing.amount_currency or page_amount_currency
                    existing.sub_account_currency = existing.sub_account_currency or expected_sub
            existing.pages.append(PageBlock(page_number=page_idx, lines=lines))

    if statement_date_global is None:
        raise ParseError("Could not find statement date in PDF")
    if not sub_accounts:
        raise ParseError("Could not find any HSBC HK account pages")

    stmt_year = statement_date_global.year
    stmt_month = statement_date_global.month

    parsed_accounts: List[dict] = []
    for account in sorted(sub_accounts.values(), key=lambda s: s.account_number):
        if account.amount_currency is None and account.sub_account_currency is None:
            raise ParseError(f"Could not determine currency for account {account.account_number}")
        if account.amount_currency is None and account.sub_account_currency is not None:
            account.amount_currency = "CNY" if account.sub_account_currency == "RMB" else "HKD"
        if account.sub_account_currency is None and account.amount_currency is not None:
            account.sub_account_currency = sub_currency_from_amount_currency(account.amount_currency)
        parse_sub_account(account, stmt_year=stmt_year, stmt_month=stmt_month)
        validate_sub_account(account)
        parsed_accounts.append(sub_account_to_json(account))

    return {
        "statement_product": statement_product,
        "statement_date": statement_date_global.isoformat(),
        "sub_accounts": parsed_accounts,
    }


def parse_sub_account(account: SubAccount, stmt_year: int, stmt_month: int) -> None:
    if account.amount_currency is None:
        raise ParseError(f"Missing amount currency for account {account.account_number}")
    base_currency = canonical_base_currency(account.amount_currency)
    current_card_number: Optional[str] = None

    for page in account.pages:
        last_tx: Optional[Transaction] = None
        for line_no, raw_line in enumerate(page.lines, start=1):
            line = squeeze_ws(raw_line)
            if not line:
                continue
            context = f"page {page.page_number} line {line_no}"

            m_prev = PREVIOUS_BALANCE_RE.match(line)
            if m_prev:
                amount, _signed, _is_credit = parse_money(m_prev.group(1), context)
                if _is_credit:
                    raise ParseError(f"Previous balance cannot be credit at {context}")
                account.previous_balance = set_once_or_same(
                    account.previous_balance, amount, "previous_balance", context
                )
                last_tx = None
                continue

            m_stmt_bal = STATEMENT_BALANCE_RE.match(line)
            if m_stmt_bal:
                amount, _signed, _is_credit = parse_money(m_stmt_bal.group(1), context)
                if _is_credit:
                    raise ParseError(f"Statement balance cannot be CR at {context}")
                account.statement_balance_summary = set_once_or_same(
                    account.statement_balance_summary,
                    amount,
                    "statement_balance_summary",
                    context,
                )
                last_tx = None
                continue

            m_credit = SUMMARY_CREDIT_RE.match(line)
            if m_credit:
                amount, _signed, is_credit = parse_money(m_credit.group(1), context)
                if amount != Decimal("0.00") and not is_credit:
                    raise ParseError(f"CREDIT/PAYMENT must be CR at {context}")
                account.summary_credit_payment = set_once_or_same(
                    account.summary_credit_payment,
                    amount,
                    "summary_credit_payment",
                    context,
                )
                last_tx = None
                continue

            m_pur = SUMMARY_PURCHASE_RE.match(line)
            if m_pur:
                amount, _signed, is_credit = parse_money(m_pur.group(1), context)
                if is_credit:
                    raise ParseError(f"PURCHASES AND INSTALMENTS cannot be CR at {context}")
                account.summary_purchases_and_instalments = set_once_or_same(
                    account.summary_purchases_and_instalments,
                    amount,
                    "summary_purchases_and_instalments",
                    context,
                )
                last_tx = None
                continue

            m_total = SUMMARY_TOTAL_RE.match(line)
            if m_total:
                amount, _signed, is_credit = parse_money(m_total.group(1), context)
                if is_credit:
                    raise ParseError(f"TOTAL ACCOUNT BALANCE cannot be CR at {context}")
                account.summary_total_account_balance = set_once_or_same(
                    account.summary_total_account_balance,
                    amount,
                    "summary_total_account_balance",
                    context,
                )
                last_tx = None
                continue

            m_card = CARD_HOLDER_RE.match(line)
            if m_card:
                card_num_raw, candidate_name = m_card.groups()
                if is_probable_cardholder(candidate_name):
                    card_number = to_card_number(card_num_raw)
                    card_name = squeeze_ws(candidate_name)
                    existing = account.cards.get(card_number)
                    if existing is None:
                        account.cards[card_number] = card_name
                    elif existing != card_name:
                        raise ParseError(
                            f"Cardholder name changed for {card_number}: {existing!r} vs {card_name!r} at {context}"
                        )
                    current_card_number = card_number
                    last_tx = None
                    continue

            if re.match(r"^\d{2}[A-Z]{3}\b", line):
                m_tx = TRANSACTION_RE.match(line)
                if not m_tx:
                    raise ParseError(f"Transaction-like line could not be parsed at {context}: {line!r}")
                if current_card_number is None:
                    raise ParseError(f"Transaction before any cardholder header at {context}: {line!r}")

                post_token, trans_token, description_raw, amount_raw = m_tx.groups()
                amount, signed, is_credit = parse_money(amount_raw, context)
                post_date = parse_ddmon(post_token, stmt_year, stmt_month, context)
                trans_date = parse_ddmon(trans_token, stmt_year, stmt_month, context)
                (
                    description,
                    region_code_alpha2,
                    tx_currency,
                    tx_currency_amount,
                ) = split_description_details(description_raw, context)
                tx_currency = tx_currency or base_currency
                tx_currency_amount = tx_currency_amount or amount

                cardholder_name = account.cards.get(current_card_number)
                if not cardholder_name:
                    raise ParseError(f"Missing cardholder name for {current_card_number} at {context}")

                if is_credit and "PAID BY AUTOPAY" in description.upper():
                    kind = "payment"
                elif is_credit:
                    kind = "refund_or_credit"
                else:
                    kind = "purchase_or_charge"

                tx = Transaction(
                    post_date=post_date,
                    transaction_date=trans_date,
                    description=description,
                    amount=amount,
                    signed_amount=signed,
                    is_credit=is_credit,
                    kind=kind,
                    card_number=current_card_number,
                    cardholder_name=cardholder_name,
                    region_code_alpha2=region_code_alpha2,
                    currency=tx_currency,
                    currency_amount=tx_currency_amount,
                )
                account.transactions.append(tx)
                last_tx = tx
                continue

            if last_tx and CONTINUATION_RE.match(line):
                m_rate = EXCHANGE_RATE_RE.match(line)
                if m_rate:
                    rate = Decimal(m_rate.group(1))
                    if last_tx.exchange_rate is None:
                        last_tx.exchange_rate = rate
                    elif last_tx.exchange_rate != rate:
                        raise ParseError(
                            f"Conflicting exchange rate for transaction at {context}: "
                            f"{last_tx.exchange_rate} vs {rate}"
                        )
                    continue

                if APPLE_PAY_RE.match(line):
                    method = "APPLE_PAY"
                    if last_tx.payment_method is None:
                        last_tx.payment_method = method
                    elif last_tx.payment_method != method:
                        raise ParseError(
                            f"Conflicting payment method for transaction at {context}: "
                            f"{last_tx.payment_method} vs {method}"
                        )
                    continue

                if UNIONPAY_QR_RE.match(line):
                    method = "UNIONPAY_QR"
                    if last_tx.payment_method is None:
                        last_tx.payment_method = method
                    elif last_tx.payment_method != method:
                        raise ParseError(
                            f"Conflicting payment method for transaction at {context}: "
                            f"{last_tx.payment_method} vs {method}"
                        )
                    continue

                last_tx.notes.append(line)
                continue

            if line.startswith("PREVIOUS BALANCE"):
                raise ParseError(f"Malformed previous balance line at {context}: {line!r}")
            if line.startswith("STATEMENT BALANCE"):
                raise ParseError(f"Malformed statement balance line at {context}: {line!r}")
            if line.startswith("CREDIT/PAYMENT"):
                raise ParseError(f"Malformed CREDIT/PAYMENT line at {context}: {line!r}")
            if line.startswith("PURCHASES AND INSTALMENTS"):
                raise ParseError(f"Malformed PURCHASES/INSTALMENTS line at {context}: {line!r}")
            if line.startswith("TOTAL ACCOUNT BALANCE"):
                raise ParseError(f"Malformed TOTAL ACCOUNT BALANCE line at {context}: {line!r}")


def validate_sub_account(account: SubAccount) -> None:
    if account.previous_balance is None:
        raise ParseError(f"Missing previous balance for account {account.account_number}")
    if account.amount_currency is None:
        raise ParseError(f"Missing amount currency for account {account.account_number}")
    base_currency = canonical_base_currency(account.amount_currency)

    credits = sum((tx.amount for tx in account.transactions if tx.is_credit), Decimal("0.00"))
    debits = sum((tx.amount for tx in account.transactions if not tx.is_credit), Decimal("0.00"))
    net = (account.previous_balance + debits - credits).quantize(MONEY_Q, rounding=ROUND_HALF_UP)

    for tx in account.transactions:
        if tx.currency is None or tx.currency_amount is None:
            raise ParseError(
                f"Incomplete currency details in transaction {tx.post_date} "
                f"{tx.description!r} for account {account.account_number}"
            )

        is_cross_currency = tx.currency != base_currency
        if is_cross_currency:
            if tx.exchange_rate is None:
                raise ParseError(
                    f"Missing exchange rate for foreign transaction {tx.post_date} "
                    f"{tx.description!r} for account {account.account_number}"
                )
            converted = (tx.currency_amount * tx.exchange_rate).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
            if abs(converted - tx.amount) > Decimal("0.05"):
                raise ParseError(
                    f"Foreign conversion mismatch for transaction {tx.post_date} {tx.description!r}: "
                    f"foreign={tx.currency} {money_to_json(tx.currency_amount)} "
                    f"rate={tx.exchange_rate} converted={money_to_json(converted)} "
                    f"posted={money_to_json(tx.amount)}"
                )
        elif tx.exchange_rate is not None:
            raise ParseError(
                f"Exchange rate present without foreign currency details in transaction {tx.post_date} "
                f"{tx.description!r} for account {account.account_number}"
            )

    if account.summary_credit_payment is not None and credits != account.summary_credit_payment:
        raise ParseError(
            f"Credit summary mismatch for {account.account_number}: "
            f"transactions={money_to_json(credits)} summary={money_to_json(account.summary_credit_payment)}"
        )
    if account.summary_credit_payment is None and credits != Decimal("0.00"):
        raise ParseError(
            f"Missing CREDIT/PAYMENT summary while credit transactions exist for {account.account_number}: "
            f"{money_to_json(credits)}"
        )

    if (
        account.summary_purchases_and_instalments is not None
        and debits != account.summary_purchases_and_instalments
    ):
        raise ParseError(
            f"Purchases summary mismatch for {account.account_number}: "
            f"transactions={money_to_json(debits)} summary={money_to_json(account.summary_purchases_and_instalments)}"
        )
    if account.summary_purchases_and_instalments is None and debits != Decimal("0.00"):
        raise ParseError(
            f"Missing PURCHASES AND INSTALMENTS summary while debit transactions exist for "
            f"{account.account_number}: {money_to_json(debits)}"
        )

    if (
        account.statement_balance_summary is None
        and account.summary_total_account_balance is None
        and account.statement_balance_header is None
    ):
        raise ParseError(f"Missing statement balance anchors for account {account.account_number}")

    if account.statement_balance_summary is not None and net != account.statement_balance_summary:
        raise ParseError(
            f"Statement balance mismatch for {account.account_number}: "
            f"previous+tx={money_to_json(net)} statement={money_to_json(account.statement_balance_summary)}"
        )

    if account.summary_total_account_balance is not None and net != account.summary_total_account_balance:
        raise ParseError(
            f"Total account balance mismatch for {account.account_number}: "
            f"previous+tx={money_to_json(net)} "
            f"total={money_to_json(account.summary_total_account_balance)}"
        )

    if (
        account.statement_balance_summary is not None
        and account.summary_total_account_balance is not None
        and account.statement_balance_summary != account.summary_total_account_balance
    ):
        raise ParseError(
            f"Statement balance vs total mismatch for {account.account_number}: "
            f"statement={money_to_json(account.statement_balance_summary)} "
            f"total={money_to_json(account.summary_total_account_balance)}"
        )

    if account.statement_balance_header is not None and net != account.statement_balance_header:
        raise ParseError(
            f"Header vs summary statement balance mismatch for {account.account_number}: "
            f"header={money_to_json(account.statement_balance_header)} previous+tx={money_to_json(net)}"
        )


def sub_account_to_json(account: SubAccount) -> dict:
    if account.sub_account_currency is None or account.amount_currency is None:
        raise ParseError(f"Internal error: missing currency for account {account.account_number}")

    credits = sum((tx.amount for tx in account.transactions if tx.is_credit), Decimal("0.00"))
    debits = sum((tx.amount for tx in account.transactions if not tx.is_credit), Decimal("0.00"))
    previous = account.previous_balance or Decimal("0.00")
    computed_statement_balance = (previous + debits - credits).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    effective_statement_balance = (
        account.statement_balance_summary
        or account.summary_total_account_balance
        or account.statement_balance_header
        or computed_statement_balance
    )

    tx_by_card: Dict[str, List[dict]] = {card_number: [] for card_number in account.cards.keys()}
    for tx in account.transactions:
        tx_by_card.setdefault(tx.card_number, [])
        tx_by_card[tx.card_number].append(
            {
                "post_date": tx.post_date,
                "transaction_date": tx.transaction_date,
                "description": tx.description,
                "amount": money_to_json(tx.amount),
                "signed_amount": money_to_json(tx.signed_amount),
                "is_credit": tx.is_credit,
                "kind": tx.kind,
                "payment_method": tx.payment_method,
                "region_code_alpha2": tx.region_code_alpha2,
                "currency": tx.currency,
                "currency_amount": money_to_json(tx.currency_amount) if tx.currency_amount is not None else None,
                "exchange_rate": decimal_to_json(tx.exchange_rate) if tx.exchange_rate is not None else None,
                "notes": tx.notes,
            }
        )

    cards = [
        {
            "card_number": card_number,
            "cardholder_name": cardholder_name,
            "transactions": tx_by_card.get(card_number, []),
        }
        for card_number, cardholder_name in sorted(account.cards.items())
    ]

    return {
        "account_number": account.account_number,
        "sub_account_currency": account.sub_account_currency,
        "amount_currency": account.amount_currency,
        "statement_balance": money_to_json(effective_statement_balance),
        "previous_balance": money_to_json(previous),
        "summary": {
            "credit_payment": (
                money_to_json(account.summary_credit_payment)
                if account.summary_credit_payment is not None
                else None
            ),
            "purchases_and_instalments": (
                money_to_json(account.summary_purchases_and_instalments)
                if account.summary_purchases_and_instalments is not None
                else None
            ),
            "total_account_balance": (
                money_to_json(account.summary_total_account_balance)
                if account.summary_total_account_balance is not None
                else None
            ),
        },
        "cards": cards,
    }
