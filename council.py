"""Council debate module for PayAgent Mesh (PayPal AI Hackathon 2026).

After policy checks pass, a 3-role AI council debates each payment:

- planner:     argues FOR the payment (business value, legitimacy)
- risk-scorer: argues AGAINST (or scores risk) — finds the danger
- auditor:     issues the final verdict with reasoning

Modes:
    DEMO  (llm=None or demo=True): deterministic scripted debate.
          No network, no API keys, no randomness. Same input -> same output.
    LIVE  (llm=<callable>, demo=False): the provided callable is invoked
          three times with role-specific framings, and the auditor reply is
          parsed for the verdict. Any LLM error falls back to QUEUE (never
          crashes).

The LLM interface accepts ANY callable ``prompt -> str`` so real free-tier
provider seats can be plugged in later without changing this module.
"""

_HIGH_RISK_CATEGORIES = {"wire", "crypto", "gift-cards"}

_VALID_VERDICTS = ("AUTO_PAY", "QUEUE", "REJECT")


class Council:
    """Three-role payment debate council.

    Args:
        llm: Optional callable ``prompt: str -> str``. In LIVE mode it is
            called once per role (planner, risk, auditor). Ignored in DEMO.
        demo: If True (or llm is None), use the deterministic scripted
            debate instead of the LLM.
    """

    def __init__(self, llm=None, demo=True):
        self.llm = llm
        self.demo = demo or (llm is None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def debate(self, request: dict, policy_result: dict) -> dict:
        """Debate a payment request and return a verdict.

        Args:
            request: {"id", "agent_id", "amount", "currency", "description",
                      "merchant", "category", "timestamp"}
            policy_result: {"approved": bool, "risk_score": float,
                            "checks": [...], "reasons": [...]}

        Returns:
            {"verdict": "AUTO_PAY"|"QUEUE"|"REJECT", "reasoning": str,
             "transcript": [{"role": "planner"|"risk"|"auditor",
                             "text": str}, ...],
             "risk_score": float}
        """
        if self.demo:
            return self._debate_demo(request, policy_result)
        return self._debate_live(request, policy_result)

    # ------------------------------------------------------------------
    # DEMO mode: deterministic scripted debate
    # ------------------------------------------------------------------
    def _combined_risk(self, request: dict, policy_result: dict) -> float:
        """Combine the policy risk score with simple deterministic heuristics."""
        risk = float(policy_result.get("risk_score", 0.0))
        amount = float(request.get("amount", 0.0))
        merchant = str(request.get("merchant", "")).strip()
        category = str(request.get("category", "")).strip().lower()

        if amount > 1000:
            risk += 0.2
        if not merchant:
            risk += 0.1
        if category in _HIGH_RISK_CATEGORIES:
            risk += 0.25

        return max(0.0, min(1.0, risk))

    def _debate_demo(self, request: dict, policy_result: dict) -> dict:
        agent_id = request.get("agent_id", "unknown")
        amount = float(request.get("amount", 0.0))
        currency = request.get("currency", "USD")
        description = request.get("description", "")
        merchant = str(request.get("merchant", "")).strip()
        category = request.get("category", "general")

        policy_ok = bool(policy_result.get("approved", False))
        risk = self._combined_risk(request, policy_result)
        risk_pct = risk * 100

        merchant_name = merchant if merchant else "an unlisted merchant"
        merchant_known = "known" if merchant else "unlisted"

        # Planner: argues FOR the payment.
        planner_text = (
            f"Agent {agent_id} is requesting {amount:.2f} {currency} for "
            f'"{description}" at {merchant_name}. The spend maps to category '
            f"'{category}', which is routine operational cost. Paying it now "
            f"keeps the workflow moving with no agent idle time."
        )

        # Risk-scorer: argues AGAINST / scores the danger.
        if risk >= 0.7:
            risk_tone = "This payment carries serious risk flags"
        elif risk >= 0.35:
            risk_tone = "This payment warrants a second look"
        else:
            risk_tone = "The exposure here looks contained"

        risk_text = (
            f"{risk_tone}: combined risk score {risk_pct:.0f}% after heuristics "
            f"(amount {amount:.2f} {currency}, merchant {merchant_known}, "
            f"category '{category}'). The policy engine itself reported "
            f"{'approval' if policy_ok else 'REJECTION'}."
        )

        # Auditor: final verdict with reasoning.
        if not policy_ok:
            failed = policy_result.get("reasons") or policy_result.get("checks") or []
            failed_str = ", ".join(str(f) for f in failed) if failed else "policy checks"
            verdict = "REJECT"
            auditor_text = (
                f"Verdict: REJECT. The policy layer already refused this payment "
                f"({failed_str}), and the council does not overrule a policy "
                f"refusal. No further debate on approval is needed; the request "
                f"should be returned to the agent for correction or review."
            )
        elif risk < 0.35:
            verdict = "AUTO_PAY"
            auditor_text = (
                f"Verdict: AUTO_PAY. Policy approved the request, combined risk "
                f"is low at {risk_pct:.0f}%, and the planner's business case "
                f"(legitimate '{category}' spend for {agent_id}) is sound. "
                f"Clearing it for immediate execution."
            )
        elif risk <= 0.7:
            verdict = "QUEUE"
            auditor_text = (
                f"Verdict: QUEUE. Policy approved, but combined risk sits at "
                f"{risk_pct:.0f}% — elevated enough that a human should glance at "
                f"the {amount:.2f} {currency} charge to {merchant_name} before it "
                f"executes. Neither clearly safe nor clearly fraudulent."
            )
        else:
            verdict = "REJECT"
            auditor_text = (
                f"Verdict: REJECT. Combined risk reached {risk_pct:.0f}%, well "
                f"past the acceptable threshold. The risk-scorer's flags on this "
                f"{amount:.2f} {currency} payment to {merchant_name} outweigh the "
                f"planner's convenience argument. Block it and surface for "
                f"human investigation."
            )

        transcript = [
            {"role": "planner", "text": planner_text},
            {"role": "risk", "text": risk_text},
            {"role": "auditor", "text": auditor_text},
        ]

        decisive = (
            f"policy {'approved' if policy_ok else 'rejected'}; combined risk "
            f"{risk_pct:.0f}% (policy {float(policy_result.get('risk_score', 0.0)) * 100:.0f}% "
            f"plus heuristics on amount, merchant, category)"
        )
        if verdict == "REJECT":
            if not policy_ok:
                reasoning = (
                    f"The policy layer rejected the payment, so the council "
                    f"rejects by rule — {decisive}. The auditor does not "
                    f"overrule policy refusals; the agent must fix the flagged "
                    f"checks and resubmit."
                )
            else:
                reasoning = (
                    f"The council rejects despite policy approval — {decisive}. "
                    f"High combined risk on a {amount:.2f} {currency} payment to "
                    f"{merchant_name} (category '{category}') outweighs the "
                    f"planner's convenience case."
                )
        elif verdict == "AUTO_PAY":
            reasoning = (
                f"The council auto-pays — {decisive}. Low risk, legitimate "
                f"'{category}' spend for {agent_id} at {merchant_name}, and no "
                f"policy flags, so execution proceeds without human review."
            )
        else:
            reasoning = (
                f"The council queues for human review — {decisive}. Elevated "
                f"risk on a {amount:.2f} {currency} payment to {merchant_name} "
                f"merits a quick human glance before release."
            )

        return {
            "verdict": verdict,
            "reasoning": reasoning,
            "transcript": transcript,
            "risk_score": round(risk, 4),
        }

    # ------------------------------------------------------------------
    # LIVE mode: delegate each role to the provided LLM callable
    # ------------------------------------------------------------------
    def _build_prompt(self, request: dict, policy_result: dict) -> str:
        """Build the shared context prompt containing the payment + policy."""
        lines = ["PAYMENT REQUEST:"]
        for key in ("id", "agent_id", "amount", "currency", "description",
                    "merchant", "category", "timestamp"):
            lines.append(f"- {key}: {request.get(key, 'n/a')}")
        lines.append("POLICY RESULT:")
        lines.append(f"- approved: {policy_result.get('approved', False)}")
        lines.append(f"- risk_score: {policy_result.get('risk_score', 0.0)}")
        lines.append(f"- checks: {policy_result.get('checks', [])}")
        lines.append(f"- reasons: {policy_result.get('reasons', [])}")
        return "\n".join(lines)

    _ROLE_FRAMINGS = {
        "planner": (
            "You are the PLANNER on a payment-approval council. Argue FOR "
            "approving this payment: business value, legitimacy, cost of "
            "delay. Keep it to 1-3 sentences, referencing the actual request "
            "fields."
        ),
        "risk": (
            "You are the RISK-SCORER on a payment-approval council. Argue "
            "AGAINST (or score the risk of) this payment: fraud signals, "
            "amount, merchant, category, policy outcome. Keep it to 1-3 "
            "sentences, referencing the actual request fields."
        ),
        "auditor": (
            "You are the AUDITOR on a payment-approval council. You have read "
            "the planner's and risk-scorer's arguments above. Issue the final "
            "verdict with reasoning in 1-3 sentences. If the policy result was "
            "not approved, you MUST reject. End your response with a line of "
            "the exact form: VERDICT: AUTO_PAY | VERDICT: QUEUE | "
            "VERDICT: REJECT"
        ),
    }

    def _parse_verdict(self, auditor_text: str) -> str:
        """Robustly extract VERDICT: <AUTO_PAY|QUEUE|REJECT>; default QUEUE."""
        upper = auditor_text.upper()
        for v in _VALID_VERDICTS:
            if f"VERDICT: {v}" in upper or f"VERDICT:{v}" in upper:
                return v
        return "QUEUE"

    def _debate_live(self, request: dict, policy_result: dict) -> dict:
        context = self._build_prompt(request, policy_result)
        transcript = []

        try:
            planner_text = self.llm(self._ROLE_FRAMINGS["planner"] + "\n\n" + context)
            risk_text = self.llm(self._ROLE_FRAMINGS["risk"] + "\n\n" + context)
            auditor_prompt = (
                self._ROLE_FRAMINGS["auditor"] + "\n\n" + context
                + "\n\nPlanner's argument:\n" + str(planner_text)
                + "\n\nRisk-scorer's argument:\n" + str(risk_text)
            )
            auditor_text = self.llm(auditor_prompt)
        except Exception as exc:  # never crash on LLM failure
            transcript = [
                {"role": "planner", "text": "LLM call failed; debate aborted."},
                {"role": "risk", "text": "LLM call failed; debate aborted."},
                {"role": "auditor", "text": "LLM call failed; debate aborted."},
            ]
            return {
                "verdict": "QUEUE",
                "reasoning": (
                    "The LLM raised an error during the council debate "
                    f"({type(exc).__name__}: {exc}), so no verdict could be "
                    "reached. The payment is queued for human review instead "
                    "of being auto-paid or rejected blind."
                ),
                "transcript": transcript,
                "risk_score": float(policy_result.get("risk_score", 0.0)),
            }

        transcript = [
            {"role": "planner", "text": str(planner_text)},
            {"role": "risk", "text": str(risk_text)},
            {"role": "auditor", "text": str(auditor_text)},
        ]
        verdict = self._parse_verdict(str(auditor_text))

        reasoning = (
            f"The auditor's parsed verdict is {verdict}. "
            f"(Policy reported {'approval' if policy_result.get('approved') else 'rejection'} "
            f"at risk score {policy_result.get('risk_score', 0.0)}.)"
        )
        if verdict == "QUEUE" and "VERDICT" not in str(auditor_text).upper():
            reasoning += (
                " The auditor's response did not contain a parseable VERDICT "
                "line, so the council defaulted to QUEUE for human review."
            )

        return {
            "verdict": verdict,
            "reasoning": reasoning,
            "transcript": transcript,
            "risk_score": float(policy_result.get("risk_score", 0.0)),
        }


if __name__ == "__main__":
    # --- Scenario 1: low-risk approved payment -> expect AUTO_PAY ---
    req1 = {
        "id": "req-001",
        "agent_id": "agent-office-01",
        "amount": 49.99,
        "currency": "USD",
        "description": "Office supplies restock",
        "merchant": "Staples",
        "category": "supplies",
        "timestamp": "2026-10-11T08:00:00-04:00",
    }
    policy1 = {"approved": True, "risk_score": 0.05, "checks": [], "reasons": []}

    # --- Scenario 2: policy-rejected high-risk wire -> expect REJECT ---
    req2 = {
        "id": "req-002",
        "agent_id": "agent-office-01",
        "amount": 9500.0,
        "currency": "USD",
        "description": "Urgent wire transfer",
        "merchant": "Unknown Offshore LLC",
        "category": "wire",
        "timestamp": "2026-10-11T08:00:00-04:00",
    }
    policy2 = {
        "approved": False,
        "risk_score": 0.8,
        "checks": ["amount_limit", "merchant_reputation"],
        "reasons": ["amount exceeds per-transaction limit", "merchant not in allowlist"],
    }

    council = Council()  # demo mode
    for name, req, pol, expect in (("Scenario 1", req1, policy1, "AUTO_PAY"),
                                   ("Scenario 2", req2, policy2, "REJECT")):
        result = council.debate(req, pol)
        status = "PASS" if result["verdict"] == expect else "FAIL"
        print(f"=== {name} [{status}] ===")
        print(f"verdict: {result['verdict']} (expected {expect})")
        print(f"risk_score: {result['risk_score']}")
        print(f"reasoning: {result['reasoning']}")
        for entry in result["transcript"]:
            print(f"  [{entry['role']}] {entry['text']}")
        print()

    # --- Determinism check: same input -> same output ---
    r1a = council.debate(req1, policy1)
    r1b = council.debate(req1, policy1)
    print("determinism check:", "PASS" if r1a == r1b else "FAIL")

    # --- LIVE mode smoke test with a stub LLM (no network) ---
    def stub_llm(prompt: str) -> str:
        if "AUDITOR" in prompt:
            return "After weighing both sides I approve. VERDICT: AUTO_PAY"
        return "Stubbed role argument."

    live = Council(llm=stub_llm, demo=False)
    live_result = live.debate(req1, policy1)
    print("live-mode stub test:",
          "PASS" if live_result["verdict"] == "AUTO_PAY" and len(live_result["transcript"]) == 3 else "FAIL")

    # --- LIVE mode failure fallback: LLM raises -> QUEUE, no crash ---
    def bad_llm(prompt: str) -> str:
        raise RuntimeError("simulated outage")

    fallback = Council(llm=bad_llm, demo=False).debate(req1, policy1)
    print("live-mode fallback test:",
          "PASS" if fallback["verdict"] == "QUEUE" else "FAIL")
