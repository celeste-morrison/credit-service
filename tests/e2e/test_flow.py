"""End-to-end: API, Redis, Celery worker and MongoDB in docker compose."""

import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx

from app.worker.celery_app import celery


def wait_until_processed(api: httpx.Client, user_id: str, timeout_s: float = 15) -> dict[str, Any]:
    """Poll the user until its status is ``processed``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        user: dict[str, Any] = api.get(f"/users/{user_id}").json()
        if user.get("status") == "processed":
            return user
        time.sleep(0.2)
    raise AssertionError(f"user {user_id} not processed within {timeout_s}s: {user}")


def test_credits_flow_through_queue_and_worker(api: httpx.Client) -> None:
    user_id = f"u_e2e_{uuid4().hex[:8]}"
    amounts = ["100.00", "50.25", "0.10"]

    task_ids = []
    for amount in amounts:
        resp = api.post("/credits", json={"user_id": user_id, "amount": amount})
        assert resp.status_code == 202
        task_ids.append(resp.json()["task_id"])

    user = wait_until_processed(api, user_id)
    assert Decimal(user["balance"]) == sum(Decimal(a) for a in amounts)
    assert user["pending_credits"] == 0
    for task_id in task_ids:
        assert api.get(f"/credits/{task_id}").json()["status"] == "applied"


def test_redelivered_task_does_not_double_credit(api: httpx.Client) -> None:
    """Deliver the same task again after it was applied.

    The test waits until the worker reports it handled the duplicate (the
    credit's ``redeliveries`` count rises), so it cannot pass just because the
    duplicate was never consumed.
    """
    user_id = f"u_e2e_{uuid4().hex[:8]}"
    task_id = api.post("/credits", json={"user_id": user_id, "amount": "25.00"}).json()["task_id"]
    wait_until_processed(api, user_id)

    celery.send_task("credits.apply", args=(task_id,), task_id=task_id)
    deadline = time.monotonic() + 15
    while api.get(f"/credits/{task_id}").json()["redeliveries"] < 1:
        assert time.monotonic() < deadline, "worker never consumed the duplicate"
        time.sleep(0.2)

    user = api.get(f"/users/{user_id}").json()
    assert user["balance"] == "25.00"
