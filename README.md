# Eve: diagnostic test booking backend

FastAPI and PostgreSQL backend for booking diagnostic tests at centres and paying through a simulated provider. Users can browse centres and tests with per-centre prices, book an appointment, and pay. A signed webhook resolves pending payments. The app uses sync SQLAlchemy and PostgreSQL; state changes go through the state-machine helpers in `app/state.py`.

## Running

With Docker:

```bash
cp .env.example .env  # Set JWT_SECRET and WEBHOOK_SECRET in .env
docker compose up --build
```

This starts `db` (Postgres 17) and `api` (runs `alembic upgrade head`, then uvicorn). The API is available at `http://localhost:8000`; Swagger UI is at `/docs`. Compose binds API port 8000 and database port 5432 to localhost.

Seed an admin and sample data:

```bash
docker compose exec -e ADMIN_EMAIL=admin@example.com -e ADMIN_PASSWORD=change-this-password api python -m scripts.seed
```

The seed script reads `ADMIN_EMAIL` and `ADMIN_PASSWORD`, creates or promotes that user to admin (dev only), and inserts sample centres, tests, and prices. It can be run repeatedly.

Without Docker, with `uv`:

```bash
cp .env.example .env
uv sync
export DATABASE_URL=postgresql+psycopg://eve:eve@localhost:5432/eve
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Common commands are also available through `make`: `make up`, `make down`,
`make migrate`, `make seed`, `make run`, `make test`, `make lint`, and `make fmt`.
`requests.http` contains end-to-end requests for clients such as the VS Code REST
Client, with a terminal command to send the signed webhook using `WEBHOOK_SECRET`.

Tests require a separate PostgreSQL test database (they rely on `FOR UPDATE`, `ON CONFLICT`, and partial indexes; there is no SQLite fallback). Use `eve_test_pay` locally (CI uses `eve_test`). For example, use the Compose database:

```bash
docker compose exec db createdb -U eve eve_test_pay
export TEST_DATABASE_URL=postgresql+psycopg://eve:eve@localhost:5432/eve_test_pay
make test
```

## Environment

- `DATABASE_URL`: required PostgreSQL DSN using `postgresql+psycopg`.
- `JWT_SECRET`: required HS256 signing key, at least 32 characters.
- `JWT_EXPIRE_MINUTES`: access token lifetime in minutes, defaults to 60.
- `WEBHOOK_SECRET`: required HMAC key for webhook signatures.
- `LOG_LEVEL`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; defaults to `INFO`.
- `TEST_DATABASE_URL`: test database DSN; use a scratch database because test cases truncate tables.
- `RATE_LIMIT_ENABLED`: signup and login rate limits are on by default; set to `false`, `0`, or `no` to disable.

`ADMIN_EMAIL` and `ADMIN_PASSWORD` are read only by `scripts/seed.py`; password length is 8 to 128 characters.

## API overview

Errors use `{"error": {"code": "<SNAKE_CASE>", "message": "<human>", "details": ...}}`; `details` is omitted when absent. Validation errors return 422. Lists use `limit` (1–100, default 20) and `offset` (default 0), returning `{"items": [...], "total": n, "limit": l, "offset": o}`. Signup and login allow 10 requests per minute per client IP; excess requests return 429 with `details.retry_after`. Rate limiting is in-process single-worker state (`app/ratelimit.py:16`), so multi-worker deployments need a shared store such as Redis. The webhook and `POST /payments/` have no rate limit by design for this assignment (abuse note: add limits before production use). JWTs have no revocation list — logout means discarding the token client-side.

Health and auth (`Authorization: Bearer <jwt>`):

- `GET /health` (200) returns `{"status":"ok"}`.
- `GET /health/ready` (200) checks database connectivity and returns 503 when the database is unavailable.
- Every HTTP response includes `X-Request-ID`. A supplied request ID is echoed; otherwise the server generates one. The ID also appears in the request completion log.
- `POST /auth/signup` (public, 201): `{email, password, full_name}`; password must be 8–128 characters. Duplicate email, case-insensitive, returns 409.
- `POST /auth/login` (public, 200): `{email, password}`; returns `{access_token, token_type: "bearer", expires_in}`. Unknown email and wrong password return the same 401.
- `GET /auth/me` (auth, 200) returns `{id, email, full_name, is_admin, created_at}`.

Centres and tests: reads are public, writes need admin (`is_admin`, else 403):

- `GET /centres`, `GET /centres/{id}` (200): paginated list with optional `q` filter on name/location; detail includes offered tests with prices.
- `POST /centres` (201) accepts `{name, location}`; `PATCH /centres/{id}` (200) accepts either field. Duplicate centres return 409; missing centres return 404.
- `DELETE /centres/{id}` (204) returns 409 when bookings reference the centre or offerings still exist (remove offerings first).
- `GET /tests` (200), `POST /tests` (201) accepts `{name, description?}`, `PATCH /tests/{id}` (200) accepts either field, and `DELETE /tests/{id}` (204). Duplicate names return 409; missing tests return 404; delete returns 409 when in use.
- `PUT /centres/{id}/tests/{test_id}` `{price}` (200) upserts an offering; price must be positive. Missing centre or test returns 404.
- `DELETE /centres/{id}/tests/{test_id}` (204) removes an offering; missing offering is 404 and an offering with bookings returns 409.

Bookings (auth; another user's id returns 404):

- `POST /bookings` `{centre_id, test_id, appointment_at}` (201) starts with status `PENDING` and `amount` copied from the current offering price. `appointment_at` must be timezone-aware and in the future; unknown offering is 404; an active duplicate slot is 409.
- `GET /bookings` (200, optional `status` filter), `GET /bookings/{id}` (200).
- `POST /bookings/{id}/cancel` (200) works only from `PENDING` with no pending payment, else 409 `INVALID_STATE`.

Payments:

- `POST /payments/` (auth, 201): `{booking_id, simulate_outcome?: "SUCCESS" | "FAILED" | "PENDING"}`; returns `{id, booking_id, amount, status, provider_ref, booking_status}`. If omitted, outcome is randomly `SUCCESS` or `FAILED`. `PENDING` leaves payment open for a webhook. Invalid booking state or duplicate active payment returns 409.
- `POST /payments/webhook/` (no JWT, 200; `X-Signature: <hex hmac_sha256(WEBHOOK_SECRET, raw_body)>`): `{event_id, provider_ref, status}`, with status `SUCCESS` or `FAILED`. Returns `{"result": "processed" | "duplicate" | "no_op" | "ignored"}`; invalid signature is 401, unknown `provider_ref` is 404 and rolls back the event.

Curl walkthrough (seed sample data first; API on `localhost:8000`). Use the same `WEBHOOK_SECRET` in `.env` as the running API:

```bash
# signup
curl -s -X POST localhost:8000/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@example.com","password":"password123","full_name":"Asha"}'

# login
LOGIN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@example.com","password":"password123"}')
# -> {"access_token":"...","token_type":"bearer","expires_in":3600}
TOKEN=$(printf '%s' "$LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# choose a centre and one of its offered tests
CENTRE_ID=$(curl -s 'localhost:8000/centres?limit=20&offset=0' | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["id"])')
TEST_ID=$(curl -s "localhost:8000/centres/$CENTRE_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tests"][0]["id"])')

# create booking (timezone-aware future datetime)
BOOKING=$(curl -s -X POST localhost:8000/bookings \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"centre_id\":$CENTRE_ID,\"test_id\":$TEST_ID,\"appointment_at\":\"2030-01-01T10:00:00+05:30\"}")
BOOKING_ID=$(printf '%s' "$BOOKING" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# create a pending payment for the webhook to resolve
PAYMENT=$(curl -s -X POST localhost:8000/payments/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"booking_id\":$BOOKING_ID,\"simulate_outcome\":\"PENDING\"}")
printf '%s\n' "$PAYMENT"
# -> status: PENDING, booking_status: PENDING, provider_ref: sim_...
PROVIDER_REF=$(printf '%s' "$PAYMENT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["provider_ref"])')

# sign and send using WEBHOOK_SECRET from .env
uv run --env-file .env python -m scripts.send_webhook "$PROVIDER_REF" SUCCESS --base-url http://localhost:8000
# -> {"result":"processed"}

# fetch the same booking: status is now CONFIRMED
curl -s "localhost:8000/bookings/$BOOKING_ID" -H "Authorization: Bearer $TOKEN"
```

The helper signs the exact bytes it sends and generates a fresh event ID. An exported `WEBHOOK_SECRET` takes precedence over `.env`; it must match the API's configured value.

`scripts/concurrency_demo.py` creates a user and booking, races concurrent
payment requests, then sends duplicate webhook deliveries:

```bash
WEBHOOK_SECRET=supersecret uv run python -m scripts.concurrency_demo --requests 10
```

## Schema

- `users` (`id`, `email`, `password_hash`, `full_name`, `is_admin`, `created_at`); email is lowercased before insert.
- `centres` (`id`, `name`, `location`, `created_at`, `updated_at`); unique `(name, location)`.
- `diagnostic_tests` (`id`, `name`, `description?`, `created_at`); unique `name`, with prices stored on offerings.
- `centre_tests` (`id`, `centre_id -> centres CASCADE`, `test_id -> diagnostic_tests RESTRICT`, `price > 0`, `UNIQUE(centre_id, test_id)`): one price per centre/test pair. Although the FK has `CASCADE`, the API returns 409 while a centre still has offerings; remove them before deleting the centre. Tests and offerings referenced by bookings cannot be deleted (409).
- `bookings` (`id`, `user_id`, `centre_id`, `test_id`, `appointment_at`, `amount`, `status`, `created_at`, `updated_at`); amount snapshots the offering price. A composite FK requires the test be offered at the centre; an active-slot partial unique index prevents duplicates while allowing rebooking after cancel or failure.
- `payments` (`id`, `booking_id`, `amount`, `status`, `provider_ref`, `created_at`, `updated_at`); amount copies the booking amount. A partial unique index permits one pending or successful payment per booking.
- `webhook_events` (`id`, `event_id`, `provider_ref`, `status`, `payload JSONB`, `received_at`); unique `event_id` deduplicates deliveries.

Statuses are `varchar` plus `CHECK`, mirrored as Python `StrEnum` values.

## Assumptions

- Appointment times are supplied with a timezone and must be in the future; centres do not expose availability or capacity slots.
- A booking uses the offering price at creation, even if that price changes later.
- The payment provider is simulated. A pending payment is completed by a signed webhook; refunds and payment retries are outside this flow.
- Webhook event IDs are unique per provider event, and delivery may be repeated.

## Design decisions

- Use synchronous FastAPI and SQLAlchemy with PostgreSQL, whose row locks and partial indexes enforce booking/payment concurrency rules.
- Keep booking and payment transitions centralized in `app/state.py`; lock the booking before its payment when processing outcomes.
- Store money as `Numeric(10, 2)`, sign webhooks with HMAC-SHA256 over the raw request body, and deduplicate events by `event_id`.

## State transitions

Bookings move from `PENDING` to `CONFIRMED`, `FAILED`, or `CANCELLED`; payments move from `PENDING` to `SUCCESS` or `FAILED`. State changes go through the state-machine helpers in `app/state.py`: `transition()` validates a single transition, and `apply_payment_outcome()` validates both booking and payment transitions before mutating either status.

## Webhook idempotency and concurrency

The HMAC is checked over raw request bytes with `hmac.compare_digest` before database access, so a bad signature does not consume an `event_id`. Webhook events are deduplicated by `event_id` in a transaction. Unknown `provider_ref` values roll back the event row, allowing a later retry. The booking row is locked before the payment row to serialize concurrent payment flows. An event matching the current payment status returns `no_op`; an illegal transition such as `FAILED` after `SUCCESS` returns `ignored` with HTTP 200.

```text
Client                 API / database                 Payment provider
  | POST /bookings             |                               |
  |--------------------------->| create PENDING booking       |
  |                            |                               |
  | POST /payments (PENDING)   |                               |
  |--------------------------->| create PENDING payment       |
  |<---------------------------| provider_ref                  |
  |                            |<------------------------------| signed webhook
  |                            | dedupe event; lock booking then payment
  |                            | transition payment + booking |
```

## Improvements

- Redis cache for the centre/test catalogue.
- Celery and an outbox for webhook processing.
- Refunds and a `REFUNDED` state path.
- A shared rate-limit store for multiple workers.
- Retry payments on a failed booking.
- Centre availability slots with capacity and opening hours.

## CI

GitHub Actions runs on pushes and pull requests: `uv sync --frozen`, `ruff check .`, `ruff format --check .`, and `pytest -q` against PostgreSQL 17.
