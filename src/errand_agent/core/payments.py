"""Payments: authorise on confirm, capture only after the booking is verified, void if it
fails, refund on cancellation. Every money movement is posted to a double-entry ledger.

  user confirms ─► authorize (key from the booking attempt) ─► book (existing saga)
      booked and verified ─► capture ─► ledger: customer → escrow
      not booked ..........─► void (no money moved, no ledger entry)
      can't tell ..........─► leave authorised; a reconcile job decides later
  cancelled (by user or provider) ─► refund ─► ledger: escrow → customer
  visit done (nightly job) ─► settle ─► ledger: escrow → provider

Gateway timeouts are handled like provider timeouts: retry with the SAME idempotency key,
and when in doubt ask the gateway for the payment's status instead of guessing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from errand_agent.core.clock import Clock
from errand_agent.core.store import AgentStore
from errand_agent.mocks.payments import GatewayTimeout, MockPaymentGateway
from errand_agent.obs.trace import Tracer

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,         -- the gateway's id
    attempt_id TEXT NOT NULL UNIQUE,     -- one payment per booking attempt
    approval_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    amount INTEGER NOT NULL,             -- rupees
    status TEXT NOT NULL,                -- authorized|captured|capture_unknown|voided|refunded
    booking_id TEXT,
    settled INTEGER NOT NULL DEFAULT 0,  -- paid out to the provider
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    account TEXT NOT NULL,               -- customer:<user> | escrow | provider:<id>
    amount INTEGER NOT NULL,             -- signed; each posting is two rows summing to 0
    memo TEXT NOT NULL
);
"""
TRIES = 3
DISPUTE_WINDOW = timedelta(hours=24)  # time to report a no-show before the payout


class Payments:
    def __init__(self, store: AgentStore, gateway: MockPaymentGateway, clock: Clock) -> None:
        self.store, self.gateway, self.clock = store, gateway, clock
        with store.lock:
            store.conn.executescript(SCHEMA)
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(payments)")}
            if "method" not in cols:
                store.conn.execute("ALTER TABLE payments ADD COLUMN method TEXT")

    # ---------------------------------------------------------------- rows
    def _rows(self, where: str = "1=1", args: tuple = ()) -> list[dict[str, Any]]:
        with self.store.lock:
            rows = self.store.conn.execute(
                f"SELECT * FROM payments WHERE {where} ORDER BY created_at, payment_id", args
            ).fetchall()
        return [dict(r) for r in rows]

    def all(self) -> list[dict[str, Any]]:
        return self._rows()

    def get(self, payment_id: str) -> dict[str, Any] | None:
        rows = self._rows("payment_id=?", (payment_id,))
        return rows[0] if rows else None

    def for_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        rows = self._rows("attempt_id=?", (attempt_id,))
        return rows[0] if rows else None

    def for_approval(self, approval_id: str) -> dict[str, Any] | None:
        rows = self._rows("approval_id=?", (approval_id,))
        return rows[-1] if rows else None

    def paid_for(self, approval_id: str) -> bool:
        """The user has authorised payment for this booking card (checkout done)."""
        p = self.for_approval(approval_id)
        return bool(p and p["status"] == "authorized")

    def authorize_checkout(self, tracer: Tracer, user_id: str, approval: Any, method: str
                           ) -> tuple[dict[str, Any] | None, str | None]:
        """The user pressed Pay in the checkout. Runs before the booking: the booking is only
        attempted once the money is authorised, and only captured once it is verified."""
        if self.paid_for(approval.approval_id):
            return self.for_approval(approval.approval_id), None
        d = approval.details
        res = self._call(tracer, "authorize", lambda: self.gateway.authorize(
            user_id, int(d["price"]), approval.approval_id,
            f"pa-{approval.approval_id}-{method}", method=method))
        if res is None:
            return None, "payment_unavailable"
        if res.get("status") != "authorized":
            tracer.event("payment_declined", approval_id=approval.approval_id, method=method,
                         reason=res.get("reason"))
            return None, res.get("reason") or "declined"
        self._insert(res["payment_id"], f"pending:{approval.approval_id}", approval, user_id,
                     method)
        tracer.event("payment_authorized", payment_id=res["payment_id"], amount=int(d["price"]),
                     method=method, via="checkout")
        return self.get(res["payment_id"]), None

    def _insert(self, payment_id: str, attempt_id: str, approval: Any, user_id: str,
                method: str) -> None:
        d = approval.details
        now = self.clock.now().isoformat()
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO payments (payment_id, attempt_id, approval_id, session_id, user_id, "
                "provider_id, slot, amount, status, method, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (payment_id, attempt_id, approval.approval_id, approval.session_id, user_id,
                 d["provider_id"], d["slot"], int(d["price"]), "authorized", method, now, now))

    def _set(self, payment_id: str, **fields: Any) -> dict[str, Any]:
        fields["updated_at"] = self.clock.now().isoformat()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.store.lock:
            self.store.conn.execute(f"UPDATE payments SET {cols} WHERE payment_id=?",
                                    (*fields.values(), payment_id))
        row = self.get(payment_id)
        assert row is not None
        return row

    def _post(self, payment_id: str, memo: str, src: str, dst: str, amount: int) -> None:
        ts = self.clock.now().isoformat()
        with self.store.lock:
            self.store.conn.execute("BEGIN IMMEDIATE")
            self.store.conn.executemany(
                "INSERT INTO ledger (payment_id, ts, account, amount, memo) VALUES (?,?,?,?,?)",
                [(payment_id, ts, src, -amount, memo), (payment_id, ts, dst, amount, memo)])
            self.store.conn.execute("COMMIT")

    def ledger(self, payment_id: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM ledger", ()
        if payment_id:
            q, args = q + " WHERE payment_id=?", (payment_id,)
        with self.store.lock:
            return [dict(r) for r in self.store.conn.execute(q + " ORDER BY entry_id", args)]

    def balances(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.ledger():
            out[e["account"]] = out.get(e["account"], 0) + e["amount"]
        return out

    # ---------------------------------------------------------------- gateway calls
    def _call(self, tracer: Tracer, op: str, fn, check=None) -> dict[str, Any] | None:
        """Call with the same key up to TRIES times. On a timeout, `check` (a status read)
        may settle it. None means we still can't tell."""
        for n in range(1, TRIES + 1):
            try:
                res = fn()
                tracer.event("payment_call", op=op, attempt=n, result=res)
                return res
            except GatewayTimeout:
                tracer.event("payment_call", op=op, attempt=n, result="timeout")
                if check:
                    done = check()
                    if done is not None:
                        return done
        return None

    # ---------------------------------------------------------------- operations
    def authorize(self, tracer: Tracer, user_id: str, attempt: Any, approval: Any
                  ) -> tuple[dict[str, Any] | None, str | None]:
        """(payment, None) or (None, reason). Idempotent per booking attempt."""
        existing = self.for_attempt(attempt.attempt_id)
        if existing:
            return existing, None
        paid = self.for_approval(approval.approval_id)
        if paid and paid["status"] == "authorized":  # paid at checkout: link it to the attempt
            return self._set(paid["payment_id"], attempt_id=attempt.attempt_id), None
        d = approval.details
        res = self._call(tracer, "authorize", lambda: self.gateway.authorize(
            user_id, int(d["price"]), attempt.attempt_id, "pa-" + attempt.idempotency_key))
        if res is None:
            return None, "payment_unavailable"
        if res.get("status") != "authorized":
            return None, "payment_declined"
        self._insert(res["payment_id"], attempt.attempt_id, approval, user_id, "saved")
        tracer.event("payment_authorized", payment_id=res["payment_id"], amount=int(d["price"]),
                     method="saved")
        return self.get(res["payment_id"]), None

    def capture(self, tracer: Tracer, attempt_id: str, booking_id: str | None
                ) -> dict[str, Any] | None:
        p = self.for_attempt(attempt_id)
        if p is None or p["status"] in ("captured", "refunded"):
            return p
        if booking_id:
            p = self._set(p["payment_id"], booking_id=booking_id)
        pid = p["payment_id"]

        def check() -> dict[str, Any] | None:
            st = self.gateway.get_payment(pid).get("status")
            return {"status": "captured"} if st == "captured" else None

        res = self._call(tracer, "capture",
                         lambda: self.gateway.capture(pid, "pc-" + attempt_id), check)
        if res is None or res.get("status") != "captured":
            tracer.event("payment_capture_unknown", payment_id=pid)
            return self._set(pid, status="capture_unknown")
        return self._captured(tracer, p)

    def _captured(self, tracer: Tracer, p: dict[str, Any]) -> dict[str, Any]:
        self._post(p["payment_id"], "captured after verified booking",
                   f"customer:{p['user_id']}", "escrow", p["amount"])
        tracer.event("payment_captured", payment_id=p["payment_id"], amount=p["amount"],
                     booking_id=p["booking_id"])
        return self._set(p["payment_id"], status="captured")

    def void(self, tracer: Tracer, attempt_id: str) -> dict[str, Any] | None:
        p = self.for_attempt(attempt_id)
        if p is None or p["status"] != "authorized":
            return p
        pid = p["payment_id"]

        def check() -> dict[str, Any] | None:
            st = self.gateway.get_payment(pid).get("status")
            return {"status": "voided"} if st == "voided" else None

        res = self._call(tracer, "void", lambda: self.gateway.void(pid, "pv-" + attempt_id), check)
        if res is None or res.get("status") != "voided":
            return p  # still authorised; the reconcile job tries again
        tracer.event("payment_voided", payment_id=pid, amount=p["amount"])
        return self._set(pid, status="voided")

    def refund_booking(self, tracer: Tracer, booking_id: str, why: str
                       ) -> dict[str, Any] | None:
        rows = self._rows("booking_id=? AND status='captured'", (booking_id,))
        if not rows:
            return None
        p = rows[0]
        pid = p["payment_id"]

        def check() -> dict[str, Any] | None:
            st = self.gateway.get_payment(pid).get("status")
            return {"status": "refunded"} if st == "refunded" else None

        res = self._call(tracer, "refund", lambda: self.gateway.refund(
            pid, p["amount"], "pr-" + pid), check)
        if res is None or res.get("status") != "refunded":
            tracer.event("payment_refund_pending", payment_id=pid)
            return None
        src = f"provider:{p['provider_id']}" if p["settled"] else "escrow"
        self._post(pid, f"refund: {why}", src, f"customer:{p['user_id']}", p["amount"])
        tracer.event("payment_refunded", payment_id=pid, amount=p["amount"], reason=why)
        return self._set(pid, status="refunded")

    def reconcile(self, tracer: Tracer, payment_id: str, attempt_status: str | None,
                  booking_id: str | None) -> str:
        """Settle a payment whose outcome was left open. Returns what happened."""
        p = self.get(payment_id)
        if p is None:
            return "missing"
        g = self.gateway.get_payment(payment_id).get("status")
        if p["status"] == "capture_unknown":
            if g == "captured":
                self._captured(tracer, p)
                return "captured"
            p = self._set(payment_id, status="authorized")
        if p["status"] != "authorized":
            return p["status"]
        if attempt_status == "confirmed":
            done = self.capture(tracer, p["attempt_id"], booking_id)
            return done["status"] if done else "missing"
        if attempt_status == "failed":
            done = self.void(tracer, p["attempt_id"])
            return done["status"] if done else "missing"
        return "waiting"  # booking outcome still unknown

    def settle(self, tracer: Tracer, provider_status: Any) -> dict[str, list[str]]:
        """Nightly: settle captured payments by what the provider says happened.

        `provider_status(payment)` returns the booking's status from the provider's
        `list_bookings` (the source of truth, as get_booking_status is for bookings):
          completed, and the 24 h dispute window has passed ─► pay the provider
          no_show or cancelled ─────────────────────────────► refund the customer
          anything else (upcoming, or still in the window) ─► wait
        """
        now = self.clock.now()
        out: dict[str, list[str]] = {"paid": [], "refunded": [], "waiting": []}
        for p in self._rows("status='captured' AND settled=0"):
            st = provider_status(p)
            visit = datetime.fromisoformat(p["slot"]).replace(tzinfo=now.tzinfo)
            if st == "completed" and now - visit >= DISPUTE_WINDOW:
                self._post(p["payment_id"], "payout for completed visit", "escrow",
                           f"provider:{p['provider_id']}", p["amount"])
                self._set(p["payment_id"], settled=1)
                out["paid"].append(p["payment_id"])
            elif st == "no_show" or (st or "").startswith("cancelled"):
                why = "the visit didn't happen" if st == "no_show" else f"booking {st}"
                if self.refund_booking(tracer, p["booking_id"], why):
                    out["refunded"].append(p["payment_id"])
            else:
                out["waiting"].append(p["payment_id"])
        tracer.event("payouts_settled", **out)
        return out
