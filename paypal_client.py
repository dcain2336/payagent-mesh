"""PayPal client for the PayAgent Mesh hackathon entry.

PayAgent Mesh is policy-governed payment rails for AI agents: agents request
payments, the mesh approves them via policy + AI council, and this client
executes them via PayPal.

Two modes:

* **Demo mode** (default, ``demo=True``): a pure simulator with ZERO network
  traffic. Orders and captures return obviously-fake IDs prefixed ``DEMO-``.
  Safe to use in demos and tests — no credentials required.
* **Live sandbox mode** (``demo=False``): talks to the real PayPal sandbox
  API (https://api-m.sandbox.paypal.com). No real money moves in the sandbox.
  Requires a PayPal developer app client_id / client_secret.

Hard constraints: free-only, PayPal SANDBOX only, no real money.

The PaymentRequest dict the client receives has the shape::

    {
        "id": str,            # payment request id (mesh-side)
        "agent_id": str,      # requesting agent id
        "amount": float,      # e.g. 25.00
        "currency": "USD",
        "description": str,
        "merchant": str,
        "category": str,
        "timestamp": ISO str,
    }

Only the standard library and ``requests`` are required.
"""

import os
import time
import uuid

import requests

#: PayPal sandbox base URL — sandbox ONLY. Production is never used here.
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

#: Timeout (seconds) applied to every live-mode HTTP call.
REQUEST_TIMEOUT = 10

#: Simulated latency (seconds) in demo mode so UIs feel real.
DEMO_DELAY = 0.2


class PayPalClient:
    """Minimal PayPal Orders v2 client with a zero-network demo mode.

    Args:
        client_id: PayPal sandbox app client_id (live mode only).
        client_secret: PayPal sandbox app client_secret (live mode only).
        demo: When True (default), all calls are simulated locally and no
            network traffic occurs. When False, ``client_id`` and
            ``client_secret`` are required and real sandbox API calls are
            made via OAuth2 client-credentials.
    """

    def __init__(self, client_id=None, client_secret=None, demo=True):
        self.demo = demo
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token = None
        self._token_expires_at = 0.0
        if not demo and (not client_id or not client_secret):
            raise RuntimeError(
                "Live (demo=False) mode requires client_id and client_secret; "
                "use demo=True or PayPalClient.from_env()."
            )

    @classmethod
    def from_env(cls) -> "PayPalClient":
        """Build a client from the environment.

        Reads ``PAYPAL_SANDBOX_CLIENT_ID`` and ``PAYPAL_SANDBOX_SECRET``.
        If both are present, returns a live sandbox client (``demo=False``);
        otherwise returns a demo simulator (``demo=True``).
        """
        client_id = os.environ.get("PAYPAL_SANDBOX_CLIENT_ID")
        client_secret = os.environ.get("PAYPAL_SANDBOX_SECRET")
        if client_id and client_secret:
            return cls(client_id=client_id, client_secret=client_secret, demo=False)
        return cls(demo=True)

    # ------------------------------------------------------------------
    # Live-mode helpers
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """Return a cached OAuth2 token, refreshing it when expired.

        Uses the client-credentials grant (POST /v1/oauth2/token with HTTP
        Basic auth). Tokens are cached until 60s before expiry.
        """
        if (
            self._access_token
            and time.time() < self._token_expires_at - 60
        ):
            return self._access_token

        try:
            resp = requests.post(
                f"{SANDBOX_BASE_URL}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"PayPal sandbox token request failed: {exc}") from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"PayPal sandbox token request failed: HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError("PayPal sandbox token response missing access_token.")
        self._access_token = token
        self._token_expires_at = time.time() + int(body.get("expires_in", 0))
        return token

    def _api_post(self, path: str, payload: dict) -> dict:
        """POST ``payload`` to a sandbox API path; raise RuntimeError on error."""
        token = self._get_access_token()
        try:
            resp = requests.post(
                f"{SANDBOX_BASE_URL}{path}",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"PayPal sandbox POST {path} failed: {exc}") from exc

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"PayPal sandbox POST {path} failed: HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )
        return resp.json()

    @staticmethod
    def _order_payload(request: dict) -> dict:
        """Map a PaymentRequest dict onto a PayPal Orders v2 create payload."""
        amount = request.get("amount", 0.0)
        return {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {
                        "currency_code": request.get("currency", "USD"),
                        "value": f"{float(amount):.2f}",
                    },
                    "description": f"{request.get('description', 'payment')} "
                    f"(merchant: {request.get('merchant', 'unknown')}, "
                    f"request {request.get('id', 'n/a')})",
                }
            ],
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_order(self, request: dict) -> dict:
        """Create a PayPal order for an approved payment request.

        Live mode: POST /v2/checkout/orders with intent CAPTURE.
        Returns ``{"order_id", "status", "demo": False, "approve_url"?}``.
        The ``approve_url`` (from the ``approve`` link) is included when the
        sandbox returns one; a payer must approve the order before capture.

        Demo mode: returns an obviously-fake
        ``{"order_id": "DEMO-ORDER-<8 hex>", "status": "CREATED",
        "demo": True, "note": "simulated — no network"}`` after a short
        simulated delay. No network is touched.

        Raises:
            RuntimeError: in live mode when the sandbox call fails (never
                silently fakes a response).
        """
        if self.demo:
            time.sleep(DEMO_DELAY)
            return {
                "order_id": f"DEMO-ORDER-{uuid.uuid4().hex[:8]}",
                "status": "CREATED",
                "demo": True,
                "note": "simulated — no network",
            }

        body = self._api_post("/v2/checkout/orders", self._order_payload(request))
        result = {
            "order_id": body.get("id"),
            "status": body.get("status"),
            "demo": False,
        }
        for link in body.get("links", []) or []:
            if link.get("rel") == "approve" and link.get("href"):
                result["approve_url"] = link["href"]
                break
        if not result["order_id"]:
            raise RuntimeError(f"PayPal order creation returned no id: {body}")
        return result

    def capture_order(self, order_id: str) -> dict:
        """Capture funds for a previously created (and approved) order.

        Live mode: POST /v2/checkout/orders/{id}/capture.
        Returns ``{"capture_id", "status", "demo": False}``.

        Demo mode: ``order_id`` must start with ``"DEMO-"`` (demo IDs are
        self-consistent — you cannot capture a real-looking ID in demo
        mode). Returns ``{"capture_id": "DEMO-CAPTURE-<8 hex>",
        "status": "COMPLETED", "demo": True}`` after a short simulated
        delay. No network is touched.

        Raises:
            RuntimeError: in demo mode when ``order_id`` is not a demo ID;
                in live mode when the sandbox call fails (never silently
                fakes a response).
        """
        if self.demo:
            if not isinstance(order_id, str) or not order_id.startswith("DEMO-"):
                raise RuntimeError(
                    f"Demo capture requires a demo order id (DEMO-*); got {order_id!r}."
                )
            time.sleep(DEMO_DELAY)
            return {
                "capture_id": f"DEMO-CAPTURE-{uuid.uuid4().hex[:8]}",
                "status": "COMPLETED",
                "demo": True,
            }

        body = self._api_post(f"/v2/checkout/orders/{order_id}/capture", {})
        capture_id = None
        for unit in body.get("purchase_units", []) or []:
            payments = unit.get("payments", {}) or {}
            for cap in payments.get("captures", []) or []:
                capture_id = cap.get("id")
                break
            if capture_id:
                break
        result = {
            "capture_id": capture_id,
            "status": body.get("status"),
            "demo": False,
        }
        if not result["capture_id"]:
            raise RuntimeError(f"PayPal capture returned no capture id: {body}")
        return result


def _demo_self_test():
    """Run the demo-mode create + capture round-trip and print results."""
    print("== PayAgent Mesh PayPalClient demo self-test ==")
    client = PayPalClient(demo=True)
    request = {
        "id": "req-001",
        "agent_id": "agent-laya",
        "amount": 25.00,
        "currency": "USD",
        "description": "Sandbox test purchase",
        "merchant": "Demo Merchant",
        "category": "test",
        "timestamp": "2026-10-11T08:10:00-04:00",
    }
    order = client.create_order(request)
    print("create_order ->", order)
    assert order["demo"] is True and order["status"] == "CREATED"
    assert order["order_id"].startswith("DEMO-ORDER-")
    capture = client.capture_order(order["order_id"])
    print("capture_order ->", capture)
    assert capture["demo"] is True and capture["status"] == "COMPLETED"
    assert capture["capture_id"].startswith("DEMO-CAPTURE-")
    # Demo capture must reject non-demo order ids.
    try:
        client.capture_order("5O190127TN364715T")
    except RuntimeError as exc:
        print("non-demo capture correctly rejected:", exc)
    else:
        raise AssertionError("demo capture accepted a non-demo order id!")
    # from_env with no creds should fall back to demo mode.
    env_client = PayPalClient.from_env()
    if not (os.environ.get("PAYPAL_SANDBOX_CLIENT_ID") and os.environ.get("PAYPAL_SANDBOX_SECRET")):
        assert env_client.demo is True
        print("from_env -> demo=True (no creds in env, as expected)")
    print("PASS: demo self-test completed — zero network traffic.")


if __name__ == "__main__":
    _demo_self_test()
    if os.environ.get("PAYPAL_SANDBOX_CLIENT_ID") and os.environ.get(
        "PAYPAL_SANDBOX_SECRET"
    ):
        print("\nSandbox credentials present — live self-test SKIPPED by design "
              "(no accidental live calls from a demo self-test).")
    else:
        print("\nNo sandbox credentials in env — live self-test skipped.")
