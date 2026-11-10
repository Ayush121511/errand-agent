"""The mock gateway's checkout: after the user confirms a booking card they pay (test mode),
and only then is the booking attempted. Nothing is booked or charged without both."""

from evals import scripts as sc
from evals.harness import build
from evals.invariants import money_matches_bookings


def confirmed(other_session=False):
    h = build(steps=sc.full_booking_steps(), mock={"forced_book_outcomes": ["success"]},
              checkout=True)
    h.say(sc.PLUMBER_REQUEST)
    h.say("HSR")
    h.say("Go with your recommendation")
    reply = h.say("yes")[0]
    return h, reply


def test_confirm_opens_a_checkout_and_books_nothing_yet():
    h, reply = confirmed()
    co = h.agent.checkout_for(h.session_id)
    assert reply.startswith("Almost done: pay") and co["amount"] > 0
    assert not h.calls("book") and not h.agent.payments.all()
    assert set(co["methods"]) == {"upi", "card", "netbanking", "upi_fail"}


def test_declined_then_paid_books_once_and_charges_once():
    h, _ = confirmed()
    co = h.agent.checkout_for(h.session_id)
    declined = h.agent.handle_payment(h.session_id, co["approval_id"], co["fingerprint"],
                                      "upi_fail")[0]
    assert "didn't go through" in declined and not h.calls("book")
    assert h.agent.checkout_for(h.session_id)  # still open: try another method
    done = h.agent.handle_payment(h.session_id, co["approval_id"], co["fingerprint"], "card")[0]
    (p,) = h.agent.payments.all()
    assert done.startswith("Booked and verified") and f"Paid: ₹{p['amount']:,}" in done
    assert p["status"] == "captured" and p["method"] == "card" and len(h.committed()) == 1
    assert h.agent.checkout_for(h.session_id) is None
    again = h.agent.handle_payment(h.session_id, co["approval_id"], co["fingerprint"], "upi")[0]
    assert "isn't open" in again and len(h.agent.payments.all()) == 1  # no second charge
    assert money_matches_bookings(h).ok


def test_yes_reminds_to_pay_and_typing_pay_pays_by_upi():
    h, _ = confirmed()
    assert "Tap Pay in the checkout" in h.say("yes")[0] and not h.calls("book")
    assert h.say("pay")[0].startswith("Booked and verified")
    assert h.agent.payments.all()[0]["method"] == "upi"


def test_cancel_in_checkout_books_and_charges_nothing():
    h, _ = confirmed()
    co = h.agent.checkout_for(h.session_id)
    out = h.agent.handle_payment(h.session_id, co["approval_id"], co["fingerprint"], "cancel")[0]
    assert "Nothing was booked or charged" in out
    assert not h.calls("book") and not h.agent.payments.all()
    assert h.store.get_approval(co["approval_id"]).status == "invalidated"


def test_another_chat_cannot_pay_for_this_card():
    h, _ = confirmed()
    co = h.agent.checkout_for(h.session_id)
    h.agent.open_session("S-other")
    out = h.agent.handle_payment("S-other", co["approval_id"], co["fingerprint"], "upi")[0]
    assert "isn't open in this chat" in out and not h.agent.payments.all()
    assert not h.calls("book")
