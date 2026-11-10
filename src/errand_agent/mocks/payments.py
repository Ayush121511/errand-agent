"""Mock payment gateway (Razorpay/Stripe-like): authorise, capture, void, refund, status.

Like the provider mock, it has idempotency keys and injectable failures, because the hard
part of payments is the same as the hard part of booking: a timeout tells you nothing about
whether the money moved. `force([...])` queues outcomes for the next calls:

  "decline"                 authorise is declined
  "timeout"                 the call times out and nothing happens
  "timeout_after_success"   the call takes effect, then times out (the dangerous one)
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from errand_agent.core.clock import Clock


class GatewayTimeout(Exception):
    pass


# Test-mode payment methods, like a real gateway's test credentials. No real money or cards.
METHODS = {
    "upi": "UPI · success@test",
    "card": "Card · 4111 1111 1111 1111 (test)",
    "netbanking": "Netbanking · Test Bank",
    "upi_fail": "UPI · failure@test (always declined)",
}
DECLINING = {"upi_fail"}


class MockPaymentGateway:
    def __init__(self, clock: Clock, path: str | Path | None = None) -> None:
        self.clock = clock
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.forced: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.payments: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, dict[str, Any]] = {}   # idempotency key -> first response
        if self.path and self.path.exists():
            d = json.loads(self.path.read_text())
            self.payments, self.by_key = d["payments"], d["by_key"]

    def force(self, outcomes: list[str]) -> None:
        self.forced.extend(outcomes)

    def _save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps({"payments": self.payments, "by_key": self.by_key}))

    def _run(self, op: str, key: str, apply) -> dict[str, Any]:
        with self.lock:
            forced = self.forced.pop(0) if self.forced else "success"
            self.calls.append({"op": op, "key": key, "forced": forced,
                               "ts": self.clock.now().isoformat()})
            if key in self.by_key:  # same key, same answer, never a second side effect
                return dict(self.by_key[key])
            if forced == "timeout":
                raise GatewayTimeout(op)
            if forced == "decline" and op == "authorize":
                res = {"status": "declined", "reason": "insufficient_funds"}
            else:
                res = apply()
            self.by_key[key] = res
            self._save()
            if forced == "timeout_after_success":
                raise GatewayTimeout(op)
            return dict(res)

    # ---------------------------------------------------------------- operations
    def authorize(self, user_id: str, amount: int, reference: str, key: str,
                  method: str = "saved") -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            if method in DECLINING:
                return {"status": "declined", "reason": "declined by the bank (test method)"}
            pid = f"pay_{len(self.payments) + 1:04d}"
            self.payments[pid] = {"payment_id": pid, "user_id": user_id, "amount": amount,
                                  "reference": reference, "status": "authorized",
                                  "method": method,
                                  "refunded": 0, "created_at": self.clock.now().isoformat()}
            return {"status": "authorized", "payment_id": pid, "amount": amount}
        return self._run("authorize", key, apply)

    def capture(self, payment_id: str, key: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            p = self.payments.get(payment_id)
            if p is None or p["status"] not in ("authorized", "captured"):
                return {"status": "error", "error": f"cannot capture ({p and p['status']})"}
            p["status"] = "captured"
            return {"status": "captured", "payment_id": payment_id, "amount": p["amount"]}
        return self._run("capture", key, apply)

    def void(self, payment_id: str, key: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            p = self.payments.get(payment_id)
            if p is None or p["status"] != "authorized":
                return {"status": "error", "error": f"cannot void ({p and p['status']})"}
            p["status"] = "voided"
            return {"status": "voided", "payment_id": payment_id}
        return self._run("void", key, apply)

    def refund(self, payment_id: str, amount: int, key: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            p = self.payments.get(payment_id)
            if p is None or p["status"] != "captured" or p["refunded"] + amount > p["amount"]:
                return {"status": "error", "error": "cannot refund"}
            p["refunded"] += amount
            if p["refunded"] == p["amount"]:
                p["status"] = "refunded"
            return {"status": "refunded", "payment_id": payment_id, "amount": amount}
        return self._run("refund", key, apply)

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        with self.lock:
            self.calls.append({"op": "get_payment", "key": payment_id, "forced": None,
                               "ts": self.clock.now().isoformat()})
            p = self.payments.get(payment_id)
            return dict(p) if p else {"status": "not_found"}
