import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider_ref")
    parser.add_argument("status", choices=("SUCCESS", "FAILED"))
    parser.add_argument("--event-id", default=str(uuid.uuid4()))
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        parser.error("WEBHOOK_SECRET must be set")

    body = json.dumps(
        {
            "event_id": args.event_id,
            "provider_ref": args.provider_ref,
            "status": args.status,
        },
        separators=(",", ":"),
    ).encode()
    request = Request(
        f"{args.base_url.rstrip('/')}/payments/webhook/",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": sign_payload(secret, body),
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            print(response.read().decode())
            return 0
    except HTTPError as error:
        print(error.read().decode(), file=sys.stderr)
        return 1
    except URLError as error:
        print(error.reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
