import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Numeric, Boolean, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

import enum


class FrequencyEnum(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class StatusEnum(str, enum.Enum):
    pending = "Pending"
    success = "Success"
    failed = "Failed"


def _uuid() -> str:
    return str(uuid.uuid4())


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_name: Mapped[str] = mapped_column(String, nullable=False)
    balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0, nullable=False)

    deposits: Mapped[list["RecurringDeposit"]] = relationship(
        "RecurringDeposit", back_populates="account"
    )


class RecurringDeposit(Base):
    __tablename__ = "recurring_deposits"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    frequency: Mapped[FrequencyEnum] = mapped_column(SAEnum(FrequencyEnum), nullable=False)
    next_run_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    account: Mapped["Account"] = relationship("Account", back_populates="deposits")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="recurring_deposit"
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    recurring_deposit_id: Mapped[str] = mapped_column(
        String, ForeignKey("recurring_deposits.id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[StatusEnum] = mapped_column(
        SAEnum(StatusEnum), default=StatusEnum.pending, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    recurring_deposit: Mapped["RecurringDeposit"] = relationship(
        "RecurringDeposit", back_populates="transactions"
    )
