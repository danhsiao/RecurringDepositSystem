# Recurring Deposit System

A proof-of-concept Fintech application demonstrating a distributed, event-driven architecture for processing recurring deposits. The core goal is to show how a naive monolithic CRON job can be decoupled into a scalable **Message Queue + Worker** pattern.

## Architecture Overview

```
┌─────────────┐     HTTP      ┌──────────────────┐     Enqueue     ┌───────────────┐
│  Frontend   │ ────────────► │   FastAPI (API)  │ ──────────────► │ Upstash Redis │
│ (Jinja2/JS) │              │  (Render Web Svc) │                 │  (MQ Broker)  │
└─────────────┘              └──────────────────┘                 └───────┬───────┘
                                      │                                   │
                                      │ SQLAlchemy                        │ Dequeue
                                      ▼                                   ▼
                             ┌────────────────┐              ┌────────────────────┐
                             │   PostgreSQL   │◄─────────────│  Celery Workers    │
                             │  (Supabase)    │   DB writes  │ (Render Background)│
                             └────────────────┘              └────────────────────┘
```

## Key Concepts Demonstrated

- **Decoupled CRON via Message Queue**: Instead of a single blocking CRON job processing all deposits sequentially, a scheduler enqueues individual deposit tasks. Celery workers pick them up in parallel — horizontally scalable.
- **Async Settlement via Webhook Simulation**: Deposit processing is two-phase (Pending → Success/Failed), mimicking real-world async settlement flows (e.g., DriveWealth webhooks).
- **Idempotency Keys**: Each transaction is created with a unique key to prevent duplicate processing on retries.

## Tech Stack (100% Free Cloud)

| Layer | Technology | Host |
|---|---|---|
| Frontend | Vanilla HTML/JS + Tailwind CSS (via Jinja2) | Render (bundled with API) |
| Backend API | Python + FastAPI | Render Web Service |
| Background Worker | Celery | Render Background Worker |
| Database | PostgreSQL + SQLAlchemy | Supabase |
| Message Queue | Redis (Serverless) | Upstash |

## Flow

### 1. Schedule Creation
User submits a deposit rule (amount, frequency, account). Saved to `recurring_deposits` table with a computed `next_run_date`.

### 2. Initiation Flow (Simulates daily CRON)
Clicking **"Simulate Midnight CRON"** hits `POST /trigger-run`. The API queries for all due deposits, enqueues each as a separate Celery task. Each worker:
- Creates a `Transaction` record with `status=Pending`
- Updates `next_run_date` on the schedule

### 3. Settlement Flow (Simulates DriveWealth Webhook)
Clicking **"Simulate Webhook Update"** on a pending transaction hits `POST /settle/{transaction_id}`. The API enqueues a settlement task. The worker:
- Calls a mock DriveWealth settlement function (randomly succeeds/fails)
- Updates `Transaction.status` to `Success` or `Failed`
- On success, increments `Account.balance`

## Local Development

```bash
# 1. Clone and set up environment
git clone https://github.com/danhsiao/RecurringDepositSystem.git
cd RecurringDepositSystem
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Copy and fill in your secrets
cp .env.example .env

# 3. Run the API
uvicorn app.main:app --reload

# 4. Run the Celery worker (separate terminal)
celery -A app.celery_app worker --loglevel=info
```

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (from Supabase) |
| `CELERY_BROKER_URL` | Redis URL (from Upstash, use `rediss://` for TLS) |
| `CELERY_RESULT_BACKEND` | Same Redis URL for storing task results |

## Deployment (Render)

Services are defined in `render.yaml`. Connect your GitHub repo to Render and it will automatically provision:
- **Web Service**: Runs `uvicorn app.main:app`
- **Background Worker**: Runs `celery -A app.celery_app worker`

Both services share the same environment variables set in the Render dashboard.

> **Note on cold starts**: Render free-tier spins down after 15 minutes of inactivity. The first request may take ~30 seconds to wake the server. This is expected behavior.
