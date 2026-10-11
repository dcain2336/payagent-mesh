"""PayAgent Mesh policy engine.

Every agent payment request passes these policy checks before it can reach
the AI council. Free-only, no network: state is kept in a local JSON file,
stdlib + PyYAML only.

PaymentRequest dict shape:
    {"id": str, "agent_id": str, "amount": float, "currency": "USD",
     "description": str, "merchant": str, "category": str, "timestamp": ISO str}
"""

import json
import os
from datetime import datetime, timezone, timedelta

import yaml

_BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE = os.path.join(_BUILD_DIR, "spend_state.json")

# risk weights: hard checks (cap / denylist / budget) vs soft (allowlist / velocity / anomaly)
_HARD_WEIGHT = 0.35
_SOFT_WEIGHT = 0.20


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp; naive timestamps are assumed UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class PolicyEngine:
    def __init__(self, policy_path: str = "policy.yaml"):
        """Load YAML policy. Relative paths resolve against the build dir."""
        if not os.path.isabs(policy_path):
            policy_path = os.path.join(_BUILD_DIR, policy_path)
        with open(policy_path, "r", encoding="utf-8") as f:
            self.policy = yaml.safe_load(f) or {}
        self.state_path = _STATE_FILE

    # ----- state ---------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"agents": {}}

    def _save_state(self, state: dict) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, self.state_path)

    def record_spend(self, agent_id: str, amount: float) -> None:
        """Record an approved spend + request timestamp for an agent."""
        state = self._load_state()
        agent = state["agents"].setdefault(agent_id, {"approved_today": [], "history": [], "requests": []})
        now = datetime.now(timezone.utc).isoformat()
        agent["requests"].append(now)
        agent["history"].append(float(amount))
        # keep only the last 90 days of approved amounts for the daily budget
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        agent["approved_today"] = [e for e in agent["approved_today"] if e["ts"] >= cutoff]
        agent["approved_today"].append({"ts": now, "amount": float(amount)})
        self._save_state(state)

    # ----- checks --------------------------------------------------------
    def check(self, request: dict) -> dict:
        """Run all policy checks; returns approved, risk_score, checks, reasons."""
        agent_id = request.get("agent_id", "")
        amount = float(request.get("amount", 0.0))
        merchant = request.get("merchant", "")
        ts = _parse_ts(request.get("timestamp", datetime.now(timezone.utc).isoformat()))

        agent_cfg = (self.policy.get("agents") or {}).get(agent_id, {})
        denylist = self.policy.get("denylist") or []
        vel_cfg = self.policy.get("velocity") or {}
        anom_cfg = self.policy.get("anomaly") or {}
        state = self._load_state()
        astate = state["agents"].get(agent_id, {"approved_today": [], "history": [], "requests": []})

        checks = []
        reasons = []
        risk_score = 0.0

        def add(name, passed, detail, hard):
            nonlocal risk_score
            checks.append({"name": name, "passed": passed, "detail": detail})
            if not passed:
                reasons.append(f"{name}: {detail}")
                risk_score += _HARD_WEIGHT if hard else _SOFT_WEIGHT

        # 1. per-transaction cap (hard)
        cap = agent_cfg.get("per_transaction_cap")
        if cap is None:
            add("per_transaction_cap", True, "no cap configured for agent", hard=True)
        else:
            add("per_transaction_cap", amount <= cap,
                f"amount {amount} vs cap {cap}", hard=True)

        # 2. daily rolling budget (hard)
        daily = agent_cfg.get("daily_budget")
        if daily is None:
            add("daily_budget", True, "no daily budget configured for agent", hard=True)
        else:
            today_total = sum(e["amount"] for e in astate["approved_today"])
            add("daily_budget", today_total + amount <= daily,
                f"today {today_total} + {amount} vs budget {daily}", hard=True)

        # 3. merchant allowlist (soft) + global denylist (hard)
        if merchant in denylist:
            add("merchant_denylist", False, f"merchant '{merchant}' is on the global denylist", hard=True)
        else:
            add("merchant_denylist", True, f"merchant '{merchant}' not on denylist", hard=True)
        allowlist = agent_cfg.get("allowlist")
        if allowlist:
            add("merchant_allowlist", merchant in allowlist,
                f"merchant '{merchant}' {'on' if merchant in allowlist else 'NOT on'} agent allowlist", hard=False)
        else:
            add("merchant_allowlist", True, "no allowlist configured for agent", hard=False)

        # 4. velocity (soft): >N requests in last M minutes
        max_req = vel_cfg.get("max_requests", 5)
        window = vel_cfg.get("window_minutes", 10)
        window_start = ts - timedelta(minutes=window)
        recent = 0
        for r in astate["requests"]:
            try:
                if _parse_ts(r) >= window_start:
                    recent += 1
            except ValueError:
                continue
        add("velocity", recent < max_req,
            f"{recent} requests in last {window} min (max {max_req})", hard=False)

        # 5. anomaly (soft): amount > K x historical mean
        history = astate["history"]
        if not history:
            add("anomaly", True, "no history", hard=False)
        else:
            k = anom_cfg.get("std_multiplier", 3.0)
            mean = sum(history) / len(history)
            add("anomaly", amount <= k * mean,
                f"amount {amount} vs {k}x mean {mean:.2f} (n={len(history)})", hard=False)

        risk_score = max(0.0, min(1.0, risk_score))
        return {
            "approved": all(c["passed"] for c in checks),
            "risk_score": round(risk_score, 4),
            "checks": checks,
            "reasons": reasons,
        }


if __name__ == "__main__":
    eng = PolicyEngine()
    now = datetime.now(timezone.utc).isoformat()

    passing = {
        "id": "req-001", "agent_id": "agent-office-01", "amount": 49.99,
        "currency": "USD", "description": "printer paper", "merchant": "Staples",
        "category": "office", "timestamp": now,
    }
    print("PASSING:", json.dumps(eng.check(passing), indent=2))

    failing = {
        "id": "req-002", "agent_id": "agent-office-01", "amount": 9999.0,
        "currency": "USD", "description": "shady deal", "merchant": "DarkWeb Market",
        "category": "misc", "timestamp": now,
    }
    print("FAILING:", json.dumps(eng.check(failing), indent=2))
