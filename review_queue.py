"""Human review queue for the PayAgent Mesh demo.

Payments the AI council can't confidently auto-pay land here for a human
to approve or reject. JSON-file persistence so the demo survives restarts.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewQueue:
    """Persistent FIFO queue of payment requests awaiting human review."""

    def __init__(self, path: str = "queue.json") -> None:
        self.path = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)

    # ---- persistence -------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self, items: list[dict]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
        os.replace(tmp, self.path)

    # ---- API ---------------------------------------------------------
    def add(self, request: dict, policy_result: dict, debate: dict) -> str:
        """Enqueue a payment for human review. Returns the item id."""
        items = self._load()
        item_id = "q-" + secrets.token_hex(3)
        item = {
            "id": item_id,
            "request": request,
            "policy": policy_result,
            "debate": {
                "verdict": debate.get("verdict"),
                "reasoning": debate.get("reasoning"),
                "risk_score": debate.get("risk_score"),
            },
            "status": "pending",
            "decided_by": None,
            "decided_ts": None,
            "created_ts": _utcnow(),
        }
        items.append(item)
        self._save(items)
        return item_id

    def pending(self) -> list[dict]:
        """Pending items, oldest first."""
        return [i for i in self._load() if i.get("status") == "pending"]

    def get(self, item_id: str) -> dict:
        for item in self._load():
            if item.get("id") == item_id:
                return item
        raise KeyError(f"queue item {item_id!r} not found")

    def decide(self, item_id: str, approved: bool, reviewer: str = "human") -> dict:
        """Approve or reject a pending item. Raises KeyError if missing/already decided."""
        items = self._load()
        for item in items:
            if item.get("id") == item_id:
                if item.get("status") != "pending":
                    raise KeyError(
                        f"queue item {item_id!r} already decided ({item.get('status')})"
                    )
                item["status"] = "approved" if approved else "rejected"
                item["decided_by"] = reviewer
                item["decided_ts"] = _utcnow()
                self._save(items)
                return item
        raise KeyError(f"queue item {item_id!r} not found")
