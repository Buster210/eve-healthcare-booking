"""Exercise concurrent payment creation and duplicate webhook delivery."""

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> tuple[int, dict]:
    body = (
        None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    )
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def require_success(response: tuple[int, dict], expected: int, action: str) -> dict:
    status, body = response
    if status != expected:
        raise RuntimeError(f"{action}: expected HTTP {expected}, got {status}: {body}")
    return body


def concurrent_posts(
    base_url: str,
    path: str,
    payload: dict,
    headers: dict[str, str],
    count: int,
    timeout: float,
) -> list[tuple[int, dict]]:
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [
            pool.submit(
                request_json,
                base_url,
                path,
                method="POST",
                payload=payload,
                headers=headers,
                timeout=timeout,
            )
            for _ in range(count)
        ]
        return [future.result() for future in futures]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("-n", "--requests", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.requests < 2:
        parser.error("--requests must be at least 2")
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        parser.error("WEBHOOK_SECRET must be set")

    email = f"concurrency-{uuid.uuid4().hex}@example.com"
    password = f"Demo-{uuid.uuid4().hex}"
    require_success(
        request_json(
            args.base_url,
            "/auth/signup",
            method="POST",
            payload={
                "email": email,
                "password": password,
                "full_name": "Concurrency Demo",
            },
            timeout=args.timeout,
        ),
        201,
        "sign up",
    )
    login = require_success(
        request_json(
            args.base_url,
            "/auth/login",
            method="POST",
            payload={"email": email, "password": password},
            timeout=args.timeout,
        ),
        200,
        "log in",
    )
    auth = {"Authorization": f"Bearer {login['access_token']}"}

    query = urlencode({"limit": 100, "offset": 0})
    centres = require_success(
        request_json(args.base_url, f"/centres?{query}", timeout=args.timeout),
        200,
        "list centres",
    )
    offering = None
    for centre in centres["items"]:
        detail = require_success(
            request_json(
                args.base_url,
                f"/centres/{centre['id']}",
                timeout=args.timeout,
            ),
            200,
            "read centre",
        )
        if detail["tests"]:
            offering = (centre, detail["tests"][0])
            break
    if offering is None:
        raise RuntimeError("No centre with an offered test was found")
    centre, test = offering

    booking = require_success(
        request_json(
            args.base_url,
            "/bookings",
            method="POST",
            payload={
                "centre_id": centre["id"],
                "test_id": test["id"],
                "appointment_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            },
            headers=auth,
            timeout=args.timeout,
        ),
        201,
        "create booking",
    )

    payment_results = concurrent_posts(
        args.base_url,
        "/payments/",
        {"booking_id": booking["id"], "simulate_outcome": "PENDING"},
        auth,
        args.requests,
        args.timeout,
    )
    created = [body for status, body in payment_results if status == 201]
    conflicts = [body for status, body in payment_results if status == 409]
    other_payments = [
        (status, body) for status, body in payment_results if status not in (201, 409)
    ]
    if len(created) != 1 or len(conflicts) != args.requests - 1 or other_payments:
        raise RuntimeError(
            f"Payment race unexpected: created={len(created)}, "
            f"conflicts={len(conflicts)}, "
            f"other={other_payments}"
        )

    event_id = str(uuid.uuid4())
    webhook_payload = {
        "event_id": event_id,
        "provider_ref": created[0]["provider_ref"],
        "status": "SUCCESS",
    }
    webhook_body = json.dumps(webhook_payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), webhook_body, hashlib.sha256).hexdigest()
    webhook_headers = {"X-Signature": signature}
    webhook_results = concurrent_posts(
        args.base_url,
        "/payments/webhook/",
        webhook_payload,
        webhook_headers,
        args.requests,
        args.timeout,
    )
    webhook_statuses = [status for status, _ in webhook_results]
    webhook_results_json = [body.get("result") for _, body in webhook_results]
    if webhook_statuses != [200] * args.requests:
        raise RuntimeError(f"Webhook HTTP failures: {webhook_results}")
    if (
        webhook_results_json.count("processed") != 1
        or webhook_results_json.count("duplicate") != args.requests - 1
    ):
        raise RuntimeError(f"Webhook race unexpected: {webhook_results_json}")

    final_booking = require_success(
        request_json(
            args.base_url,
            f"/bookings/{booking['id']}",
            headers=auth,
            timeout=args.timeout,
        ),
        200,
        "read final booking",
    )
    if final_booking["status"] not in ("CONFIRMED", "FAILED"):
        raise RuntimeError(
            f"Booking did not reach a terminal state: {final_booking['status']}"
        )

    print(
        f"OK: booking={booking['id']} {final_booking['status']}; "
        f"payments={len(created)}/{args.requests} created, {len(conflicts)} conflicts; "
        f"webhooks={webhook_results_json.count('processed')} processed, "
        f"{webhook_results_json.count('duplicate')} duplicate no-ops"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, URLError, ValueError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
