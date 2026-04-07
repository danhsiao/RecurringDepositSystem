import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models import Account, FrequencyEnum, Notification, RecurringDeposit, Transaction, StatusEnum
from app.tasks import process_deposit, settle_transaction

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Recurring Deposit System")

templates = Jinja2Templates(directory="app/templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create all tables on startup (idempotent — safe to run on every cold start)
@app.on_event("startup")
def create_tables():
    Base.metadata.create_all(bind=engine)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class AccountCreate(BaseModel):
    user_id: str
    account_name: str
    balance: float = 0.0


class AccountOut(BaseModel):
    id: str
    user_id: str
    account_name: str
    balance: float

    model_config = {"from_attributes": True}


class DepositCreate(BaseModel):
    account_id: str
    amount: float = Field(gt=0)
    frequency: FrequencyEnum


class DepositOut(BaseModel):
    id: str
    account_id: str
    amount: float
    frequency: FrequencyEnum
    next_run_date: datetime
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TransactionOut(BaseModel):
    id: str
    recurring_deposit_id: str
    amount: float
    status: StatusEnum
    created_at: datetime
    idempotency_key: str

    model_config = {"from_attributes": True}


class NotificationOut(BaseModel):
    id: str
    account_id: str
    message: str
    read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@app.post("/accounts", response_model=AccountOut, status_code=201)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    account = Account(
        id=str(uuid.uuid4()),
        user_id=payload.user_id,
        account_name=payload.account_name,
        balance=payload.balance,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@app.get("/accounts", response_model=list[AccountOut])
def list_accounts(user_id: str = "demo_user", db: Session = Depends(get_db)):
    return db.query(Account).filter(Account.user_id == user_id).all()


@app.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: str, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


# ---------------------------------------------------------------------------
# Recurring Deposits (Schedules)
# ---------------------------------------------------------------------------

@app.post("/deposits", response_model=DepositOut, status_code=201)
def create_deposit(payload: DepositCreate, db: Session = Depends(get_db)):
    account = db.get(Account, payload.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Check for any existing schedule with the same amount + frequency (active or paused)
    existing = (
        db.query(RecurringDeposit)
        .filter(
            RecurringDeposit.account_id == payload.account_id,
            RecurringDeposit.amount == payload.amount,
            RecurringDeposit.frequency == payload.frequency,
        )
        .first()
    )
    if existing and existing.active:
        raise HTTPException(
            status_code=409,
            detail={
                "type": "active_duplicate",
                "message": f"An active {payload.frequency} deposit of ${float(payload.amount):,.2f} already exists.",
            },
        )
    if existing and not existing.active:
        raise HTTPException(
            status_code=409,
            detail={
                "type": "paused_duplicate",
                "message": f"A paused {payload.frequency} deposit of ${float(payload.amount):,.2f} already exists.",
                "deposit_id": existing.id,
            },
        )

    now = datetime.now(timezone.utc)
    deposit = RecurringDeposit(
        id=str(uuid.uuid4()),
        account_id=payload.account_id,
        amount=payload.amount,
        frequency=payload.frequency,
        next_run_date=now,
        active=True,
        created_at=now,
    )
    db.add(deposit)
    db.commit()
    db.refresh(deposit)
    return deposit


@app.get("/accounts/{account_id}/deposits", response_model=list[DepositOut])
def list_deposits(account_id: str, db: Session = Depends(get_db)):
    return (
        db.query(RecurringDeposit)
        .filter(RecurringDeposit.account_id == account_id)
        .order_by(RecurringDeposit.created_at.asc())
        .all()
    )


@app.delete("/deposits/{deposit_id}", status_code=204)
def pause_deposit(deposit_id: str, db: Session = Depends(get_db)):
    """Soft-delete: marks schedule as inactive but preserves transaction history."""
    deposit = db.get(RecurringDeposit, deposit_id)
    if not deposit:
        raise HTTPException(status_code=404, detail="Deposit not found")
    deposit.active = False
    db.commit()


@app.patch("/deposits/{deposit_id}/activate", response_model=DepositOut)
def unpause_deposit(deposit_id: str, db: Session = Depends(get_db)):
    """Reactivates a paused schedule. Resets next_run_date to now so it's immediately eligible."""
    deposit = db.get(RecurringDeposit, deposit_id)
    if not deposit:
        raise HTTPException(status_code=404, detail="Deposit not found")
    deposit.active = True
    deposit.next_run_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(deposit)
    return deposit


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@app.get("/accounts/{account_id}/transactions", response_model=list[TransactionOut])
def list_transactions(account_id: str, db: Session = Depends(get_db)):
    """Return all transactions for every deposit linked to this account."""
    deposit_ids = (
        db.query(RecurringDeposit.id)
        .filter(RecurringDeposit.account_id == account_id)
        .subquery()
    )
    return (
        db.query(Transaction)
        .filter(Transaction.recurring_deposit_id.in_(deposit_ids))
        .order_by(Transaction.created_at.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# Developer / Demo Endpoints
# ---------------------------------------------------------------------------

@app.post("/trigger-run")
def trigger_run(db: Session = Depends(get_db)):
    """
    Simulate the Midnight CRON job.

    Finds all active RecurringDeposits whose next_run_date is due (<= now)
    and enqueues a process_deposit Celery task for each one.
    """
    now = datetime.now(timezone.utc)
    due_deposits = (
        db.query(RecurringDeposit)
        .filter(RecurringDeposit.active == True, RecurringDeposit.next_run_date <= now)
        .all()
    )

    if not due_deposits:
        return {"message": "No deposits due at this time.", "enqueued": 0}

    enqueued = []
    for deposit in due_deposits:
        task = process_deposit.delay(deposit.id)
        enqueued.append({"deposit_id": deposit.id, "task_id": task.id})

    return {
        "message": f"Enqueued {len(enqueued)} deposit task(s).",
        "enqueued": len(enqueued),
        "tasks": enqueued,
    }


@app.post("/settle/{transaction_id}")
def trigger_settlement(transaction_id: str, db: Session = Depends(get_db)):
    """
    Simulate a DriveWealth settlement webhook for a specific transaction.

    Validates the transaction exists and is Pending, then enqueues
    a settle_transaction Celery task.
    """
    transaction = db.get(Transaction, transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if transaction.status != StatusEnum.pending:
        raise HTTPException(
            status_code=400,
            detail=f"Transaction is already in '{transaction.status}' state.",
        )

    task = settle_transaction.delay(transaction_id)
    return {
        "message": "Settlement task enqueued.",
        "transaction_id": transaction_id,
        "task_id": task.id,
    }


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@app.get("/notifications/{account_id}", response_model=list[NotificationOut])
def get_notifications(account_id: str, unread_only: bool = False, db: Session = Depends(get_db)):
    q = db.query(Notification).filter(Notification.account_id == account_id)
    if unread_only:
        q = q.filter(Notification.read == False)
    return q.order_by(Notification.created_at.desc()).all()


@app.post("/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_notification_read(notification_id: str, db: Session = Depends(get_db)):
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.read = True
    db.commit()
    db.refresh(notification)
    return notification


# ---------------------------------------------------------------------------
# Seed endpoint — creates demo accounts so there is data to play with
# ---------------------------------------------------------------------------

@app.post("/seed", status_code=201)
def seed_demo_data(db: Session = Depends(get_db)):
    """
    Creates two demo accounts for user 'demo_user' if they don't exist yet.
    Safe to call multiple times (idempotent by account_name).
    """
    existing = db.query(Account).filter(Account.user_id == "demo_user").count()
    if existing >= 2:
        return {"message": "Demo data already seeded.", "seeded": False}

    accounts = [
        Account(id=str(uuid.uuid4()), user_id="demo_user", account_name="Brokerage Account", balance=0.0),
        Account(id=str(uuid.uuid4()), user_id="demo_user", account_name="Retirement (IRA)", balance=0.0),
    ]
    for a in accounts:
        db.add(a)
    db.commit()
    return {"message": "Demo accounts created.", "seeded": True, "count": len(accounts)}
