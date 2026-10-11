"""PayAgent Mesh append-only audit log (JSONL).

Free-only, no network: events are appended as one JSON object per line to a
local file. Stdlib only. Write failures never raise — they print a warning
to stderr so a logging fault can't break the payment path.
"""

import json
import os
import sys
from datetime import datetime, timezone

_BUILD_DIR = os.path.dirname(os.path.abspath(__file__))


class AuditLog:
    def __init__(self, path: str = "audit.jsonl"):
        """Resolve relative paths against the build dir."""
        if not os.path.isabs(path):
            path = os.path.join(_BUILD_DIR, path)
        self.path = path

    def record(self, event: str, payload: dict) -> None:
        """Append one JSON line: {"ts": <UTC ISO>, "event": event, **payload}."""
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
        entry.update(payload or {})
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:  # never raise on write failure
            print(f"audit: failed to write event '{event}': {exc}", file=sys.stderr)

    def tail(self, n: int = 50) -> list[dict]:
        """Last n entries, oldest-first."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        entries = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries


if __name__ == "__main__":
    log = AuditLog()
    log.record("payment_checked", {"request_id": "req-001", "approved": True})
    log.record("payment_checked", {"request_id": "req-002", "approved": False, "risk_score": 0.9})
    for e in log.tail(2):
        print(e)
