"""PayAgent Mesh — demo web app.

One-file Flask demo for the hackathon <3-min video. Graceful degradation:
component modules (policy, paypal_client, council, audit) are imported
lazily per-request so a missing file yields HTTP 500 JSON naming the module
instead of crashing the process.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_PATH = os.path.join(BASE_DIR, "audit.jsonl")
QUEUE_PATH = os.path.join(BASE_DIR, "queue.json")

CATEGORIES = ["office", "software", "travel", "marketing", "misc"]
AGENTS = ["agent-office-01", "agent-ops-02"]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _need(module_name: str):
    """Load a sibling component module from this directory's file.

    Loads by explicit file path (not bare __import__) so stdlib modules
    with the same name (e.g. `queue`) can't shadow the component, and a
    missing file yields a named MissingComponent instead of a crash.
    """
    import importlib.util

    path = os.path.join(BASE_DIR, module_name + ".py")
    if not os.path.isfile(path):
        raise MissingComponent(module_name)
    spec = importlib.util.spec_from_file_location(f"payagent_{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MissingComponent(Exception):
    def __init__(self, module_name: str):
        super().__init__(f"component module '{module_name}' is not available yet")
        self.module_name = module_name


@app.errorhandler(MissingComponent)
def _missing_component(err: MissingComponent):
    return jsonify({"error": f"missing component module: {err.module_name}"}), 500


# ---------------------------------------------------------------- helpers
def _policy():
    return _need("policy").PolicyEngine(policy_path=os.path.join(BASE_DIR, "policy.yaml"))


def _client():
    return _need("paypal_client").PayPalClient.from_env()


def _council():
    return _need("council").Council(demo=True)


def _audit():
    return _need("audit").AuditLog(AUDIT_PATH)


def _queue():
    return _need("review_queue").ReviewQueue(path=QUEUE_PATH)


def _demo_mode() -> bool:
    try:
        client = _client()
    except MissingComponent:
        return True
    return bool(getattr(client, "demo", True))


def _execute_payment(pay_request: dict) -> dict:
    """Create + capture a PayPal order, record spend, audit the execution."""
    client = _client()
    order = client.create_order(pay_request)
    capture = client.capture_order(order["order_id"])
    _policy().record_spend(pay_request["agent_id"], float(pay_request["amount"]))
    _audit().record(
        "payment_executed",
        {
            "request_id": pay_request["id"],
            "agent_id": pay_request["agent_id"],
            "amount": pay_request["amount"],
            "currency": pay_request["currency"],
            "order_id": order["order_id"],
            "capture_id": capture["capture_id"],
            "demo": order.get("demo"),
        },
    )
    return {"order": order, "capture": capture}


def _run_pipeline(payload: dict) -> dict:
    agent_id = payload.get("agent_id", AGENTS[0])
    amount = float(payload.get("amount", 0))
    merchant = payload.get("merchant", "")
    description = payload.get("description", "")
    category = payload.get("category", "misc")

    pay_request = {
        "id": "req-" + uuid.uuid4().hex[:8],
        "agent_id": agent_id,
        "amount": amount,
        "currency": "USD",
        "description": description,
        "merchant": merchant,
        "category": category,
        "timestamp": _utcnow(),
    }

    policy = _policy()
    policy_result = policy.check(pay_request)
    debate = _council().debate(pay_request, policy_result)

    verdict = debate.get("verdict")
    paypal_result = None
    queue_id = None

    if verdict == "AUTO_PAY":
        paypal_result = _execute_payment(pay_request)
    elif verdict == "QUEUE":
        queue_id = _queue().add(pay_request, policy_result, debate)
        _audit().record(
            "payment_queued",
            {
                "request_id": pay_request["id"],
                "queue_id": queue_id,
                "amount": amount,
                "risk_score": debate.get("risk_score"),
            },
        )
    else:  # REJECT or anything else
        _audit().record(
            "payment_rejected",
            {
                "request_id": pay_request["id"],
                "amount": amount,
                "reasons": policy_result.get("reasons"),
                "reasoning": debate.get("reasoning"),
            },
        )

    return {
        "request": pay_request,
        "policy": policy_result,
        "debate": debate,
        "paypal_result": paypal_result,
        "queue_id": queue_id,
    }


# ------------------------------------------------------------------- routes
@app.get("/")
def index():
    demo = _demo_mode()
    html = INDEX_HTML.replace("__DEMO_BANNER__", _demo_banner_html(demo))
    return html


def _demo_banner_html(demo: bool) -> str:
    if not demo:
        return (
            '<div class="banner live">● LIVE SANDBOX — '
            "real PayPal sandbox calls (no real money)</div>"
        )
    return (
        '<div class="banner">● DEMO MODE — simulated PayPal sandbox, no real money</div>'
    )


@app.post("/api/request")
def api_request():
    payload = request.get_json(force=True) or {}
    try:
        return jsonify(_run_pipeline(payload))
    except MissingComponent as e:
        raise e


@app.post("/api/queue/<item_id>/decision")
def api_decision(item_id: str):
    payload = request.get_json(force=True) or {}
    approved = bool(payload.get("approved", False))
    q = _queue()
    item = q.decide(item_id, approved)
    if approved:
        pay_request = item["request"]
        paypal_result = _execute_payment(pay_request)
        _audit().record(
            "payment_approved_executed",
            {
                "request_id": pay_request["id"],
                "queue_id": item_id,
                "amount": pay_request["amount"],
                "order_id": paypal_result["order"]["order_id"],
                "capture_id": paypal_result["capture"]["capture_id"],
            },
        )
        item["paypal_result"] = paypal_result
    else:
        _audit().record(
            "payment_rejected",
            {
                "request_id": item["request"]["id"],
                "queue_id": item_id,
                "via": "human_review",
            },
        )
    return jsonify(item)


@app.get("/api/queue")
def api_queue():
    return jsonify(_queue().pending())


@app.get("/api/audit")
def api_audit():
    n = int(request.args.get("n", 50))
    return jsonify(_audit().tail(n))


# -------------------------------------------------------------- index page
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PayAgent Mesh — AI payments with guardrails</title>
<style>
  :root { --bg:#0b1220; --panel:#111a2e; --line:#22304f; --txt:#e8eefb;
          --dim:#8ea0c2; --blue:#4da3ff; --amber:#f5b544; --green:#3ddc84; --red:#ff5d5d; }
  * { box-sizing:border-box; font-family: ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif; }
  body { margin:0; background:var(--bg); color:var(--txt); }
  header { padding:18px 24px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:14px; }
  header h1 { font-size:20px; margin:0; }
  header h1 span { color:var(--blue); }
  .banner { margin-left:auto; padding:6px 14px; border-radius:999px; font-size:12px;
            background:#2a2410; border:1px solid var(--amber); color:var(--amber); }
  .banner.live { background:#0f2a1a; border-color:var(--green); color:var(--green); }
  main { display:grid; grid-template-columns: 320px 1fr 340px; gap:16px; padding:16px 24px 32px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .card h2 { margin:0 0 12px; font-size:14px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); }
  label { display:block; font-size:12px; color:var(--dim); margin:10px 0 4px; }
  input, select, textarea { width:100%; padding:8px 10px; border-radius:8px; border:1px solid var(--line);
      background:#0d1526; color:var(--txt); font-size:14px; }
  button { cursor:pointer; border:none; border-radius:8px; padding:10px 14px; font-size:14px; font-weight:600; }
  .btn-primary { width:100%; margin-top:14px; background:var(--blue); color:#04121f; }
  .btn-primary:hover { filter:brightness(1.1); }
  .scenario { display:block; width:100%; margin-top:8px; text-align:left; background:#0d1526;
      border:1px solid var(--line); color:var(--txt); font-weight:400; }
  .scenario:hover { border-color:var(--blue); }
  .scenario b { display:block; font-size:13px; }
  .scenario small { color:var(--dim); }
  #verdict { border-radius:10px; padding:12px; margin-bottom:12px; font-weight:700; text-align:center; display:none; }
  #verdict.auto { display:block; background:#0f2a1a; border:1px solid var(--green); color:var(--green); }
  #verdict.queued { display:block; background:#2a2410; border:1px solid var(--amber); color:var(--amber); }
  #verdict.rejected { display:block; background:#2e1414; border:1px solid var(--red); color:var(--red); }
  #transcript { max-height:420px; overflow-y:auto; display:flex; flex-direction:column; gap:10px; }
  .bubble { max-width:88%; padding:10px 12px; border-radius:12px; font-size:13.5px; line-height:1.45; }
  .bubble .role { display:block; font-size:11px; font-weight:700; text-transform:uppercase; margin-bottom:4px; }
  .bubble.planner { align-self:flex-start; background:#10233f; border:1px solid #24507f; }
  .bubble.planner .role { color:var(--blue); }
  .bubble.risk { align-self:flex-end; background:#2a2110; border:1px solid #7f5f24; }
  .bubble.risk .role { color:var(--amber); }
  .bubble.auditor { align-self:flex-start; background:#0f2a1a; border:1px solid #247f52; }
  .bubble.auditor .role { color:var(--green); }
  .bubble.system { align-self:center; background:#1a2133; border:1px dashed var(--line); color:var(--dim); font-size:12.5px; }
  .qitem { border:1px solid var(--line); border-radius:10px; padding:10px; margin-bottom:10px; background:#0d1526; }
  .qitem .meta { font-size:12px; color:var(--dim); margin:4px 0; }
  .qitem .amt { font-size:18px; font-weight:700; }
  .qitem .btns { display:flex; gap:8px; margin-top:8px; }
  .btn-approve { flex:1; background:var(--green); color:#04140a; }
  .btn-reject { flex:1; background:var(--red); color:#1f0404; }
  #audit { max-height:300px; overflow-y:auto; font-size:12px; }
  #audit .ev { padding:6px 4px; border-bottom:1px solid var(--line); }
  #audit .ev .t { color:var(--dim); }
  #audit .ev .e { color:var(--blue); font-weight:600; }
  .result { margin-top:12px; font-size:12.5px; color:var(--dim); word-break:break-all; }
  .result code { color:var(--txt); }
  @media (max-width:1100px){ main{ grid-template-columns:1fr; } }
</style>
</head>
<body>
<header>
  <h1>PayAgent <span>Mesh</span></h1>
  __DEMO_BANNER__
</header>
<main>
  <!-- LEFT: request form -->
  <section class="card">
    <h2>Request payment</h2>
    <label>Agent</label>
    <select id="agent">
      <option>agent-office-01</option>
      <option>agent-ops-02</option>
    </select>
    <label>Amount (USD)</label>
    <input id="amount" type="number" step="0.01" min="0" value="49.99">
    <label>Merchant</label>
    <input id="merchant" value="Staples">
    <label>Description</label>
    <input id="description" value="Office supplies">
    <label>Category</label>
    <select id="category">
      <option>office</option><option>software</option><option>travel</option>
      <option>marketing</option><option>misc</option>
    </select>
    <button class="btn-primary" onclick="submitRequest()">Send to council</button>
    <div id="result" class="result"></div>
    <h2 style="margin-top:18px">Seeded scenarios</h2>
    <button class="scenario" onclick='scenario("agent-office-01",49.99,"Staples","Office supplies","office")'>
      <b>Routine: $49.99 at Staples</b><small>expect AUTO-PAY</small></button>
    <button class="scenario" onclick='scenario("agent-ops-02",9500,"Unknown Offshore LLC","Wire transfer","misc")'>
      <b>Anomaly: $9,500 wire to Unknown Offshore LLC</b><small>expect QUEUE or REJECT</small></button>
    <button class="scenario" onclick='scenario("agent-ops-02",120,"CryptoMixer","Tumbling fee","misc")'>
      <b>Blocked: $120 at CryptoMixer</b><small>expect REJECT (denylist)</small></button>
  </section>

  <!-- CENTER: council debate -->
  <section class="card">
    <h2>Council debate</h2>
    <div id="verdict"></div>
    <div id="transcript"><div class="bubble system">No debate yet — send a payment request and watch the
      planner, risk analyst, and auditor argue it out.</div></div>
  </section>

  <!-- RIGHT: queue + audit -->
  <section>
    <div class="card" style="margin-bottom:16px">
      <h2>Human review queue</h2>
      <div id="queue"><p style="color:var(--dim);font-size:13px">Queue is empty.</p></div>
    </div>
    <div class="card">
      <h2>Audit trail</h2>
      <div id="audit"></div>
    </div>
  </section>
</main>
<script>
const ROLE_CLASS = { planner:"planner", risk:"risk", auditor:"auditor" };

async function api(url, opts={}) {
  const r = await fetch(url, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

function scenario(agent, amount, merchant, description, category) {
  document.getElementById("agent").value = agent;
  document.getElementById("amount").value = amount;
  document.getElementById("merchant").value = merchant;
  document.getElementById("description").value = description;
  document.getElementById("category").value = category;
  submitRequest();
}

async function submitRequest() {
  const payload = {
    agent_id: document.getElementById("agent").value,
    amount: parseFloat(document.getElementById("amount").value),
    merchant: document.getElementById("merchant").value,
    description: document.getElementById("description").value,
    category: document.getElementById("category").value,
  };
  const out = document.getElementById("result");
  out.innerHTML = "running council...";
  try {
    const d = await api("/api/request", { method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload) });
    renderDebate(d.debate);
    let html = "";
    if (d.paypal_result) {
      html = "PAID — order <code>" + d.paypal_result.order.order_id + "</code>, " +
             "capture <code>" + d.paypal_result.capture.capture_id + "</code>";
    } else if (d.queue_id) {
      html = "QUEUED — item <code>" + d.queue_id + "</code>, awaiting human review";
    } else {
      html = "REJECTED — see policy reasons in the debate transcript";
    }
    out.innerHTML = html;
    refreshQueue(); refreshAudit();
  } catch (e) { out.innerHTML = "error: " + e.message; }
}

function renderDebate(debate) {
  const v = document.getElementById("verdict");
  const t = document.getElementById("transcript");
  const verdict = debate.verdict || "UNKNOWN";
  v.className = verdict === "AUTO_PAY" ? "auto" : verdict === "QUEUE" ? "queued" : "rejected";
  const label = verdict === "AUTO_PAY" ? "✓ AUTO-PAID" : verdict === "QUEUE" ? "⏳ QUEUED FOR REVIEW" : "✕ REJECTED";
  v.textContent = label + "  ·  risk " + (debate.risk_score ?? "?");
  t.innerHTML = "";
  (debate.transcript || []).forEach(m => {
    const cls = ROLE_CLASS[(m.role||"").toLowerCase()] || "system";
    const div = document.createElement("div");
    div.className = "bubble " + cls;
    const role = document.createElement("span");
    role.className = "role"; role.textContent = m.role || "system";
    div.appendChild(role);
    div.appendChild(document.createTextNode(m.text || ""));
    t.appendChild(div);
  });
  if (debate.reasoning) {
    const div = document.createElement("div");
    div.className = "bubble system";
    div.textContent = "Council reasoning: " + debate.reasoning;
    t.appendChild(div);
  }
}

async function refreshQueue() {
  const items = await api("/api/queue");
  const el = document.getElementById("queue");
  if (!items.length) { el.innerHTML = '<p style="color:var(--dim);font-size:13px">Queue is empty.</p>'; return; }
  el.innerHTML = "";
  items.forEach(it => {
    const r = it.request || {};
    const d = document.createElement("div");
    d.className = "qitem";
    d.innerHTML =
      '<div class="amt">$' + Number(r.amount).toFixed(2) + ' <small style="color:var(--dim)">' +
      (r.currency||"USD") + "</small></div>" +
      '<div class="meta">' + (it.id||"") + " · " + (r.agent_id||"") + "<br>" +
      (r.merchant||"") + " — " + (r.description||"") + "<br>" +
      "policy risk " + ((it.policy||{}).risk_score ?? "?") +
      " · council risk " + ((it.debate||{}).risk_score ?? "?") + "</div>" +
      '<div class="btns"><button class="btn-approve">Approve</button>' +
      '<button class="btn-reject">Reject</button></div>';
    d.querySelector(".btn-approve").onclick = () => decide(it.id, true);
    d.querySelector(".btn-reject").onclick = () => decide(it.id, false);
    el.appendChild(d);
  });
}

async function decide(id, approved) {
  await api("/api/queue/" + id + "/decision", { method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify({approved}) });
  refreshQueue(); refreshAudit();
}

async function refreshAudit() {
  const events = await api("/api/audit?n=50");
  const el = document.getElementById("audit");
  el.innerHTML = events.slice().reverse().map(e =>
    '<div class="ev"><span class="t">' + (e.ts||"").slice(11,19) + "</span> " +
    '<span class="e">' + (e.event||"") + "</span> " +
    (e.request_id ? ("· " + e.request_id) : "") +
    (e.amount ? (" · $" + Number(e.amount).toFixed(2)) : "") + "</div>"
  ).join("");
}

refreshQueue(); refreshAudit();
setInterval(refreshQueue, 5000);
setInterval(refreshAudit, 5000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=False)
