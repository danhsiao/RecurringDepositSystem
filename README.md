# Recurring Deposit System

A proof-of-concept Fintech application demonstrating a distributed, event-driven architecture for processing recurring deposits. The core goal is to make deposits into investment accounts at some recurring frequency and to be able to see deposits for particular recurring deposit.

---

## System Architecture

### POC (Current)

```
  Manual CRON Trigger                                     ┌──────────────────────────┐
  "Simulate CRON" Button                                   │                          │
  POST /trigger-run                                        │  Database                │
         │                                                 │  PostgreSQL (Supabase)   │
         ▼                                                 │                          │
  Celery Worker ────────────── reads recurring_deposits ──►│                          │
  (Initiation)                                             │                          │
         │                                                 │                          │
         ▼                                                 │                          │
  Upstash Redis                                            │                          │
  (Message Queue)                                          │                          │
         │                                                 │                          │
         ▼                                                 │                          │
  Celery Worker ────── writes transactions/accounts/notifs►│                          │
  (Processing)                                             │                          │
                                                           │                          │
  Client → Load Balancer → FastAPI ──── reads/writes ─────►│                          │
            (Round Robin)   (Render)                       │                          │
                            (Rate Limiter)                 └──────────────────────────┘
```

### Production (AWS)

```
  EventBridge                                              ┌──────────────────────────┐
  cron(0 0 * * ? *)                                        │                          │
         │                                                 │  Database                │
         ▼                                                 │  PostgreSQL (RDS Aurora) │
  Initiation Lambda ───── reads recurring_deposits ───────►│                          │
         │                                                 │                          │
         ▼                                                 │                          │
      AWS SQS                                              │                          │
         │                                                 │                          │
         ▼                                                 │                          │
  Processing Lambda ── writes transactions/accounts/notifs►│                          │
                                                           │                          │
  Client → Load Balancer → API Gateway → FastAPI ─ reads/writes ►│                   │
            (Round Robin)   (Auth,        (Rate                   │                   │
                             Routing)      Limiter)               └───────────────────┘
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
       ├─ Check for existing schedule with same amount + frequency:
       │       ├─ active=true  → 409 "Active Duplicate" → UI shows modal (dismiss only)
       │       └─ active=false → 409 "Paused Duplicate" → UI shows modal with [Unpause] button
       │                              └─ User clicks Unpause → PATCH /deposits/{id}/activate
       │
       └─ (no duplicate) INSERT recurring_deposits (active=true, next_run_date=now)
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
                            ├─ Build idempotency_key = "{recurring_deposit_id}:{next_run_date}"
                            ├─ Check: transaction with this key already exists? → skip (safe retry)
                            ├─ Call mock DriveWealth initiate_deposit()
                            ├─ INSERT transactions (status=Pending)
                            └─ UPDATE recurring_deposits.next_run_date += frequency interval
```

**What is the idempotency key?** It is `"{recurring_deposit_id}:{next_run_date}"` — for example `"abc-123:2024-04-07T17:00:00+00:00"`. It is **not** just the deposit ID, because the same deposit ID is reused on every cycle. Including the specific `next_run_date` makes the key unique per deposit per run:
- Same cycle retried → same key → `UNIQUE` constraint fires → skipped safely
- Next cycle runs → different `next_run_date` → different key → new transaction allowed

**Why the idempotency key matters:** If the worker crashes after inserting the transaction but before acknowledging the Redis message, the broker re-delivers the task. The constraint catches the retry and skips it cleanly — no double-charging.

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
This is the manual "2-3 day" verfication that emulates the banking verification. The balance will not be added until the transaction successfully transfers. In production, there would be logic from the "given" mechanisms talked about in the interview. 
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

In production this would also fire a transactional email via **SendGrid or AWS SES** at the point of the `INSERT notifications` in the worker. The DB record is the audit trail; the email is the delivery mechanism. For now, the user is notified via the App (look at the notifications icon when a transaction fails).

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
└── idempotency_key  varchar        UNIQUE — "{recurring_deposit_id}:{next_run_date}"

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

## Scaling to Production

This POC maps directly onto a production AWS architecture. Every component has a 1:1 equivalent — the *pattern* is identical, only the infrastructure provider changes.

### Celery vs Lambda

| | Celery (this POC) | AWS Lambda (production) |
|---|---|---|
| **Execution model** | Long-running process, always on | Spins up per event, terminates after |
| **Idle cost** | Paying for compute 24/7 even when queue is empty | Pay only per invocation (first 1M/month free) |
| **Cold start** | None — always warm | ~100ms–1s on first invocation |
| **Max run time** | Unlimited | 15 minutes hard cap |
| **Scaling** | Manual — spin up more worker processes | Automatic — AWS spawns one Lambda per SQS message |
| **Ops burden** | You manage the process, restarts, crashes | Fully managed by AWS |

For a deposit that takes ~500ms to settle, Lambda is dramatically cheaper at scale. Celery is the right choice for this POC because it requires zero cloud configuration.

**Does Celery ever stop?** No. A Celery worker is an always-on process. It connects to Redis on startup and sits in a continuous listen loop — executing tasks when messages arrive, idle when the queue is empty. It only stops if the process is killed or the server goes down.

---

| | This POC | Production |
|---|---|---|
| **Trigger** | Human clicks "Simulate CRON" button | AWS EventBridge rule: `cron(0 0 * * ? *)` |
| **What it calls** | `POST /trigger-run` on FastAPI | An "Initiation Lambda" directly |
| **Infrastructure** | None — manual | Fully managed, serverless scheduler |

AWS EventBridge *is* the CRON. You define a cron expression and it fires your Lambda at midnight with no server to manage or worry about missing a run. 
---

### POC → Production Migration Map

```
POC (Render / Upstash / Supabase)        Production (AWS)
──────────────────────────────────────   ──────────────────────────────────────
"Simulate CRON" button              →    AWS EventBridge  (cron: 0 0 * * ? *)
                                                  │
FastAPI  POST /trigger-run          →    Initiation Lambda
         (queries DB, enqueues)               (queries RDS, pushes to SQS)
                                                  │
Upstash Redis  (task broker)        →    AWS SQS  (one message per deposit)
                                                  │
Celery Worker  (always-on process)  →    Processing Lambda  (auto-scales 1:1
         (polls Redis, executes)              with queue depth, serverless)
                                                  │
Supabase PostgreSQL                 →    RDS Aurora  (Multi-AZ, read replicas)

Manual "Settle" button (webhook sim)→    Real DriveWealth webhook
         POST /settle/{id}               POST /webhook/settlement
                                         → API Gateway → SQS → Settlement Lambda (Seen in the LucidChart docs talked about in interview)
```

---

---
### Additional Production Concerns

**CQRS (Read/Write Split)**
Currently one FastAPI service handles both writes (create schedule, enqueue) and reads (ledger, balance). At scale these split:
- **Write service** — accepts new schedules, enqueues tasks. Optimized for throughput.
- **Read service** — serves the ledger and balance. Backed by a read replica, optimized for low latency.
(This is what I was getting at in the interview, splitting up the recurring deposit creation and the view transaction services.) 
**Idempotency at Lambda Scale**
The idempotency key (`{recurring_deposit_id}:{next_run_date}`) becomes even more critical with Lambda because SQS has *at-least-once* delivery — a message can be delivered more than once if the Lambda times out mid-execution. The `UNIQUE` constraint on `idempotency_key` is the last line of defense against double-charging.

**Email Notifications**
The `Notification` DB record is the hook point. In production the Processing Lambda calls SendGrid or AWS SES at the same moment it writes the notification row — so the email and the audit trail are always in sync.

**Authentication**
This POC hardcodes `user_id = "demo_user"`. Production would add JWT-based auth (e.g. via AWS Cognito) at the API Gateway layer, scoping all queries to the authenticated user's accounts.

**Immediate vs Scheduled First Deposit**
Currently, creating a schedule sets `next_run_date = now()` but the first deposit only fires when the CRON runs at midnight — leaving the user with no feedback after submission. A future improvement would add a `start_immediately` flag to the `POST /deposits` request body:

- `start_immediately: true` — API enqueues `process_deposit` directly after the `INSERT`, bypassing the CRON for the first run. `next_run_date` is set to `now + 1 interval` so the CRON doesn't also pick it up.
- `start_immediately: false` (default) — `next_run_date` is set to `now + 1 interval` and the CRON handles the first run at midnight as normal.

This is worth noting architecturally: it demonstrates that the CRON and the API are both valid **producers** to the same queue — workers don't care how a task arrived, only that it's there. The UI would expose this as a toggle: *"Make first deposit now"* vs *"Start on next scheduled run."*

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

---

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (from Supabase) |
| `CELERY_BROKER_URL` | Redis URL from Upstash — must use `rediss://` for TLS |
| `CELERY_RESULT_BACKEND` | Same Redis URL (stores Celery task result metadata) |

---
https://lucid.app/lucidchart/fe6f21a9-4766-4863-9e34-ca7271aff039/edit?invitationId=inv_e96b8802-08ef-4eb8-a15a-58ed2363f833&page=0_0#

## Deployment (Render)

Services are defined in `render.yaml`. Connect your GitHub repo to Render and it will automatically provision:
- **Web Service** — runs `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Background Worker** — runs `celery -A app.celery_app worker --loglevel=info`

Set `DATABASE_URL`, `CELERY_BROKER_URL`, and `CELERY_RESULT_BACKEND` in the Render dashboard under Environment for both services.

> **Cold starts:** Render free-tier spins down after 15 minutes of inactivity. The first request may take ~30 seconds. This is expected.
