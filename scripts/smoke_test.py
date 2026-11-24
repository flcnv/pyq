import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "invoice_with_qr.pdf"
TIMEOUT_SECONDS = 60


def main(base_url: str) -> None:
    """Upload a QR-bill to a running stack (API + worker) and wait until it is an invoice.

    `python scripts/smoke_test.py [base URL]`, default http://localhost:8000. Standard library
    only, so it runs anywhere Python does, CI included.
    """
    if not base_url.startswith(("http://", "https://")):
        fail(f"not an HTTP URL: {base_url}")
    ingestion = upload(base_url, FIXTURE)
    print(f"uploaded: {ingestion['id']} ({ingestion['status']})")
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while ingestion["status"] in ("pending", "processing"):
        if time.monotonic() > deadline:
            fail(f"not processed within {TIMEOUT_SECONDS} s: {ingestion}")
        time.sleep(1)
        ingestion = get_json(f"{base_url}/ingestions/{ingestion['id']}")
    if ingestion["status"] != "done":
        fail(f"ingestion {ingestion['status']}: {ingestion['error']}")

    invoice = get_json(f"{base_url}/invoices/{ingestion['invoice_id']}")
    if (invoice["source"], invoice["amount"]) != ("qr", {"value": "50.00", "currency": "CHF"}):
        fail(f"unexpected invoice: {invoice}")
    print(f"done: invoice {invoice['id']}, {invoice['creditor']['name']}, 50.00 CHF")


def upload(base_url: str, path: Path) -> dict[str, object]:
    boundary = uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    request = urllib.request.Request(
        f"{base_url}/invoices",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status not in (200, 202):  # 200: this file was processed before
            fail(f"upload answered {response.status}")
        result: dict[str, object] = json.load(response)
        return result


def get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=10) as response:
        result: dict[str, object] = json.load(response)
        return result


def fail(message: str) -> None:
    print(f"smoke test failed: {message}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000")
