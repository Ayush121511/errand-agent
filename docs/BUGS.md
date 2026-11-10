# Bugs hit while building

These are real bugs from this build, each logged when it was found (see the git history and the AI-assistant transcript). The first three are the submission write-ups. Each was caught a different way: a contract test, an eval invariant, and a live run with the real model.

## 1. The source of truth reported "confirmed" instead of "booked"

- **What broke.** After a timeout-after-success, `get_booking_status` returned `status: "confirmed"` instead of `status: "booked"`. The executor treats "booked" as proof that the booking exists. Against this response it would have concluded *nothing was booked* and retried. The idempotency key would have prevented a duplicate in the mock, but the agent's reasoning would have been wrong at exactly the moment the brief cares most about.
- **Why.** `{"status": "booked", **booking_view}`. The booking view has its own `status` field (confirmed, cancelled and so on), and dict unpacking let it overwrite the lookup result.
- **Fix.** Store the booking's own status as `booking_status` and set the lookup `status` last.
- **Regression tests.** `tests/test_mocks.py::test_timeout_after_success_is_visible_only_via_status` and `::test_state_survives_restart`. Both failed before the fix.

## 2. Model contexts exceeded the token cap

- **What broke.** Eval S27 (60 memories, a 220-message history and a small cap) failed the global invariant "every model context within token budget". Contexts came out 9–22 tokens over.
- **Why.** `ContextBuilder` filled history up to the budget and only then added the "[N earlier messages omitted]" placeholder, so the placeholder's tokens were never reserved. The fallback that trims the latest message also used `max(200, …)` characters, which could overshoot.
- **Fix.** Reserve the placeholder's cost before filling history, and size the trim from the remaining room.
- **Regression test.** `tests/test_context.py::test_context_never_exceeds_budget_when_history_is_cut` checks 36 budgets. On the old code it fails at 4082 and 4155. Scenario S27 and the global invariant also guard it.

## 3. "fine, yes" did not confirm, and the user got stuck in a loop

- **What broke.** In a live Gemini run, the user answered a confirmation card with "fine, yes". It wasn't recognised as approval, so it went to the model, which proposed the same card again, and again. This was safe, since nothing was booked without approval, but it was unusable.
- **Why.** `is_affirmative` was a single regex of exact phrases ("yes", "yes please", "book it" and so on). The offline scripts only ever said "yes", so no scenario covered natural phrasing. Only a run with real-user wording found it.
- **Fix.** A word-based matcher. A reply approves only if every word is a confirming word or filler and at least one is a strong confirmation. Any other word (a time, "but", a provider name) makes it a change request. "cancel" and "reschedule" only count when they match the card's action, so "yes, cancel it" can never approve a *booking* card.
- **Regression tests.** `tests/test_core.py::test_natural_confirmations_accepted`, `::test_action_words_must_match_the_card` and `::test_not_affirmative`.

## Also found: the claim guard blocked honest negatives

- **What broke.** Eval scenarios S05 and S06 failed the invariant "no success claim without verification", and the text that triggered it was the guard's own fallback, "Nothing has been booked." An honest model reply such as "Nothing has been booked yet" would also have been replaced with a canned message.
- **Why.** The guard was a bare regex (`has been … booked`) that knew nothing about negation.
- **Fix.** Separate action claims from state claims, and count a match only when its clause has no negation word before it. The invariant checker uses the same function.
- **Regression tests.** `tests/test_claim_guard.py`, plus scenarios S05, S06 and S31.
