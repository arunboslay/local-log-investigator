"""Stand-in for an outside payment service on port 8101.

Extra delay is read from run/payment_control.json on every call, so the lab can make it slow.
"""

import asyncio
import json
import random
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI

CONTROL = Path(__file__).resolve().parent / "run" / "payment_control.json"
app = FastAPI()


def extra_delay_s() -> float:
    try:
        return json.loads(CONTROL.read_text()).get("delay_ms", 0) / 1000
    except (OSError, ValueError):
        return 0


@app.post("/charge")
async def charge(body: dict):
    await asyncio.sleep(random.uniform(0.02, 0.06) + extra_delay_s())
    return {"status": "APPROVED", "authId": uuid.uuid4().hex[:10], "amount": body.get("amount")}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8101, log_level="warning")
