from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    or_,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# allow importing the parser from repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hsbc_hk_statement_parser import ParseError, parse_statement  # noqa: E402


APP_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = APP_DIR / "frontend"
CONFIG_PATH = APP_DIR / "config.json"


class Base(DeclarativeBase):
    pass


class Statement(Base):
    __tablename__ = "statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    statement_date: Mapped[str] = mapped_column(String(32), nullable=False)
    statement_product: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(128), nullable=False)
    account_numbers_json: Mapped[str] = mapped_column(Text, nullable=False)
    card_numbers_json: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_json: Mapped[str] = mapped_column(Text, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id"), index=True, nullable=False)
    statement_product: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    account_number: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    card_number: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    cardholder_name: Mapped[str] = mapped_column(String(128), nullable=False)
    account_currency: Mapped[str] = mapped_column(String(16), nullable=False)

    post_date: Mapped[str] = mapped_column(String(16), nullable=False)
    transaction_date: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    amount: Mapped[float] = mapped_column(Float, nullable=False)
    signed_amount: Mapped[float] = mapped_column(Float, nullable=False)
    is_credit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    payment_method: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    region_code_alpha2: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    currency_amount: Mapped[float] = mapped_column(Float, nullable=False)
    exchange_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes_json: Mapped[str] = mapped_column(Text, nullable=False)


@dataclass(frozen=True)
class Scope:
    statement_products: Set[str]
    account_numbers: Set[str]
    card_numbers: Set[str]


@dataclass(frozen=True)
class User:
    username: str
    token: str
    role: str
    read_scope: Scope


class LoginRequest(BaseModel):
    token: str


@dataclass(frozen=True)
class ParsedStatementBundle:
    statement_date: str
    statement_product: str
    account_numbers: List[str]
    card_numbers: List[str]
    parsed: dict
    transaction_count: int


def parse_scope(raw: Optional[list]) -> Scope:
    rules = raw or []
    if not isinstance(rules, list):
        raise RuntimeError("permission scope must be a list of typed rules")

    statement_products: Set[str] = set()
    account_numbers: Set[str] = set()
    card_numbers: Set[str] = set()

    for rule in rules:
        if not isinstance(rule, dict):
            raise RuntimeError("each permission rule must be an object")
        rule_type = str(rule.get("type", "")).strip()
        value = str(rule.get("value", "")).strip()
        if not rule_type or not value:
            continue
        if rule_type == "statement_product":
            statement_products.add(value)
        elif rule_type == "account_number":
            account_numbers.add(value)
        elif rule_type == "card_number":
            card_numbers.add(value)
        else:
            raise RuntimeError(f"unsupported permission type: {rule_type}")

    return Scope(
        statement_products=statement_products,
        account_numbers=account_numbers,
        card_numbers=card_numbers,
    )


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing config file: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def build_user_index(cfg: dict) -> Dict[str, User]:
    users: Dict[str, User] = {}
    for raw in cfg.get("users", []):
        token = str(raw.get("token", "")).strip()
        if not token:
            continue
        role = str(raw.get("role", "user")).strip().lower()
        perms = raw.get("permissions", {}) if role != "admin" else {}
        if role != "admin" and not isinstance(perms, dict):
            raise RuntimeError("permissions must be an object")
        if role != "admin" and "read" not in perms:
            raise RuntimeError("permissions.read is required for non-admin users")
        read_rules = perms.get("read")
        user = User(
            username=str(raw.get("username", "unknown")),
            token=token,
            role=role,
            read_scope=parse_scope(read_rules),
        )
        users[token] = user
    return users


def has_scope_match(
    scope: Scope,
    statement_product: str,
    account_number: str,
    card_number: str,
) -> bool:
    return (
        statement_product in scope.statement_products
        or account_number in scope.account_numbers
        or card_number in scope.card_numbers
    )


def has_full_statement_access(user: User, statement: Statement) -> bool:
    if user.role == "admin":
        return True
    return statement.statement_product in user.read_scope.statement_products


def can_see_statement_in_list(user: User, statement: Statement) -> bool:
    if user.role == "admin":
        return True
    if statement.statement_product in user.read_scope.statement_products:
        return True
    accounts = set(json.loads(statement.account_numbers_json))
    cards = set(json.loads(statement.card_numbers_json))

    if accounts.intersection(user.read_scope.account_numbers):
        return True
    if cards.intersection(user.read_scope.card_numbers):
        return True
    return False


def can_read_transaction(user: User, tx: Transaction) -> bool:
    if user.role == "admin":
        return True
    return has_scope_match(
        user.read_scope,
        tx.statement_product,
        tx.account_number,
        tx.card_number,
    )


def ensure_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin permission required")


def ensure_schema_compat(engine) -> None:
    # Lightweight migration for early schema revisions.
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(transactions)"))]
        if cols and "statement_product" not in cols:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN statement_product TEXT NOT NULL DEFAULT ''"))
        if cols and "account_currency" not in cols:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN account_currency TEXT NOT NULL DEFAULT ''"))


def backfill_account_currency(engine) -> None:
    # Fill missing account_currency for pre-migration rows using parsed statement payload.
    with engine.begin() as conn:
        missing_rows = conn.execute(
            text(
                """
                SELECT id, statement_id, account_number
                FROM transactions
                WHERE account_currency IS NULL OR account_currency = ''
                """
            )
        ).fetchall()
        if not missing_rows:
            return

        statement_rows = conn.execute(text("SELECT id, parsed_json FROM statements")).fetchall()
        account_currency_map: Dict[tuple[int, str], str] = {}
        for st_id, parsed_json_raw in statement_rows:
            try:
                parsed = json.loads(parsed_json_raw)
            except Exception:
                continue
            for acc in parsed.get("sub_accounts", []):
                account_number = str(acc.get("account_number", "")).strip()
                account_currency = str(acc.get("sub_account_currency", "")).strip()
                if account_number and account_currency:
                    account_currency_map[(int(st_id), account_number)] = account_currency

        for tx_id, st_id, account_number in missing_rows:
            ccy = account_currency_map.get((int(st_id), str(account_number)))
            if not ccy:
                continue
            conn.execute(
                text("UPDATE transactions SET account_currency = :ccy WHERE id = :id"),
                {"ccy": ccy, "id": int(tx_id)},
            )


def parse_iso_date_or_400(raw: str, field_name: str) -> str:
    value = (raw or "").strip()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {field_name}, expected YYYY-MM-DD")
    return parsed.isoformat()


def apply_transaction_read_scope(stmt, user: User):
    if user.role == "admin":
        return stmt
    scope_predicates = []
    if user.read_scope.statement_products:
        scope_predicates.append(Transaction.statement_product.in_(user.read_scope.statement_products))
    if user.read_scope.account_numbers:
        scope_predicates.append(Transaction.account_number.in_(user.read_scope.account_numbers))
    if user.read_scope.card_numbers:
        scope_predicates.append(Transaction.card_number.in_(user.read_scope.card_numbers))
    if not scope_predicates:
        # Return empty result for no read scope.
        return stmt.where(text("1 = 0"))
    return stmt.where(or_(*scope_predicates))


def build_parsed_statement_bundle(parsed: dict) -> ParsedStatementBundle:
    account_numbers: Set[str] = set()
    card_numbers: Set[str] = set()
    transaction_count = 0
    for acc in parsed.get("sub_accounts", []):
        account_number = str(acc.get("account_number", "")).strip()
        if account_number:
            account_numbers.add(account_number)
        for card in acc.get("cards", []):
            card_number = str(card.get("card_number", "")).strip()
            if card_number:
                card_numbers.add(card_number)
            transaction_count += len(card.get("transactions", []))

    return ParsedStatementBundle(
        statement_date=str(parsed.get("statement_date", "")),
        statement_product=str(parsed.get("statement_product", "")),
        account_numbers=sorted(account_numbers),
        card_numbers=sorted(card_numbers),
        parsed=parsed,
        transaction_count=transaction_count,
    )


def update_statement_from_bundle(statement: Statement, bundle: ParsedStatementBundle) -> None:
    statement.statement_date = bundle.statement_date
    statement.statement_product = bundle.statement_product
    statement.account_numbers_json = json.dumps(bundle.account_numbers)
    statement.card_numbers_json = json.dumps(bundle.card_numbers)
    statement.parsed_json = json.dumps(bundle.parsed, ensure_ascii=False)


def add_transactions_from_bundle(db: Session, statement: Statement, bundle: ParsedStatementBundle) -> None:
    for acc in bundle.parsed.get("sub_accounts", []):
        account_number = acc["account_number"]
        account_currency = acc["sub_account_currency"]
        for card in acc.get("cards", []):
            card_number = card["card_number"]
            cardholder_name = card["cardholder_name"]
            for tx in card.get("transactions", []):
                row = Transaction(
                    statement_id=statement.id,
                    statement_product=bundle.statement_product,
                    account_number=account_number,
                    card_number=card_number,
                    cardholder_name=cardholder_name,
                    account_currency=account_currency,
                    post_date=tx["post_date"],
                    transaction_date=tx["transaction_date"],
                    description=tx["description"],
                    amount=float(tx["amount"]),
                    signed_amount=float(tx["signed_amount"]),
                    is_credit=bool(tx["is_credit"]),
                    kind=tx["kind"],
                    payment_method=tx.get("payment_method"),
                    region_code_alpha2=tx.get("region_code_alpha2"),
                    currency=tx["currency"],
                    currency_amount=float(tx["currency_amount"]),
                    exchange_rate=float(tx["exchange_rate"]) if tx.get("exchange_rate") is not None else None,
                    notes_json=json.dumps(tx.get("notes", []), ensure_ascii=False),
                )
                db.add(row)


def create_app() -> FastAPI:
    cfg = load_config()

    db_path = Path(cfg.get("database", {}).get("sqlite_path", "web/data/app.db"))
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    upload_dir = Path(cfg.get("storage", {}).get("upload_dir", "web/data/uploads"))
    if not upload_dir.is_absolute():
        upload_dir = REPO_ROOT / upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(engine)
    ensure_schema_compat(engine)
    backfill_account_currency(engine)

    user_index = build_user_index(cfg)
    rebuild_status_lock = threading.Lock()
    rebuild_status = {
        "job_id": 0,
        "state": "idle",
        "phase": "idle",
        "requested_by": None,
        "started_at": None,
        "finished_at": None,
        "total_statements": 0,
        "processed_statements": 0,
        "statements_rebuilt": 0,
        "transactions_rebuilt": 0,
        "current_statement_id": None,
        "current_filename": None,
        "message": "",
        "error": None,
    }

    app = FastAPI(title="Statement Management API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.get("server", {}).get("cors_origins", ["*"]),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    bearer = HTTPBearer(auto_error=False)

    def get_db() -> Session:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(bearer),
    ) -> User:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = credentials.credentials.strip()
        user = user_index.get(token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid token")
        return user

    def snapshot_rebuild_status() -> dict:
        with rebuild_status_lock:
            return dict(rebuild_status)

    def update_rebuild_status(**updates) -> dict:
        with rebuild_status_lock:
            rebuild_status.update(updates)
            return dict(rebuild_status)

    def is_rebuild_running() -> bool:
        with rebuild_status_lock:
            return rebuild_status["state"] == "running"

    def rebuild_all_parsed_data(job_id: int, requested_by: str) -> None:
        try:
            with SessionLocal() as db:
                statements = db.scalars(select(Statement).order_by(Statement.id.asc())).all()

            total_statements = len(statements)
            prepared_rows: List[tuple[int, ParsedStatementBundle]] = []
            seen_accounts: Dict[tuple[str, str, str], int] = {}
            total_transactions = 0

            update_rebuild_status(
                job_id=job_id,
                state="running",
                phase="parsing",
                total_statements=total_statements,
                processed_statements=0,
                statements_rebuilt=0,
                transactions_rebuilt=0,
                current_statement_id=None,
                current_filename=None,
                message="Parsing stored statements",
                error=None,
            )

            for idx, statement in enumerate(statements, start=1):
                update_rebuild_status(
                    current_statement_id=statement.id,
                    current_filename=statement.original_filename,
                    processed_statements=idx - 1,
                    message=f"Parsing statement {idx}/{total_statements}",
                )

                stored_path = Path(statement.stored_path)
                if not stored_path.exists():
                    raise RuntimeError(
                        f"stored PDF not found for statement #{statement.id}: {statement.stored_path}"
                    )

                parsed = parse_statement(stored_path)
                bundle = build_parsed_statement_bundle(parsed)
                for account_number in bundle.account_numbers:
                    key = (bundle.statement_date, bundle.statement_product, account_number)
                    existing_statement_id = seen_accounts.get(key)
                    if existing_statement_id is not None and existing_statement_id != statement.id:
                        raise RuntimeError(
                            "duplicate statement detected during rebuild: "
                            f"statement_date={bundle.statement_date}, "
                            f"statement_product={bundle.statement_product}, "
                            f"account_number={account_number}, "
                            f"statement_ids={existing_statement_id},{statement.id}"
                        )
                    seen_accounts[key] = statement.id

                prepared_rows.append((statement.id, bundle))
                total_transactions += bundle.transaction_count

            update_rebuild_status(
                phase="writing",
                processed_statements=total_statements,
                current_statement_id=None,
                current_filename=None,
                message="Writing rebuilt statement and transaction data",
            )

            with SessionLocal() as db:
                db.execute(delete(Transaction))
                for statement_id, bundle in prepared_rows:
                    statement = db.get(Statement, statement_id)
                    if statement is None:
                        raise RuntimeError(f"statement #{statement_id} disappeared during rebuild")
                    update_statement_from_bundle(statement, bundle)
                    add_transactions_from_bundle(db, statement, bundle)
                db.commit()

            update_rebuild_status(
                state="completed",
                phase="completed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                processed_statements=total_statements,
                statements_rebuilt=total_statements,
                transactions_rebuilt=total_transactions,
                current_statement_id=None,
                current_filename=None,
                message=(
                    f"Rebuilt {total_statements} statements and {total_transactions} transactions"
                ),
                error=None,
            )
        except Exception as exc:
            update_rebuild_status(
                state="failed",
                phase="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                current_statement_id=None,
                current_filename=None,
                message="Database rebuild failed",
                error=str(exc),
            )

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/api/login")
    def login(payload: LoginRequest) -> dict:
        token = payload.token.strip()
        user = user_index.get(token)
        if not user:
            raise HTTPException(status_code=401, detail="invalid token")
        return {
            "username": user.username,
            "role": user.role,
            "token": user.token,
        }

    @app.get("/api/me")
    def me(user: User = Depends(get_current_user)) -> dict:
        return {
            "username": user.username,
            "role": user.role,
        }

    @app.post("/api/statements/upload")
    async def upload_statement(
        file: UploadFile = File(...),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict:
        ensure_admin(user)
        if is_rebuild_running():
            raise HTTPException(status_code=409, detail="database rebuild is in progress")

        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="only PDF is supported")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        safe_name = f"{stamp}_{os.path.basename(file.filename)}"
        stored_path = upload_dir / safe_name

        with stored_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

        try:
            parsed = parse_statement(stored_path)
        except ParseError as e:
            stored_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"parse failed: {e}")

        bundle = build_parsed_statement_bundle(parsed)

        existing_rows = db.scalars(
            select(Statement).where(
                Statement.statement_date == bundle.statement_date,
                Statement.statement_product == bundle.statement_product,
            )
        ).all()
        for existing in existing_rows:
            existing_accounts = set(json.loads(existing.account_numbers_json))
            duplicated_accounts = sorted(set(bundle.account_numbers).intersection(existing_accounts))
            if duplicated_accounts:
                stored_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "duplicate statement detected",
                        "existing_statement_id": existing.id,
                        "statement_date": existing.statement_date,
                        "statement_product": existing.statement_product,
                        "duplicated_account_numbers": duplicated_accounts,
                    },
                )

        st = Statement(
            original_filename=file.filename,
            stored_path=str(stored_path),
            statement_date=bundle.statement_date,
            statement_product=bundle.statement_product,
            uploaded_at=datetime.now(timezone.utc),
            uploaded_by=user.username,
            account_numbers_json=json.dumps(bundle.account_numbers),
            card_numbers_json=json.dumps(bundle.card_numbers),
            parsed_json=json.dumps(bundle.parsed, ensure_ascii=False),
        )
        db.add(st)
        db.flush()
        add_transactions_from_bundle(db, st, bundle)
        db.commit()

        return {
            "statement_id": st.id,
            "statement_date": st.statement_date,
            "statement_product": st.statement_product,
            "transactions_count": bundle.transaction_count,
        }

    @app.get("/api/admin/rebuild_database")
    def get_rebuild_database_status(user: User = Depends(get_current_user)) -> dict:
        ensure_admin(user)
        return snapshot_rebuild_status()

    @app.post("/api/admin/rebuild_database", status_code=202)
    def start_rebuild_database(user: User = Depends(get_current_user)) -> dict:
        ensure_admin(user)

        with rebuild_status_lock:
            if rebuild_status["state"] == "running":
                raise HTTPException(status_code=409, detail="database rebuild is already running")

            next_job_id = int(rebuild_status["job_id"]) + 1
            rebuild_status.update(
                {
                    "job_id": next_job_id,
                    "state": "running",
                    "phase": "queued",
                    "requested_by": user.username,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "finished_at": None,
                    "total_statements": 0,
                    "processed_statements": 0,
                    "statements_rebuilt": 0,
                    "transactions_rebuilt": 0,
                    "current_statement_id": None,
                    "current_filename": None,
                    "message": "Rebuild queued",
                    "error": None,
                }
            )
            snapshot = dict(rebuild_status)

        worker = threading.Thread(
            target=rebuild_all_parsed_data,
            args=(next_job_id, user.username),
            daemon=True,
        )
        worker.start()
        return snapshot

    @app.get("/api/statements")
    def list_statements(
        limit: int = Query(default=500, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict:
        rows = db.scalars(select(Statement).order_by(Statement.id.desc())).all()
        visible: List[dict] = []
        for st in rows:
            if not can_see_statement_in_list(user, st):
                continue
            has_full = has_full_statement_access(user, st)
            visible.append(
                {
                    "id": st.id,
                    "original_filename": st.original_filename,
                    "statement_date": st.statement_date,
                    "statement_product": st.statement_product,
                    "uploaded_at": st.uploaded_at.isoformat(),
                    "uploaded_by": st.uploaded_by,
                    "can_view_raw": has_full,
                    "can_view_pdf": has_full,
                    "can_view_tx": True,
                    "can_view_summary": True,
                }
            )

        total = len(visible)
        items = visible[offset : offset + limit]
        returned = len(items)
        has_more = offset + returned < total
        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "returned": returned,
            "total": total,
            "has_more": has_more,
        }

    @app.get("/api/statements/{statement_id}")
    def get_statement(
        statement_id: int,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict:
        st = db.get(Statement, statement_id)
        if not st:
            raise HTTPException(status_code=404, detail="statement not found")
        if not has_full_statement_access(user, st):
            raise HTTPException(status_code=403, detail="forbidden")

        parsed = json.loads(st.parsed_json)
        return {
            "id": st.id,
            "original_filename": st.original_filename,
            "statement_date": st.statement_date,
            "statement_product": st.statement_product,
            "uploaded_at": st.uploaded_at.isoformat(),
            "uploaded_by": st.uploaded_by,
            "parsed": parsed,
        }

    @app.get("/api/statements/{statement_id}/file")
    def get_statement_file(
        statement_id: int,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> FileResponse:
        st = db.get(Statement, statement_id)
        if not st:
            raise HTTPException(status_code=404, detail="statement not found")
        if not has_full_statement_access(user, st):
            raise HTTPException(status_code=403, detail="forbidden")
        path = Path(st.stored_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(path=str(path), filename=st.original_filename, media_type="application/pdf")

    @app.get("/api/transactions")
    def list_transactions(
        statement_id: Optional[int] = Query(default=None),
        statement_product: Optional[str] = Query(default=None),
        account_number: Optional[str] = Query(default=None),
        card_number: Optional[str] = Query(default=None),
        cardholder_name: Optional[str] = Query(default=None),
        tx_date_from: Optional[str] = Query(default=None),
        tx_date_to: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        limit: int = Query(default=500, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict:
        stmt = select(Transaction).order_by(Transaction.id.desc())

        date_from: Optional[str] = None
        date_to: Optional[str] = None
        if tx_date_from:
            date_from = parse_iso_date_or_400(tx_date_from, "tx_date_from")
        if tx_date_to:
            date_to = parse_iso_date_or_400(tx_date_to, "tx_date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=400, detail="tx_date_from must be <= tx_date_to")

        stmt = apply_transaction_read_scope(stmt, user)

        if statement_id is not None:
            stmt = stmt.where(Transaction.statement_id == statement_id)
        if statement_product:
            stmt = stmt.where(Transaction.statement_product == statement_product)
        if account_number:
            stmt = stmt.where(Transaction.account_number == account_number)
        if card_number:
            stmt = stmt.where(Transaction.card_number == card_number)
        if cardholder_name:
            stmt = stmt.where(Transaction.cardholder_name.ilike(f"%{cardholder_name}%"))
        if date_from:
            stmt = stmt.where(Transaction.transaction_date >= date_from)
        if date_to:
            stmt = stmt.where(Transaction.transaction_date <= date_to)
        if q:
            stmt = stmt.where(Transaction.description.ilike(f"%{q}%"))

        rows = db.scalars(stmt.offset(offset).limit(limit + 1)).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        out: List[dict] = []
        for tx in rows:
            out.append(
                {
                    "id": tx.id,
                    "statement_id": tx.statement_id,
                    "statement_product": tx.statement_product,
                    "account_number": tx.account_number,
                    "card_number": tx.card_number,
                    "cardholder_name": tx.cardholder_name,
                    "account_currency": tx.account_currency,
                    "post_date": tx.post_date,
                    "transaction_date": tx.transaction_date,
                    "description": tx.description,
                    "amount": tx.amount,
                    "signed_amount": tx.signed_amount,
                    "is_credit": tx.is_credit,
                    "kind": tx.kind,
                    "payment_method": tx.payment_method,
                    "region_code_alpha2": tx.region_code_alpha2,
                    "currency": tx.currency,
                    "currency_amount": tx.currency_amount,
                    "exchange_rate": tx.exchange_rate,
                    "notes": json.loads(tx.notes_json),
                    }
                )
        return {
            "items": out,
            "offset": offset,
            "limit": limit,
            "returned": len(out),
            "has_more": has_more,
        }

    @app.get("/api/statement_summary")
    def get_statement_summary(
        statement_id: int = Query(..., ge=1),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict:
        st = db.get(Statement, statement_id)
        if not st:
            raise HTTPException(status_code=404, detail="statement not found")

        stmt = select(Transaction).where(Transaction.statement_id == statement_id)
        stmt = apply_transaction_read_scope(stmt, user)
        rows = db.scalars(stmt).all()

        account_map: Dict[str, dict] = {}
        card_map: Dict[tuple[str, str], dict] = {}
        for tx in rows:
            acc = account_map.setdefault(
                tx.account_number,
                {
                    "account_number": tx.account_number,
                    "account_currency": tx.account_currency,
                    "spend": 0.0,
                    "refund": 0.0,
                    "payment": 0.0,
                    "cards": {},
                },
            )
            if not acc["account_currency"] and tx.account_currency:
                acc["account_currency"] = tx.account_currency

            amount = float(tx.amount)
            if not tx.is_credit:
                acc["spend"] += amount
            elif tx.kind == "payment":
                acc["payment"] += amount
            else:
                acc["refund"] += amount

            card_entry = acc["cards"].setdefault(
                tx.card_number,
                {
                    "card_number": tx.card_number,
                    "cardholder_name": tx.cardholder_name,
                    "account_currency": tx.account_currency,
                    "spend": 0.0,
                    "refund": 0.0,
                    "payment": 0.0,
                },
            )
            if not tx.is_credit:
                card_entry["spend"] += amount
            elif tx.kind == "payment":
                card_entry["payment"] += amount
            else:
                card_entry["refund"] += amount

            flat_card = card_map.setdefault(
                (tx.account_number, tx.card_number),
                {
                    "account_number": tx.account_number,
                    "card_number": tx.card_number,
                    "cardholder_name": tx.cardholder_name,
                    "account_currency": tx.account_currency,
                    "spend": 0.0,
                    "refund": 0.0,
                    "payment": 0.0,
                },
            )
            if not tx.is_credit:
                flat_card["spend"] += amount
            elif tx.kind == "payment":
                flat_card["payment"] += amount
            else:
                flat_card["refund"] += amount

        accounts: List[dict] = []
        for account_number in sorted(account_map.keys()):
            item = account_map[account_number]
            cards = []
            for card_number in sorted(item["cards"].keys()):
                c = item["cards"][card_number]
                cards.append(
                    {
                        "card_number": c["card_number"],
                        "cardholder_name": c["cardholder_name"],
                        "account_currency": c["account_currency"],
                        "spend": round(float(c["spend"]), 2),
                        "refund": round(float(c["refund"]), 2),
                        "net_spend": round(float(c["spend"] - c["refund"]), 2),
                        "payment": round(float(c["payment"]), 2),
                    }
                )
            accounts.append(
                {
                    "account_number": item["account_number"],
                    "account_currency": item["account_currency"],
                    "spend": round(float(item["spend"]), 2),
                    "refund": round(float(item["refund"]), 2),
                    "net_spend": round(float(item["spend"] - item["refund"]), 2),
                    "payment": round(float(item["payment"]), 2),
                    "cards": cards,
                }
            )

        cards = [
            {
                "account_number": item["account_number"],
                "card_number": item["card_number"],
                "cardholder_name": item["cardholder_name"],
                "account_currency": item["account_currency"],
                "spend": round(float(item["spend"]), 2),
                "refund": round(float(item["refund"]), 2),
                "net_spend": round(float(item["spend"] - item["refund"]), 2),
                "payment": round(float(item["payment"]), 2),
            }
            for _, item in sorted(card_map.items(), key=lambda kv: (kv[0][0], kv[0][1]))
        ]

        show_stmt_meta = can_see_statement_in_list(user, st)
        return {
            "statement_id": st.id,
            "statement_date": st.statement_date if show_stmt_meta else None,
            "statement_product": st.statement_product if show_stmt_meta else None,
            "accounts": accounts,
            "cards": cards,
        }

    @app.get("/", include_in_schema=False)
    def serve_index() -> FileResponse:
        index_file = FRONTEND_DIR / "index.html"
        if not index_file.exists():
            raise HTTPException(status_code=404, detail="frontend index not found")
        return FileResponse(path=str(index_file), media_type="text/html")

    @app.get("/{asset_path:path}", include_in_schema=False)
    def serve_frontend_assets(asset_path: str) -> FileResponse:
        if asset_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")

        root = FRONTEND_DIR.resolve()
        candidate = (FRONTEND_DIR / asset_path).resolve()
        if root not in candidate.parents and candidate != root:
            raise HTTPException(status_code=404, detail="not found")

        if candidate.is_file():
            return FileResponse(path=str(candidate))

        index_file = FRONTEND_DIR / "index.html"
        if index_file.exists():
            return FileResponse(path=str(index_file), media_type="text/html")
        raise HTTPException(status_code=404, detail="frontend asset not found")

    return app


app = create_app()
