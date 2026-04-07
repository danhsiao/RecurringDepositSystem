"""
Celery tasks.

Two task families:
  1. process_deposit(deposit_id)  — initiated by the "Simulate Midnight CRON" flow.
     Creates a Pending transaction and advances next_run_date.

  2. settle_transaction(transaction_id)  — initiated by the "Simulate Webhook" flow.
     Calls mock DriveWealth settlement and updates status + account balance.
"""

import uuid
import logging
from datetime import datetime, timezone, timedelta

from celery import shared_task
from sqlalchemy.exc import IntegrityError

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import Account, Notification, RecurringDeposit, Transaction, StatusEnum, FrequencyEnum
from app.mock_drivewealth import initiate_deposit, settle_deposit

logger = logging.getLogger(__name__)


def _next_run(frequency: FrequencyEnum, from_date: datetime) -> datetime:
    """Compute the next run date given a frequency and the current run date."""
    if frequency == FrequencyEnum.daily:
        return from_date + timedelta(days=1)
    elif frequency == FrequencyEnum.weekly:
        return from_date + timedelta(weeks=1)
    elif frequency == FrequencyEnum.monthly:
        # Add ~30 days; good enough for a POC
        return from_date + timedelta(days=30)
    raise ValueError(f"Unknown frequency: {frequency}")


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, name="app.tasks.process_deposit")
def process_deposit(self, deposit_id: str):
    """
    Worker task: process a single due recurring deposit.

    Steps:
      1. Load the RecurringDeposit record.
      2. Generate an idempotency key — prevents duplicate transactions if the
         task is retried after a partial failure.
      3. Call mock DriveWealth initiation.
      4. Persist a Pending Transaction.
      5. Advance next_run_date so the schedule is ready for the next cycle.
    """
    db = SessionLocal()
    try:
        deposit = db.get(RecurringDeposit, deposit_id)
        if not deposit or not deposit.active:
            logger.warning(f"Deposit {deposit_id} not found or inactive — skipping.")
            return {"status": "skipped", "deposit_id": deposit_id}

        # Idempotency key ties this attempt to this specific run date
        idempotency_key = f"{deposit_id}:{deposit.next_run_date.isoformat()}"

        # Check whether we already have a transaction for this run (retry safety)
        existing = (
            db.query(Transaction)
            .filter(Transaction.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            logger.info(f"Transaction already exists for key {idempotency_key} — idempotent skip.")
            return {"status": "already_processed", "transaction_id": existing.id}

        # Call mock DriveWealth — may raise on rejection
        try:
            dw_response = initiate_deposit(deposit.account_id, float(deposit.amount))
        except RuntimeError as exc:
            logger.error(f"DriveWealth initiation failed: {exc}")
            raise self.retry(exc=exc)

        # Persist Pending transaction
        transaction = Transaction(
            id=str(uuid.uuid4()),
            recurring_deposit_id=deposit_id,
            amount=deposit.amount,
            status=StatusEnum.pending,
            created_at=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
        )
        db.add(transaction)

        # Advance the schedule
        deposit.next_run_date = _next_run(deposit.frequency, deposit.next_run_date)

        db.commit()
        db.refresh(transaction)

        logger.info(
            f"Created Pending transaction {transaction.id} for deposit {deposit_id} "
            f"(DW ref: {dw_response['dw_reference_id']})"
        )
        return {"status": "pending", "transaction_id": transaction.id}

    except IntegrityError:
        db.rollback()
        logger.warning(f"IntegrityError for deposit {deposit_id} — likely a duplicate, skipping.")
        return {"status": "duplicate_skipped", "deposit_id": deposit_id}
    except Exception as exc:
        db.rollback()
        logger.exception(f"Unexpected error processing deposit {deposit_id}: {exc}")
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, name="app.tasks.settle_transaction")
def settle_transaction(self, transaction_id: str):
    """
    Worker task: handle a DriveWealth settlement result (simulates webhook).

    handleDepositUpdate logic:
      SUCCESS → mark Transaction.status = Success, credit Account.balance.
      FAILURE → mark Transaction.status = Failed, deactivate the RecurringDeposit
                (active = False), and create a Notification for the customer.

    Note: deactivating on a single failure is intentional for the POC demo.
    In production you would track consecutive_failures and pause after N attempts.
    """
    db = SessionLocal()
    try:
        transaction = db.get(Transaction, transaction_id)
        if not transaction:
            logger.warning(f"Transaction {transaction_id} not found.")
            return {"status": "not_found", "transaction_id": transaction_id}

        if transaction.status != StatusEnum.pending:
            logger.info(f"Transaction {transaction_id} already settled ({transaction.status}) — skipping.")
            return {"status": "already_settled", "transaction_id": transaction_id}

        deposit = db.get(RecurringDeposit, transaction.recurring_deposit_id)

        # Simulate DriveWealth settlement callback
        dw_result = settle_deposit(f"DW-REF-{transaction_id[:8].upper()}")

        if dw_result["settlement_status"] == "SUCCESS":
            # --- Success path ---
            transaction.status = StatusEnum.success

            if deposit:
                account = db.get(Account, deposit.account_id)
                if account:
                    account.balance = float(account.balance) + float(transaction.amount)

            db.commit()
            logger.info(f"Transaction {transaction_id} settled SUCCESS — balance updated.")
            return {"status": "success", "transaction_id": transaction_id}

        else:
            # --- Failure path ---
            transaction.status = StatusEnum.failed

            # Deactivate the recurring schedule so no further deposits are attempted
            if deposit:
                deposit.active = False
                account_id = deposit.account_id

                # Create an in-app notification for the customer
                # In production this would also fire an automated email (SendGrid / AWS SES)
                notification = Notification(
                    account_id=account_id,
                    message=(
                        f"Your recurring deposit of ${float(transaction.amount):,.2f} "
                        f"(schedule ID: {deposit.id[:8]}) failed to settle. "
                        f"Reason: {dw_result['message']} "
                        f"The schedule has been paused — please review and reactivate."
                    ),
                )
                db.add(notification)

            db.commit()
            logger.warning(
                f"Transaction {transaction_id} settled FAILED — schedule deactivated, notification created."
            )
            return {
                "status": "failed",
                "transaction_id": transaction_id,
                "reason": dw_result["message"],
                "schedule_deactivated": True,
            }

    except Exception as exc:
        db.rollback()
        logger.exception(f"Unexpected error settling transaction {transaction_id}: {exc}")
        raise self.retry(exc=exc)
    finally:
        db.close()
