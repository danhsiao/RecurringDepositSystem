# Recurring Deposit System

A proof-of-concept Fintech application demonstrating a distributed, event-driven architecture for processing recurring deposits. The core goal is to show how a naive monolithic CRON job can be decoupled into a scalable **Message Queue + Worker** pattern.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENT BROWSER                                  │
│  Vanilla HTML/JS + Tailwind CSS (served by FastAPI via Jinja2 templates)    │
│  Polls /transactions and /notifications every 4 seconds for live updates    │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │ HTTP (JSON REST)
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FastAPI  —  Render Web Service                       │
│                                                                               │
│  Responsibilities:                                                            │
│  • Serve the dashboard HTML (GET /)                                          │
│  • CRUD for Accounts, Recurring Deposits, Notifications                      │
│  • Validate requests & enforce business rules (no duplicate schedules)        │
│  • Enqueue Celery tasks — does NOT process deposits itself                   │
│  • Read-only DB queries for the UI (transactions, balances)                  │
└──────────┬────────────────────────────────────┬──────────────────────────────┘
           │ SQLAlchemy (reads + enqueue signal) │ .delay() — pushes task message
           │                                     ▼
           │                    ┌────────────────────────────┐
           │                    │   Upstash Redis (TLS)       │
           │                    │   Serverless Message Queue  │
           │                    │                             │
           │                    │  Queue: process_deposit     │
           │                    │  Queue: settle_transaction  │
           │                    └──────────────┬─────────────┘
           │                                   │ Celery dequeues task
           │                                   ▼
           │                    ┌────────────────────────────┐
           │                    │  Celery Worker             │
           │                    │  Render Background Worker  │
           │                    │                            │
           │                    │  • Calls mock DriveWealth  │
           │                    │  • Writes to PostgreSQL    │
           │                    │  • Retries on failure (3x) │
           │                    └──────────────┬─────────────┘
           │                                   │ SQLAlchemy (writes)
           ▼                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PostgreSQL  —  Supabase                                   │
│   accounts · recurring_deposits · transactions · notifications               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Role |
|---|---|
| **FastAPI** | HTTP layer — validates input, enforces business rules, enqueues work, serves UI |
| **Upstash Redis** | Message broker — decouples the API from the workers. The API never waits for a deposit to process |
| **Celery Worker** | Executes deposit logic asynchronously — calls DriveWealth, writes results to DB |
| **Supabase PostgreSQL** | Single source of truth for all state |
| **Frontend (JS)** | Polls the API every 4 seconds to reflect worker results without websockets |

---

## Data Flow

### Flow 1 — Creating a Schedule

```
User fills form → POST /deposits
       │
       ├─ Validate: account exists?
       ├─ Validate: no duplicate active schedule (same amount + frequency)?
       │
       └─ INSERT recurring_deposits (active=true, next_run_date=now)
              └─ Return schedule to UI
```

A schedule is just a rule stored in the DB. No money moves yet.

---

### Flow 2 — Initiation (Simulates Midnight CRON)

```
User clicks "Simulate CRON" → POST /trigger-run
       │
       ├─ SELECT recurring_deposits
       │    WHERE active = true AND next_run_date <= NOW()
       │
       └─ For each due deposit:
              └─ celery.delay(process_deposit, deposit_id)
                     │         [task pushed to Redis queue]
                     │
                     └─ Worker picks up task:
                            ├─ Build idempotency_key = "{deposit_id}:{next_run_date}"
                            ├─ Check: transaction with this key already exists? → skip (safe retry)
                            ├─ Call mock DriveWealth initiate_deposit()
                            ├─ INSERT transactions (status=Pending)
                            └─ UPDATE recurring_deposits.next_run_date += frequency interval
```

**Why the idempotency key?** If the worker crashes after inserting the transaction but before acknowledging the message, Redis will re-deliver the task. The `UNIQUE` constraint on `idempotency_key` means the retry hits a duplicate error and skips safely — no double-charging.

**Why enqueue instead of processing inline?** The API returns immediately to the UI. Workers process all deposits in parallel. If you have 10,000 due deposits, the API doesn't block for 10,000 sequential DB writes — it just pushes 10,000 messages and returns.

---

### Flow 3 — Settlement (Simulates DriveWealth Webhook)

```
User clicks "Settle" on a Pending transaction → POST /settle/{transaction_id}
       │
       ├─ Validate: transaction exists and status == Pending
       │
       └─ celery.delay(settle_transaction, transaction_id)
                  │         [task pushed to Redis queue]
                  │
                  └─ Worker picks up task:
                         ├─ Call mock DriveWealth settle_deposit() → 80% SUCCESS / 20% FAILED
                         │
                         ├─ [SUCCESS path]
                         │       ├─ UPDATE transactions.status = 'Success'
                         │       └─ UPDATE accounts.balance += transaction.amount
                         │
                         └─ [FAILURE path]
                                 ├─ UPDATE transactions.status = 'Failed'
                                 ├─ UPDATE recurring_deposits.active = False  ← pauses schedule
                                 └─ INSERT notifications (message, account_id, read=false)
```

**Why two phases (Pending → Success/Failed)?** This mirrors how real brokerage APIs work. DriveWealth doesn't settle instantly — they accept the instruction (Pending), process it overnight, then POST a webhook with the result. Our "Simulate Webhook" button replays that async callback.

**Why pause the schedule on failure?** A failed settlement usually means something is wrong — insufficient funds, a compliance hold, an expired bank link. Auto-retrying would likely fail again and could violate regulations. The correct pattern is to pause, notify the user, and require manual reactivation.

---

### Flow 4 — Notifications

```
[After settlement failure, worker creates a Notification record]
       │
Frontend polls GET /notifications/{account_id} every 4 seconds
       │
       ├─ Unread notifications → bell badge count + toast banner
       └─ User clicks ✓ → POST /notifications/{id}/read → read=true
```

In production this would also fire a transactional email via **SendGrid or AWS SES** at the point of the `INSERT notifications` in the worker. The DB record is the audit trail; the email is the delivery mechanism.

---

## Database Schema

Four tables, all using UUID primary keys stored as `varchar`.

```
accounts
├── id               varchar        PK
├── user_id          varchar        indexed — scopes accounts to a user
├── account_name     varchar
└── balance          numeric(12,2)  credited on successful settlement

recurring_deposits
├── id               varchar        PK
├── account_id       varchar        FK → accounts.id
├── amount           numeric(12,2)
├── frequency        enum           daily | weekly | monthly
├── next_run_date    timestamptz    indexed — CRON query filters on this column
├── active           bool           false = paused (on failure or manual pause)
└── created_at       timestamptz    used for sort order + accordion disambiguation

transactions
├── id               varchar        PK
├── recurring_deposit_id  varchar   FK → recurring_deposits.id
├── amount           numeric(12,2)
├── status           enum           Pending | Success | Failed
├── created_at       timestamptz
└── idempotency_key  varchar        UNIQUE — "{deposit_id}:{next_run_date}"

notifications
├── id               varchar        PK
├── account_id       varchar        FK → accounts.id, indexed
├── message          varchar        human-readable failure description
├── read             bool           toggled by user via UI
└── created_at       timestamptz
```

### Entity Relationships

```
accounts ──< recurring_deposits ──< transactions
    └─────────────────────────────< notifications
```

One account has many schedules. Each schedule generates one transaction per CRON run. Notifications are tied directly to the account (not the transaction) so they survive schedule deletion.

### Key Design Decisions

| Decision | Reason |
|---|---|
| `next_run_date` is indexed | The CRON query `WHERE active=true AND next_run_date <= now()` runs as a fast index scan, not a full table scan — critical at scale |
| `idempotency_key` is unique | Guarantees exactly-once transaction creation even if a Celery task is retried after a partial failure |
| Soft-delete (`active=false`) instead of hard-delete | Preserves the full transaction audit trail linked to the schedule |
| `created_at` on `recurring_deposits` | Enables chronological sort and disambiguates two schedules with the same amount/frequency in the UI |
| Notifications linked to `account_id`, not `transaction_id` | Notifications survive if the linked schedule is paused or removed |

---

## Tech Stack (100% Free Cloud)

| Layer | Technology | Host |
|---|---|---|
| Frontend | Vanilla HTML/JS + Tailwind CSS (Jinja2) | Render (bundled with API) |
| Backend API | Python + FastAPI | Render Web Service |
| Background Worker | Celery | Render Background Worker |
| Database | PostgreSQL + SQLAlchemy | Supabase |
| Message Queue | Redis (Serverless, TLS) | Upstash |

---

## Scalability & Future Improvements

### Microservice Split (CQRS)
Currently a single FastAPI service handles both writes (create schedule, enqueue tasks) and reads (fetch ledger, balance). At scale, these would split into separate services:
- **Write service** — accepts new schedules, enqueues CRON tasks. Optimized for throughput.
- **Read service** — serves the ledger and balance APIs. Optimized for low-latency reads, potentially backed by a read replica.

### Infrastructure Routing
In a production AWS environment, an **API Gateway + Load Balancer** would sit in front of the web service for rate limiting, auth, and traffic routing. For this POC, Render's built-in platform routing handles this automatically.

### Worker Scaling
Celery workers are stateless — you can horizontally scale by increasing the `--concurrency` flag or spinning up more worker instances. Each worker independently dequeues from Redis, so there's no coordination overhead.

### Email Notifications
The `Notification` DB record is the hook point for a real email integration. In production, the worker would call SendGrid or AWS SES at the same point it writes the notification row, so the email and the audit record are always in sync.

---

## Local Development

```bash
# 1. Clone and set up environment
git clone https://github.com/danhsiao/RecurringDepositSystem.git
cd RecurringDepositSystem
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Copy and fill in your secrets
cp .env.example .env

# 3. Run the API (Terminal 1)
uvicorn app.main:app --reload

# 4. Run the Celery worker (Terminal 2)
celery -A app.celery_app worker --loglevel=info
```

Open `http://localhost:8000` in your browser.

> **Note for Supabase:** If you added `created_at` to an existing `recurring_deposits` table, run this once in the Supabase SQL editor:
> ```sql
> ALTER TABLE recurring_deposits ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
> ```

---

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (from Supabase) |
| `CELERY_BROKER_URL` | Redis URL from Upstash — must use `rediss://` for TLS |
| `CELERY_RESULT_BACKEND` | Same Redis URL (stores Celery task result metadata) |

---

## Deployment (Render)

Services are defined in `render.yaml`. Connect your GitHub repo to Render and it will automatically provision:
- **Web Service** — runs `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Background Worker** — runs `celery -A app.celery_app worker --loglevel=info`

Set `DATABASE_URL`, `CELERY_BROKER_URL`, and `CELERY_RESULT_BACKEND` in the Render dashboard under Environment for both services.

> **Cold starts:** Render free-tier spins down after 15 minutes of inactivity. The first request may take ~30 seconds. This is expected.
