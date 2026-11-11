# Errand Agent: design note

**Shape.** One tool-using model (`gemini-3.6-flash`) inside deterministic code: the model reads the user, calls read-only tools and explains options; code owns state, approvals, side effects, memory and limits. A second, tool-less model (`gemini-3.5-flash-lite`) reads reviews in quarantine.

**State model.** A `Session{phase, task, errands}` per chat and user, saved to SQLite after every step. The phase machine `IDLE → GATHERING → SEARCHING → PRESENTING → HOLDING → AWAITING_APPROVAL → EXECUTING → VERIFYING → DONE | FAILED` checks every transition against an allow-list and traces it. Each task field keeps its value and source (user message, memory, booking history), so a correction changes one field; code computes missing fields, `ask_user` refuses known ones, and code ranks providers against budget, time and avoid rules. A second errand in the same chat is parked as a short summary and comes up after the current booking. Provider calls act as the chat's user, like an auth token.

**Approval enforcement.** The model cannot book, cancel or reschedule; it can only `propose_*`. Code renders the card from the *held* quote with fingerprint `hash(action, user, provider, slot, price)`. Only the user approves: the button (approval id + fingerprint) or a reply made only of confirming words ("yes but 11am" is a change). Before acting, code recomputes the fingerprint and moves the approval from approved to consumed in one compare-and-set, so it works once; a changed provider, slot or price needs a fresh card. A correction the held slot still meets keeps the card.

**Idempotency and recovery.** Code writes an attempt row with `key = hash(approval, hold)` *before* calling `book`, in the same transaction that consumes the approval. On a timeout, 503 or malformed reply, code asks `get_booking_status`: booked → confirm; not booked → retry with the same key (max 3); can't tell → report "unknown", never success. A repeated confirmation replays the stored result; `recover()` resolves pending attempts at start-up. Model text claiming an unverified action is replaced. Payments follow the same saga: authorise on confirm, capture only after the booking is verified, void or refund otherwise, double-entry ledger. Inside our database every multi-row change is one transaction (`AgentStore.tx()`): approval use + attempt, ledger rows + payment status, memory insert + supersede, job writes + done mark, provider event + its card/refund/message. Across the provider and the bank, idempotency keys, status reads and reconcile jobs.

**Untrusted output.** Raw reviews never reach the main model; the reader returns a schema-checked summary. Even a fooled model can't act: facts need a quote from a *user* message, search arguments must match state, side effects need an approval. Requests for restricted data and gibberish are screened by code before any model call and never enter a context.

| Source | May change |
|---|---|
| User message | task fields and memories (with a quote) |
| Approval | one fingerprinted action |
| Tool structured data | candidates, prices, slots (validated) |
| Tool free text, model output | nothing, or wording and proposals only |

**Memory and context.** Memories are *explicit* (quoted) or *inferred* (unused until confirmed), and active, superseded or forgotten; users can list, correct and forget them in plain words. One gate, `select_for_context`, drops forgotten, superseded, unconfirmed and irrelevant facts and keeps the current service's. Code also applies remembered budget and times before ranking. The context (tool schemas, prompt, STATE, MEMORY, session, last 6 earlier messages) is capped at 6,000 tokens against Gemini's own count; if the fixed part leaves under 250 tokens for the chat, code drops memory, then trims STATE. Every call logs a manifest of what was sent and why each memory was excluded; tests assert on it.

**Bounds and observability.** Per run: 8 model calls, 12 tool calls, 45 s; executors cap their own retries; every exit states the true state. Traces are JSONL per session: transitions, context manifests, model calls (tokens, latency, cost), tool calls (arguments, sanitised result, latency, attempt), approval, memory and payment events, streamed to the UI.

**Trade-offs.** A hand-written phase machine instead of a framework (every transition explicit and testable); an in-process mock and a mock payment gateway; polling instead of webhooks; one job worker on a `jobs` table with compare-and-set claims. Scaling path: Postgres, per-session locks, more workers, pub/sub for traces. Evidence checks are substring matches backed by validators; the token estimate is calibrated, not exact.

**Trace excerpt** (live run, `traces/committed/live_timeout_after_success.jsonl`):
```
#26 untrusted_content_quarantined PLB-02 [ignore_instructions, fake_approval…] ← model saw summary only
#48 approval_granted  AP-0001 via=text fp=8abf3319       ← user's bare "yes"
#51 action_attempt_started key=bo-befb786d…              ← persisted before the side effect
#52 tool_call book(H-1001, bo-befb…) → timeout           ← provider committed, reply lost
#55 tool_call get_booking_status(bo-befb…) → booked      ← source of truth; no blind retry
#57 action_confirmed verified_via=after_timeout → "Booked and verified … exactly once"
```
