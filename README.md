# PayAgent Mesh

**Let AI agents spend money — without handing an LLM your credit card.**

Autonomous agents need to buy things: API credits, office supplies, travel,
domain renewals. Today nobody lets an agent spend real money, because an LLM
with a payment method is a fraud incident waiting to happen. PayAgent Mesh is
the missing layer: every payment request from an agent passes through a
**policy engine** (hard limits, denylists, per-agent budgets) and an **AI
council** (planner, risk analyst, auditor) that debates it. Safe payments
auto-execute against the PayPal Sandbox; suspicious ones land in a **human
review queue**; blocked ones are rejected. Every step is written to an
append-only **audit log**.

## Architecture

```
                          +-----------------+
                          |     Agent       |
                          | (agent-office-01|
                          |  agent-ops-02)  |
                          +--------+--------+
                                   | PaymentRequest {id, agent_id, amount,
                                   |  currency, description, merchant,
                                   v  category, timestamp}
                          +-----------------+
                          |  Policy Engine  |  spend caps, per-agent budgets,
                          |  policy.py      |  merchant denylist, risk score
                          +--------+--------+
                                   | {approved, risk_score, checks, reasons}
                                   v
                          +-----------------+
                          |   AI Council    |  planner (blue) argues for,
                          |   council.py    |  risk analyst (amber) argues against,
                          |                 |  auditor (green) checks policy
                          +--------+--------+
              +------------+        |        +------------+
              | verdict              | verdict             | verdict
              v            AUTO_PAY  | QUEUE               v  REJECT
     +----------------+    +--------+--------+    +----------------+
     | PayPal Sandbox |    | Human Review    |    |   Rejected +     |
     | paypal_client  |    | Queue           |    |   audit entry    |
     | create +       |    | queue.py        |    +----------------+
     | capture order  |    | Approve / Reject|          |
     +----------------+    +-----------------+          |
              |                      |                    |
              +----------------------+--------------------+
                                     v
                          +-----------------+
                          |   Audit Log     |  JSONL, every event:
                          |   audit.py      |  check, debate, queued,
                          |                 |  executed, decided
                          +-----------------+
```

## Why PayPal AND AI are each load-bearing

- **PayPal is load-bearing.** Sandbox accounts are free, no card required, and
  orders API semantics (create → capture) are exactly how a real agent payment
  would flow. PayPal handles the money; the mesh never touches card numbers.
- **AI is load-bearing.** Static rules can't catch a $9,500 wire to "Unknown
  Offshore LLC" that passes every numeric threshold. The council's risk
  analyst reasons about *anomalies* — merchant reputation, amount-vs-history,
  category plausibility — and forces suspicious payments into human review
  even when the policy engine says "approved." Rules set the floor; the
  council sets the ceiling.

## Quickstart

```bash
cd paypal-ai/build
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5057
```

The app starts in **demo mode** when no PayPal credentials are set (no real
money anywhere). Click a seeded scenario button and watch the council debate.

## Running with real sandbox credentials

1. Go to https://developer.paypal.com — sign up free, no card needed.
2. Dashboard → Apps & Credentials → Sandbox → create an app, copy the
   Client ID and Secret.
3. `export PAYPAL_SANDBOX_CLIENT_ID=...`
   `export PAYPAL_SANDBOX_SECRET=...`
4. Restart `python app.py`. The banner flips to LIVE SANDBOX (still sandbox —
   never real money).

## Seeded scenarios

| Button | Payload | Expected outcome |
|---|---|---|
| Routine: $49.99 at Staples | agent-office-01, office supplies | **AUTO-PAY** — policy clean, council low risk, PayPal order created + captured (DEMO- IDs in demo mode) |
| Anomaly: $9,500 wire to Unknown Offshore LLC | agent-ops-02, misc | **QUEUE** (or REJECT) — policy flags high amount, council spots the anomalous merchant, human review required |
| Blocked: $120 at CryptoMixer | agent-ops-02, misc | **REJECT** — merchant denylist in `policy.yaml` blocks instantly, no council vote needed |

## Repo layout

```
build/
├── app.py            # Flask demo app + self-contained UI (port 5057)
├── queue.py          # Human review queue (JSON persistence)
├── policy.py         # Policy engine: caps, denylist, budgets, risk score
├── audit.py          # Append-only JSONL audit log
├── council.py        # AI council debate (planner / risk / auditor)
├── paypal_client.py  # PayPal Orders v2 wrapper (demo mode w/o creds)
├── policy.yaml       # Denylist, spend caps, per-agent budgets
├── requirements.txt
├── README.md
├── LICENSE           # MIT
├── queue.json        # review-queue state (created at runtime)
└── audit.jsonl       # audit log (created at runtime)
```

## License

MIT, copyright 2026 David Cain. See `LICENSE`.
