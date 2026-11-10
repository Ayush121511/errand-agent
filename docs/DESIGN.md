# Errand Agent: design note

**Shape.** One tool-using model (`gemini-3.6-flash`) runs inside deterministic code. The model interprets requests, calls read-only tools and explains options. Code owns state, approvals, side effects, memory gating and limits. A tool-less model (`gemini-3.5-flash-lite`) reads reviews in quarantine.

**State model.** Each session is a `Session{phase, task}` belonging to one user (profile in a `users` table), saved to SQLite after every step. Provider calls carry the chat's user like an auth token, so nobody can touch another user's bookings or cards. The phase machine runs `IDLE → GATHERING → SEARCHING → PRESENTING → HOLDING → AWAITING_APPROVAL → EXECUTING → VERIFYING → DONE | FAILED`, and every transition is checked against an allow-list and traced. Each task field stores its value and source (user message, memory or booking history), so a correction changes one field. Changing a search field drops the hold and the pending approval. Code computes missing fields, `ask_user` refuses known ones, and code ranks providers against budget, time and avoid constraints.

**Approval enforcement.** The model cannot book, cancel or reschedule; it can only `propose_*`. The flow is pick, then hold, then a card showing the *held* quote, rendered by code. The card's fingerprint is `hash(action, user, provider, slot, price)`. Only the user can approve: a button carrying the approval id and fingerprint, or a reply made only of confirming words ("yes but 11am" is a change). Right before acting, code recomputes the fingerprint from current state and moves the approval from approved to consumed in one compare-and-set step, so it works once. A changed price on re-hold invalidates it.

**Idempotency and recovery.** Code writes an attempt row with `key = hash(approval, hold)` *before* calling `book`. If the result is a timeout, 503 or malformed reply, code asks `get_booking_status`:
- booked: confirm;
- not booked: retry with the same key, at most 3 times;
- can't tell: report "unknown", never success.

A repeated confirmation replays the stored result. On start-up, `recover()` resolves pending attempts from the source of truth. The mock de-duplicates keys and persists separately, so it survives restarts. Success messages come from verified state, and model text claiming an action is replaced.

**Untrusted output.** Raw reviews never reach the main model; the quarantined reader returns a schema-checked summary. Even if the model is fooled, facts and memories still need a quote found in a *user* message, search arguments must match state, and side effects need approval.

| Source | May change |
|---|---|
| User message | task fields and memories (with a quote) |
| Approval | one fingerprinted action |
| Tool structured data | candidates, prices, slots (validated) |
| Tool free text, model output | nothing, or wording and proposals only |

**Payments.** Money follows the same saga as the booking. After the card is confirmed, the user pays in a test-mode checkout (the approval stays approved but unused until then; only that chat can pay, and Cancel invalidates it). Paying authorises the held price with a key derived from the booking attempt; it captures only after the booking is verified, voids if it fails and refunds on any cancellation (user or provider). A gateway timeout leads to a status read with the same key, never a second charge. An open outcome (booking unknown, capture unanswered) leaves the money authorised and schedules a reconcile job. Each movement is a balanced double-entry posting (customer → escrow → provider) in `ledger`; safety rule 10 checks after every scenario that the ledger balances and every captured payment has a live booking.

**ACID inside our database.** Every state change that must happen together is one SQLite transaction (`AgentStore.tx()`, `BEGIN IMMEDIATE`): customer → escrow plus the payment's `captured` status; escrow → provider plus `settled`; escrow → customer plus `refunded`; when the user confirms, using up the approval plus recording the booking attempt with its idempotency key; a new memory value plus retiring the old one; a new card plus retiring older ones; forgetting several facts; seeding; each background job's writes plus its "done" mark; and each provider event's "handled" mark plus the card, refund and message it produces (the provider re-delivers an event until we acknowledge it after the commit, so events are handled exactly once). Single-statement writes (messages, sessions, flags, approval compare-and-set) are atomic by themselves. *Atomic*: a crash midway rolls all of it back. *Consistent*: CHECK constraints, every posting is two rows summing to 0, and a unique index lets each payment be captured, refunded and paid out at most once. *Isolated*: the write lock is taken at the start and state is re-read inside, so two workers can't both pay out. *Durable*: WAL with `synchronous=FULL`. The bank and the provider sit outside our database, so no transaction can span them; across those boundaries we use the saga above (idempotency keys, status checks, reconcile jobs), as payment systems do. `tests/test_acid.py` crashes each step midway, races two workers and reopens the file; with transactions switched off, 9 of its 12 tests fail (the other 3 are guarded by constraints and idempotency keys).

**Jobs.** A `jobs` table is the scheduler: run time, status, attempts, lease and a unique dedupe key. A worker claims a due job by compare-and-set (`UPDATE … WHERE status='queued' OR lease expired`), so jobs run once and survive crashes and restarts; failures back off exponentially and park as `dead`. Jobs today: the evening-before visit reminder, payment reconciliation and the nightly payout, which settles by the provider's `list_bookings` status (`completed` + 24 h dispute window → pay the provider; `no_show` or cancelled → refund and notify).

**Screened input.** Before any model call, code refuses requests for restricted data (prompt, keys, other customers' data, dumps), stores each attempt and blocks the user after 3; gibberish gets a rephrase prompt. Screened messages never enter a model context (a global invariant), and they cost nothing.

**Memory and context.** Memories are *explicit* (quoted) or *inferred* (unconfirmed until the user confirms), and active, superseded or forgotten. One gate, `select_for_context`, drops forgotten, superseded, unconfirmed and irrelevant memories. Before ranking, code applies the remembered budget and earliest time for the current service itself, unless the user said otherwise in this chat. Context is the tool schemas, system prompt, STATE, MEMORY, the current turn, the current session and the last 6 earlier messages, capped at 6,000 tokens (estimate × a ratio learned from Gemini's own counts). Each call logs a manifest of included and excluded memory ids with reasons, and tests assert on it.

**Bounds and observability.** Each run allows at most 8 model calls, 12 tool calls and 45 seconds, and the executors cap their own retries. Every exit states the true state. Traces are JSONL, one file per session, recording transitions, context manifests, model calls (tokens, latency, cost) and tool calls (arguments, sanitised result, latency, attempt) as well as approval and memory events.

**Trade-offs.** A hand-written phase machine instead of a framework; an in-process mock; polling instead of webhooks; a mock payment gateway; one job worker. Scaling path: Postgres, a lock per session instead of the global one, several workers on the `jobs` table, Redis pub/sub for the trace stream. The evidence check is a substring match backed by value validators. The token estimator is approximate (Gemini's counts are logged too). Single user.

**Trace excerpt** (live run, `traces/committed/live_timeout_after_success.jsonl`):
```
#26 untrusted_content_quarantined PLB-02 [ignore_instructions, fake_approval…] ← model saw summary only
#48 approval_granted  AP-0001 via=text fp=8abf3319       ← user's bare "yes"
#51 action_attempt_started key=bo-befb786d…              ← persisted before the side effect
#52 tool_call book(H-1001, bo-befb…) → timeout           ← provider committed, reply lost
#55 tool_call get_booking_status(bo-befb…) → booked      ← source of truth; no blind retry
#57 action_confirmed verified_via=after_timeout → "Booked and verified … exactly once"
```
