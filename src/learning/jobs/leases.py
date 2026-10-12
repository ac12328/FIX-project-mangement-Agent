"""Lease identity helpers for durable learning workers."""
from __future__ import annotations

import uuid


def new_lease_token(worker_id: str) -> str:
    return f"{worker_id}:{uuid.uuid4().hex}"
