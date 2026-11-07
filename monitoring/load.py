"""Steady background traffic for the demo shop, with realistic fake customers.

Requests per second come from run/load_control.json (re-read every second), so the lab can
create a traffic spike. Requests are sent in 100 ms batches, because Windows timers are too
coarse for evenly spaced sleeps.
"""

import asyncio
import json
import os
import random
import time
import uuid
from pathlib import Path

import httpx

URL = "http://127.0.0.1:8100"
ROOT = Path(__file__).resolve().parent
CONTROL = ROOT / "run" / "load_control.json"
PEOPLE_FILE = ROOT / "demo_app_template" / "src" / "main" / "resources" / "fake-people.properties"
MIX = {"list_products": 25, "get_product": 15, "signup": 10, "update_address": 8, "get_customer": 5,
       "place_order": 22, "get_order": 15}
TICK_S = 0.1
CARDS = ["4111111111111111", "5500005555555559", "4012888888881881"]  # standard test card numbers


def load_people() -> dict[str, list[str]]:
    people = {}
    for line in PEOPLE_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            people[key.strip()] = value.split(",")
    return people


PEOPLE = load_people()


def read_rps(default: float = 10) -> float:
    try:
        return float(json.loads(CONTROL.read_text()).get("rps", default))
    except (OSError, ValueError):
        return default


def fake_phone() -> str:
    number = f"98{random.randint(0, 999):03d} {random.randint(0, 99999):05d}"
    return number if random.random() < 0.5 else "+91 " + number


async def one_request(client: httpx.AsyncClient, kind: str, record):
    customer_id = random.randint(1, 5000)
    body = None
    try:
        if kind == "list_products":
            resp = await client.get("/api/products")
        elif kind == "get_product":
            resp = await client.get(f"/api/products/{random.randint(1, 50)}")
        elif kind == "signup":
            first, last = random.choice(PEOPLE["firstNames"]), random.choice(PEOPLE["lastNames"])
            email = f"{first}.{last}.{uuid.uuid4().hex[:6]}@example.com".lower()
            body = {"name": f"{first} {last}", "email": email, "phone": fake_phone() if random.random() < 0.95 else None}
            resp = await client.post("/api/customers", json=body)
        elif kind == "update_address":
            street = f"{random.randint(1, 200)} {random.choice(PEOPLE['streets'])}"
            body = {"street": street, "city": random.choice(PEOPLE["cities"])}
            resp = await client.put(f"/api/customers/{customer_id}/address", json=body)
        elif kind == "get_customer":
            resp = await client.get(f"/api/customers/{customer_id}")
        elif kind == "place_order":
            gift = "Happy birthday!" if random.random() < 0.1 else None
            body = {"customerId": customer_id, "productId": random.randint(1, 50), "quantity": random.randint(1, 3),
                    "cardNumber": random.choice(CARDS), "giftMessage": gift}
            resp = await client.post("/api/orders", json=body)
        else:
            resp = await client.get(f"/api/orders/{random.randint(1, 300_000)}")
    except httpx.HTTPError:
        return  # app down or too slow; the app's own logs and metrics record what happened
    if record:
        record({"request_id": resp.headers.get("x-request-id"), "kind": kind, "status": resp.status_code,
                "path": str(resp.request.url.path), "body": body, "response": resp.text[:300], "ts": time.time()})


async def main():
    kinds, weights = list(MIX), list(MIX.values())
    tasks: set[asyncio.Task] = set()
    # LOAD_RECORD=<file>: write one JSON line per request (request id, status, what was sent), used to
    # check later whether each write really reached the database.
    record_path = os.environ.get("LOAD_RECORD")
    record_file = open(record_path, "a", encoding="utf-8") if record_path else None
    record = (lambda row: (record_file.write(json.dumps(row) + "\n"), record_file.flush())) if record_file else None
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=URL, timeout=10, limits=limits) as client:
        rps, next_read, next_tick = read_rps(), time.monotonic() + 1, time.monotonic()
        while True:
            if time.monotonic() >= next_read:
                rps, next_read = read_rps(), next_read + 1
            expected = rps * TICK_S  # average requests per tick
            count = int(expected) + (random.random() < expected - int(expected))
            for kind in random.choices(kinds, weights, k=count):
                task = asyncio.create_task(one_request(client, kind, record))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            next_tick += TICK_S
            await asyncio.sleep(max(0, next_tick - time.monotonic()))


if __name__ == "__main__":
    asyncio.run(main())
