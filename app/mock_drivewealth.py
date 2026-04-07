"""
Mock DriveWealth API client.

In production these would be real HTTP calls to DriveWealth's brokerage API.
Here we simulate network latency and probabilistic outcomes so we can demo
the full async flow without any third-party credentials.
"""

import random
import time
import uuid


def initiate_deposit(account_id: str, amount: float) -> dict:
    """
    Simulate sending a deposit instruction to DriveWealth.

    Returns a dict with a `dw_reference_id` that would be stored and later
    used to look up settlement status via webhook or polling.
    """
    # Simulate ~200ms network round-trip
    time.sleep(0.2)

    # 5% chance the initiation itself is rejected (e.g. account not found)
    if random.random() < 0.05:
        raise RuntimeError(
            f"[DriveWealth Mock] Deposit initiation rejected for account {account_id}"
        )

    reference_id = f"DW-{uuid.uuid4().hex[:12].upper()}"
    return {
        "dw_reference_id": reference_id,
        "account_id": account_id,
        "amount": amount,
        "status": "PENDING",
    }


def settle_deposit(dw_reference_id: str) -> dict:
    """
    Simulate DriveWealth's async settlement callback / webhook payload.

    In reality DriveWealth POSTs a webhook to our server once settlement
    is complete.  Here we call this synchronously from the Celery task to
    mimic that final status update.

    Outcomes (weighted):
        80% SUCCESS  — deposit cleared
        20% FAILED   — insufficient funds / compliance hold / etc.
    """
    # Simulate settlement processing delay (~300ms)
    time.sleep(0.3)

    outcome = random.choices(
        ["SUCCESS", "FAILED"],
        weights=[80, 20],
        k=1,
    )[0]

    return {
        "dw_reference_id": dw_reference_id,
        "settlement_status": outcome,
        "message": (
            "Deposit cleared successfully."
            if outcome == "SUCCESS"
            else "Settlement failed: insufficient funds or compliance hold."
        ),
    }
