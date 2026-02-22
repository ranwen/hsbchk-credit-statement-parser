# Web Statement Management Platform

A minimal frontend-backend separated platform for managing HSBC HK credit card statements.

- Backend: FastAPI + SQLite
- Frontend: static HTML/CSS/JS served by `web/app.py` (same process/port)
- Auth: token-based login
- Authorization: file-configured only (no in-app permission editing)

## Important Rules Implemented

1. Only two roles exist: `admin` and non-admin user.
2. `admin` has full read/write permissions (including upload).
3. Non-admin has read-only permissions.
4. Permissions are date-independent.
5. Permission precision supports 3 typed levels:
- `statement_product`
- `account_number`
- `card_number`
6. Both statement read and transaction read use the same scope list: `permissions.read`.
7. Type must be explicit in config (to avoid ambiguity when number strings overlap).
8. Upload has deduplication: if `statement_date + statement_product + account_number` already exists, upload is rejected.

## Config (Single File)

Runtime config file: `web/config.json` (local-only, gitignored)

Template file: `web/config.example.json`

Example permission format (placeholder values only):

```json
{
  "users": [
    {
      "username": "reader",
      "token": "CHANGE_ME_READER_TOKEN",
      "role": "user",
      "permissions": {
        "read": [
          { "type": "statement_product", "value": "EXAMPLE_STATEMENT_PRODUCT" },
          { "type": "account_number", "value": "EXAMPLE_ACCOUNT_NUMBER" },
          { "type": "card_number", "value": "EXAMPLE_CARD_NUMBER" }
        ]
      }
    }
  ]
}
```

## Backend API

Base path: `/api`

- `POST /api/login` token login
- `GET /api/me`
- `POST /api/statements/upload` (admin only)
- `GET /api/statements`
- `GET /api/statements/{statement_id}`
- `GET /api/statements/{statement_id}/file`
- `GET /api/transactions`
- `GET /api/statement_summary` (`statement_id` required)
  - summary metrics: `spend`, `refund`, `net_spend`, `payment` (grouped by account and card)

Pagination:
- `GET /api/statements` supports `limit` and `offset` (default `limit=500`)
- `GET /api/transactions` supports `limit` and `offset` (default `limit=500`)
- `GET /api/transactions` also supports transaction date range filters: `tx_date_from`, `tx_date_to` (`YYYY-MM-DD`)

## Data Model

Only two tables are used:

- `statements`
- `transactions`

No extra middle tables for accounts/cards are used.

## Run

Install dependencies:

```bash
python3 -m pip install --break-system-packages -r web/requirements.txt
```

Start app (API + frontend together):

```bash
python3 web/run.py
```

Open:

- Frontend: `http://127.0.0.1:8000/`
- Backend API: `http://127.0.0.1:8000/api/*`

Token can also be passed via URL query parameter for auto-login:

`http://127.0.0.1:8000/?token=YOUR_TOKEN`

URL state is also supported for page action and transaction filters, for example:

`http://127.0.0.1:8000/?action=transactions&statement_id=1`

Supported query keys:
- `action` = `statements` | `transactions` | `summary` | `upload`
- `statement_id`
- `statement_product`
- `summary_statement_id`
- `card_number`
- `cardholder_name` (cardholder name fuzzy match)
- `tx_date_from` (transaction date from, `YYYY-MM-DD`)
- `tx_date_to` (transaction date to, `YYYY-MM-DD`)
- `q`
- `st_offset` (statements page offset)
- `tx_offset` (transactions page offset)

The UI button `Copy Login Link` copies the current URL state plus `token=...`, so receivers can auto-login and see the same filtered view.

## Security Notes

- `web/config.json` is ignored by git, so real tokens are not committed.
- `web/data/` is ignored by git, so uploaded PDFs and local DB are not committed.
- Keep all config examples sanitized; never put real account/card/token values in docs.
