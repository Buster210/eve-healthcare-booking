# EVE diagnostic bookings API

A FastAPI and PostgreSQL service for diagnostic test bookings and simulated payments. Users browse centres and their tests, book an appointment, and pay through a mock provider. Signed webhooks complete pending payments without processing the same event twice.

Beyond the brief, the service also caches catalogue reads in Redis, shares auth rate limits through Redis, runs a Celery worker for stale bookings and failed webhooks, allows limited payment retries, and validates money and text inputs at the database.

## Run locally

### Docker

For the Python helper commands below, install `uv` and run `uv sync --frozen` first.

Copy the environment file and replace both signing secrets with random values of at least 32 characters:

```bash
cp .env.example .env
docker compose up --build
```

Compose starts PostgreSQL 17, Redis, the API, a Celery worker and Celery beat. The API applies migrations before serving requests. Beat expires stale bookings every five minutes and retries failed webhooks every minute. The API runs at `http://localhost:8000`; Swagger UI is at `/docs`. Both exposed ports, 8000 and 5432, bind to localhost.

Create a development admin and sample catalogue:

```bash
docker compose exec -e ADMIN_EMAIL=admin@example.com -e ADMIN_PASSWORD=change-this-password api python -m scripts.seed
```

The seed adds four centres, five tests and their prices. Running it again does not duplicate records. It creates or promotes `ADMIN_EMAIL` to admin; it does not reset an existing user's password. Keep this command restricted to trusted operators.

### Local Python

Use Python 3.12 or newer and `uv`. Start PostgreSQL separately, or use `docker compose up -d db` after configuring `.env`.

```bash
cp .env.example .env  # Skip if already configured; replace the signing secrets.
uv sync --frozen
export DATABASE_URL=postgresql+psycopg://eve:eve@localhost:5432/eve
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

`make` provides shortcuts for starting Docker, migrations, seeding, running the API, worker and beat, testing, linting and formatting. When running processes locally, set `REDIS_URL=redis://localhost:6379/0` and start Redis; `make worker` and `make beat` connect to that broker. For local seeding, set `ADMIN_EMAIL` and `ADMIN_PASSWORD` before running `make seed`.

### Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection URL using `postgresql+psycopg`; required. |
| `JWT_SECRET` | HS256 signing key, at least 32 characters; required. |
| `WEBHOOK_SECRET` | HMAC signing key, at least 32 characters; required. |
| `WEBHOOK_MAX_BODY_BYTES` | Maximum webhook request size; defaults to 65536 bytes (64 KiB). Larger bodies return 413. |
| `REDIS_URL` | Optional Redis URL for catalogue caching and shared rate limits. Compose sets this automatically; the local worker and beat need a reachable Redis broker. |
| `JWT_EXPIRE_MINUTES` | Access-token lifetime; defaults to 60 minutes. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`; defaults to `INFO`. |
| `TEST_DATABASE_URL` | Separate scratch database for tests; see below. |
| `TEST_REDIS_URL` | Optional Redis URL for cache integration tests; tests skip those checks when unset. |
| `RATE_LIMIT_ENABLED` | Auth rate limits default to enabled; `false`, `0` or `no` disables them. |

Application, access, worker and beat log records are JSON; Celery, Uvicorn and Alembic startup banners are plain text.

`ADMIN_EMAIL` and `ADMIN_PASSWORD` apply only to the seed command. Passwords must contain 8–128 characters. Do not commit `.env` or real credentials.

## Tests and CI

Tests use PostgreSQL because the implementation depends on row locks, partial indexes and `ON CONFLICT`. **Tests truncate their database**, so they require a separate database whose name starts with `eve_test`.

With the Compose database running:

```bash
docker compose exec db createdb -U eve eve_test_pay
export TEST_DATABASE_URL=postgresql+psycopg://eve:eve@localhost:5432/eve_test_pay
make test
uv run ruff check .
uv run ruff format --check .
```

Create the test database once. GitHub Actions uses `eve_test` on PostgreSQL 17 and runs dependency installation from `uv.lock`, lint, format checks and pytest on pushes and pull requests.

## API

Use `Authorization: Bearer <token>` for authenticated requests. Catalogue reads are public; catalogue writes require an admin. Users can access only their own bookings and payments; another user's booking ID returns 404.

| Method and path | Access | Request or result |
| --- | --- | --- |
| `GET /health` | Public | Liveness check: `{"status":"ok"}`. |
| `GET /health/ready` | Public | Database check; returns 503 if unavailable. |
| `POST /auth/signup` | Public | `{email, password, full_name}`; creates a user. |
| `POST /auth/login` | Public | `{email, password}`; returns `{access_token, token_type, expires_in}`. |
| `GET /auth/me` | User | Current user's profile. |
| `GET /centres` | Public | Paginated centres; optional `q` searches name/location. |
| `GET /centres/{id}` | Public | Centre details, offered tests and prices. |
| `POST /centres` | Admin | `{name, location}`. |
| `PATCH /centres/{id}` | Admin | Update `name` or `location`. |
| `DELETE /centres/{id}` | Admin | Remove an empty centre; remove offerings first. |
| `GET /tests` | Public | Paginated diagnostic tests. |
| `POST /tests` | Admin | `{name, description?}`. |
| `PATCH /tests/{id}` | Admin | Update `name` or `description`. |
| `DELETE /tests/{id}` | Admin | Remove a test that is not in use. |
| `PUT /centres/{id}/tests/{test_id}` | Admin | `{price}`; create or update the centre's test offering. |
| `DELETE /centres/{id}/tests/{test_id}` | Admin | Remove an offering with no bookings. |
| `POST /bookings` | User | `{centre_id, test_id, appointment_at}`; creates a `PENDING` booking. |
| `GET /bookings` | User | Paginated bookings; optional `status` filter. |
| `GET /bookings/{id}` | User | Booking details. |
| `POST /bookings/{id}/cancel` | User | Cancel a pending booking with no pending payment. |
| `POST /payments/` | User | `{booking_id, simulate_outcome?}`; creates a simulated payment. |
| `POST /payments/webhook/` | HMAC signature | `{event_id, provider_ref, status}`; applies a provider update. |

POST creation returns 201; `PUT` offering upserts return 200 for both creation and update. Reads and other updates return 200, and successful deletes return 204. Duplicate emails, duplicate catalogue entries, active duplicate bookings, resources in use and invalid state changes return 409. Unknown resources return 404. Emails are matched case-insensitively; unknown email and incorrect password both return 401 at login.

Appointment times must include a timezone and be in the future. Offering prices must be positive. Database checks reject non-positive and `NaN` prices and amounts for offerings, bookings and payments. The server determines booking and payment amounts, rather than accepting amounts from the caller. NUL characters in user names, catalogue text fields and the centre search query return 422.

Lists accept `limit` (1–100, default 20) and `offset` (default 0), and return `{"items": [...], "total": n, "limit": l, "offset": o}`. Errors return `{"error": {"code": "...", "message": "...", "details": ...}}`; `details` is optional. Request validation failures return 422.

Responses carry `X-Request-ID`. Signup and login allow 10 requests per minute per client IP (429 beyond that).

Centre and test reads use Redis for up to 60 seconds. Catalogue writes bump a shared cache version so older entries are no longer used. If Redis is unset or unavailable, reads go to PostgreSQL. Auth rate limits also use Redis when configured and fall back to per-process memory if Redis is unavailable.

### Example: booking and payment

Seed the catalogue first. These commands use the API on port 8000 and the signing secret from the same `.env` used by the API. They create records; change the email when repeating signup.

```bash
curl -s -X POST localhost:8000/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"patient@example.com","password":"local-demo-password","full_name":"Asha"}'

LOGIN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"patient@example.com","password":"local-demo-password"}')
TOKEN=$(printf '%s' "$LOGIN" | uv run python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

CENTRE_ID=$(curl -s 'localhost:8000/centres' | uv run python -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["id"])')
TEST_ID=$(curl -s "localhost:8000/centres/$CENTRE_ID" | uv run python -c 'import json,sys; print(json.load(sys.stdin)["tests"][0]["id"])')
APPOINTMENT_AT=$(uv run python -c 'from datetime import UTC,datetime,timedelta; print((datetime.now(UTC)+timedelta(days=1)).isoformat())')

BOOKING=$(curl -s -X POST localhost:8000/bookings \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"centre_id\":$CENTRE_ID,\"test_id\":$TEST_ID,\"appointment_at\":\"$APPOINTMENT_AT\"}")
BOOKING_ID=$(printf '%s' "$BOOKING" | uv run python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

PAYMENT=$(curl -s -X POST localhost:8000/payments/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"booking_id\":$BOOKING_ID,\"simulate_outcome\":\"PENDING\"}")
PROVIDER_REF=$(printf '%s' "$PAYMENT" | uv run python -c 'import json,sys; print(json.load(sys.stdin)["provider_ref"])')

uv run --env-file .env python -m scripts.send_webhook "$PROVIDER_REF" SUCCESS --base-url http://localhost:8000
# Expected: {"result":"processed"}

curl -s "localhost:8000/bookings/$BOOKING_ID" -H "Authorization: Bearer $TOKEN"
# The booking is now CONFIRMED.
```

Omit `simulate_outcome` for a random `SUCCESS` or `FAILED`, or supply either value to test an immediate outcome. `PENDING` leaves the payment open for a webhook.

A failed payment marks its booking `FAILED`. A new payment request for that booking moves it back to `PENDING`; at most three payment attempts are allowed per booking. Payments, including retries, return 409 once the appointment time has passed. Failed webhook processing is recorded and returns HTTP 202 with `{"result":"queued"}`. The worker retries those events in the background, recording `attempts` and `last_error`; retries stop after five total attempts. An event whose booking or payment is locked by another transaction is skipped until the next run without using an attempt. The worker and beat exit at startup if `REDIS_URL` is unset.

`requests.http` provides the same flow for REST clients. To exercise concurrent payment requests and duplicate webhook deliveries:

```bash
uv run --env-file .env python -m scripts.concurrency_demo --requests 10
```

Exported environment variables take precedence over `.env`. In particular, a helper's `WEBHOOK_SECRET` must match the running API.

## Database design

Models live in `app/models.py`; Alembic migrations create the schema.

| Table | Role and relationships |
| --- | --- |
| `users` | Email, password hash, name and admin flag. Email is stored lowercase and unique. |
| `centres`, `diagnostic_tests`, `centre_tests` | Catalogue: centres with locations, tests, and one `Numeric(10,2)` price per centre/test pair. |
| `bookings` | User, centre, test, appointment time, amount snapshot and status. A composite foreign key requires a valid offering. |
| `payments` | Booking, amount, status and unique provider reference. |
| `webhook_events` | Unique event ID, provider reference, processing status, attempts, last error, JSON payload and receipt time. |

Money uses decimal columns rather than floating-point values; seed prices use INR. Database `CHECK` constraints require positive, non-`NaN` money values. Appointment timestamps include timezone information. Status columns use database `CHECK` constraints alongside Python enums.

Partial unique indexes allow one active booking per user/centre/test/time and one pending or successful payment per booking. Failed or cancelled bookings do not prevent rebooking. Bookings keep their original price even if an offering changes later.

The centre-to-offering foreign key uses `CASCADE`, but the API rejects centre deletion while offerings exist. Test deletion uses `RESTRICT`; offerings referenced by bookings cannot be removed. Offering upserts lock both the centre and test; centre deletion locks the centre before checking its offerings.

## Payment states and webhook handling

Bookings move from `PENDING` to `CONFIRMED`, `FAILED` or `CANCELLED`. A failed booking can move back to `PENDING` for another payment attempt, up to three attempts per booking. Payments move from `PENDING` to `SUCCESS` or `FAILED`; each payment outcome is terminal. `app/state.py` validates both payment and booking transitions before applying an outcome.

The webhook requires `X-Signature` containing the hexadecimal HMAC-SHA256 of the **raw request bytes**. Signature verification uses `hmac.compare_digest` before accessing the database; missing or invalid signatures return 401.

Within one transaction, the handler records the unique `event_id`, locks the booking before its payment, and applies the outcome. A duplicate event returns `duplicate`. An unknown provider reference returns 404 and rolls back the event, so a later retry can succeed. The handler returns `no_op` for an already-applied outcome and `ignored` for a conflicting terminal outcome, both with HTTP 200. Successful updates return `processed`. Other processing errors are recorded as `FAILED`; the API returns 202 `queued`, and a scheduled task retries them every minute up to five total attempts. Each event stores its attempt count and most recent error.

Event IDs remain stored for replay detection. No retention job deletes them.

## Assumptions and trade-offs

- Catalogue reads are public; only admins manage centres, tests and prices. Each booking belongs to the authenticated user.
- Every five minutes, PENDING bookings older than 30 minutes with no PENDING or SUCCESS payment are CANCELLED in committed batches.
- Centres have no availability calendar, opening hours or capacity limits. A future appointment time does not guarantee a physical slot.
- Payments are simulated. The service does not collect card details, issue refunds or call a real payment gateway.
- Synchronous SQLAlchemy keeps request handling and transactions straightforward. PostgreSQL supplies the row locks and constraints needed for concurrency.
- Redis shares auth rate-limit counters across API processes when available; the in-memory fallback has per-process counters. Payment and webhook routes have no application rate limit. Apply deployment-level limits before exposing them publicly.
- Redis cache entries expire after 60 seconds. Catalogue mutations invalidate them by incrementing a shared version; Redis outages bypass the cache and use PostgreSQL.
- Celery beat schedules stale-booking expiration every five minutes and failed-webhook retries every minute. Webhook retries are capped at five total processing attempts.
- JWTs expire but have no revocation list. Discarding a token logs out the client; the token remains valid until expiry.

## With more time

- Model centre availability and capacity before accepting real appointments.
- Integrate a real payment provider, then define refund and reconciliation policies. Current payments are simulated and webhook retries are capped.
- Add application-level limits for payment and webhook traffic if those routes need them.
