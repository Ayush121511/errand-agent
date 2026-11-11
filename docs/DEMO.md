# Demo script (about 5 minutes)

The brief asks for a 3–5 minute screen recording of one full booking, with the chat and the trace side by side, and rewards a failure handled live. This script books one plumber visit end to end and handles four failures live: a prompt injection in a review, a declined payment, a booking timeout after the provider had already booked, and a provider cancellation (plus, optionally, a bank that can't confirm a charge).

Every step below was dry-run on 11 Nov 2026 against the real model. Code-written messages (cards, checkout, booking receipts, proactive messages, refusals) come out word for word as quoted. Gemini's own replies vary a little in wording.

Each step has three parts:
- **Do**: what to click or type.
- **Say**: what to say while it runs (in your own words; these are suggestions).
- **Point at**: what to hover over or click in the trace.

## Before recording (10 minutes)

1. **Fresh state.** In a terminal, stop any running server (Ctrl+C), then:
   ```bash
   cd /Users/ayushsrivastava/Documents/Orange/errand-agent
   conda activate errand-agent
   rm -rf var traces/live
   uvicorn errand_agent.app.server:app --port 8000
   ```
   Do this again after every rehearsal: a rehearsal leaves bookings, payments and a data-theft flag behind.
2. **Browser.** Open http://localhost:8000 (no `?s=` in the URL, so you get a new chat). Make the window about 1400 px wide and zoom to 110% so the text reads on video. The chat sits left and the trace right. The top-right button should say **Hide trace**; if it says **Show trace**, click it.
3. **Trace scroll.** Scroll the trace panel to the bottom once. After that it follows new lines on its own. If you scroll up to show something, scroll back to the bottom before the next step.
4. **Second tab** for the end: `docs/EVALUATION_REPORT.md` open in VS Code's Markdown preview (or on GitHub).
5. **Quiet screen.** Turn on Do Not Disturb, close other tabs and hide the bookmarks bar.
6. **Recorder.** Loom (or QuickTime) on the browser window with your mic. Upload as unlisted.
7. **Timing.** Each Gemini turn takes 5–10 seconds. Keep talking through the wait. Code-only steps (payments, booking, proactive follow-up, refusals) are instant.
8. **Hold timer.** A held slot lasts 10 minutes. Go from the booking card to **Pay** without a long pause.

## The Demo ▾ menu (top right)

| Item | What it does |
|---|---|
| **Next booking attempt** | Forces the next `book()`: succeeds, times out after it went through, times out with nothing booked, or slot taken. Back to random after one use |
| **Provider cancels my latest booking** | The mock provider cancels this user's upcoming booking with the latest slot. The agent reacts within 3 s |
| **Next payment: reply lost after it went through** | The bank takes the next charge but the reply is lost. The retry uses the same key, so one charge |
| **Next payment: bank can't confirm the charge** | Authorisation works, then every capture times out. The payment stays open until a background job settles it |
| **Run background jobs now** | Runs queued reminders and payment checks immediately |
| **Money: payments and ledger ↗** | Opens `/api/admin/money`: payments, double-entry ledger, balances, jobs |
| **Flagged users ↗** / **Unblock this user** | Audit of data-theft attempts; clears the block |
| **Try saying** | Fills the message box with a ready example |

## Script

### 0:00–0:25 · What this is

**Do:** nothing. The greeting is on screen.

**Say:** "This is Errand, a chat agent that arranges home-service errands. Gemini 3.6 Flash reads what the customer wants and picks tools. But none of its 16 tools can book, cancel or charge anything. Code owns every side effect: approvals, exactly-once booking, payments, memory and limits. On the left is what the customer sees. On the right is the live trace of every step."

**Point at:** the greeting. "Even this greeting is written by code from live availability, with no model call. That's `greeting_built` in the trace, and it cost nothing."

### 0:25–1:05 · The request (the brief's first example)

**Do:** type and send:
> Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.

**Say while it runs (about 9 s):** "This is the first example from the brief. Watch the trace: every model call shows its tokens, latency and cost."

**Expect:** three options. In the dry run these were AquaCare Plumbers (₹1,250, 4.8★, tomorrow 9:00), FlowFix Plumbing (₹1,100, 4.4★, today 9:00) and Kumar Plumbing Works (₹850, 4.1★).

**Point at:**
- `context_built`: "About 2,900 of a 6,000-token budget. Kavya has 60 stored memories. Only 4 were sent, then 6 once the agent knew it was a plumbing job. The other 54 were left out: forgotten, superseded, unconfirmed guesses, or irrelevant, like her pet's name."
- `task_updated`: "Every field the model records needs a source: a quote from the user's own message. The model can't invent a budget."
- `search_providers`: "I never said where I live. HSR Layout came from memory. The ranking against budget and time window is done by code, not the model."

### 1:05–1:40 · Prompt injection, handled live

**Do:** type and send:
> What do reviewers say about AquaCare?

**Say while it runs (about 5 s):** "One of AquaCare's reviews is a planted attack. It pretends to be a system note, says the user has already approved a ₹4,999 booking, and tells the AI to call `book()` at once."

**Point at:**
- `model_call review_reader gemini-3.5-flash-lite`: "Reviews go to a second, cheaper model that has no tools at all."
- `untrusted_content_quarantined`: click it. "Review 1 is flagged: ignore-instructions, role spoof, fake approval. The main agent only sees the summary."
- The reply: "A normal summary. Nothing was booked and no price changed. The phase is still PRESENTING."

### 1:40–3:05 · One full booking, with a timeout after success

**Do:** open **Demo ▾**, set **Next booking attempt** to **times out after it went through**, click outside the menu to close it.

**Say:** "Now the hard case from the brief. I'm telling the mock provider that the next booking will go through, but its reply will time out."

**Do:** type and send:
> Book AquaCare at the earliest morning slot

**Expect:** a "Please confirm this booking" card: AquaCare Plumbers (PLB-02), Plumber — kitchen tap, Thu 12 Nov 9:00 AM, ₹1,250, slot held for 10 minutes.

**Say:** "This card isn't text from the model. Code draws it from the provider's held quote and fingerprints the provider, slot and price. If any of them changes, this approval is dead and I'd get a new card. Only a click or a plain 'yes' from me can approve it."

**Point at:** `hold_slot`, then `approval_requested` with its fingerprint.

**Do:** click **Confirm**. A test-mode checkout opens.

**Say:** "Confirming opens a checkout. The money is only authorised now. It's captured after the booking is verified, and voided if the booking fails."

**Do (optional, skip if running long):** choose **UPI · failure@test (always declined)** and click **Pay ₹1,250**.

**Expect:** "The payment didn't go through (declined by the bank (test method)). Nothing was charged or booked. Try another method in the checkout."

**Say:** "A declined payment books nothing, and the checkout stays open."

**Do:** choose **UPI · success@test** and click **Pay ₹1,250**.

**Expect:** "✓ Booked and verified … Booking ID: BK-0014 … Paid: ₹1,250 … The provider's system didn't answer in time, so I checked the booking status before telling you. It went through exactly once — no duplicate."

**Point at (read the trace top to bottom):**
1. `payment_authorized`, then `approval_consumed` with the same fingerprint: "the approval is used up, exactly once."
2. `action_attempt_started` with the `idempotency_key`: "written to the database *before* calling the provider, so even a crash here can be recovered."
3. `book → timeout`, then `outcome_ambiguous`: "we don't know if it booked."
4. `get_booking_status → booked`: "so we ask the source of truth instead of retrying blindly. It did book, so no retry."
5. `action_confirmed verified_via: after_timeout`, then `payment_captured`: "only now is the money taken."
6. `reminder_scheduled`: "and a reminder is queued for the evening before."

**Do:** type and send `yes`.

**Expect (instant):** "That's already done (booking BK-0014). I didn't repeat it, so there's no duplicate."

**Say:** "A repeated confirmation is caught by code. No model call, no second booking." **Point at:** `duplicate_confirmation_ignored`.

### 3:05–3:45 · Proactive follow-up: the provider cancels

**Say:** "Kavya also had an AC service booked from before, for Friday at 3 PM. Now the AC provider cancels it."

**Do:** open **Demo ▾** and click **Provider cancels my latest booking**. Don't type anything.

**Expect (within 3 s, unprompted):** "Heads-up: CoolCare AC Services cancelled your AC servicing booking for Fri 13 Nov, 3:00 PM (Technician unavailable). Nothing has been rebooked yet. Based on your saved preferences (HSR Layout, not before 09:00, under ₹1,800), the best replacement is Breeze Masters (rated 4.4). I've held the slot; it will only be booked if you confirm." Then a new card: Breeze Masters, Fri 13 Nov 2:00 PM, ₹1,550.

**Say:** "I didn't send anything. The agent picked up the provider's event, decided to notify her, found a replacement inside her saved preferences, held it, and asked for fresh approval. Nothing is rebooked until she confirms. This whole path is code, so it cost nothing."

**Point at:** `proactive_decision` (notify: true), then `search_providers` and `hold_slot` tagged `[proactive]`, then `approval_requested`.

### 3:45–4:20 · Payment the bank can't confirm (optional, for the backend asks)

**Do:** open **Demo ▾** and click **Next payment: bank can't confirm the charge**. Click **Confirm** on the Breeze Masters card, keep **UPI · success@test**, click **Pay ₹1,550**.

**Expect:** "✓ Booked and verified … Payment: authorised; the bank hasn't confirmed the charge yet. I'll check it and you'll never be charged twice."

**Point at:** three `payment_call capture → timeout`, then `payment_capture_unknown`. "The booking is real and verified. The charge is honestly reported as unconfirmed."

**Do:** open **Demo ▾** and click **Run background jobs now**.

**Expect:** "Update: the bank has now confirmed your payment of ₹1,550 (pay_0002). You were charged once." Two evening-before reminders also appear, each with her saved note for the technician ("Dog at home; keep the gate closed").

**Say:** "These come from a durable jobs table: compare-and-set claims, leases, retries with backoff. They survive restarts. The reconcile job settled the charge exactly once."

### 4:20–4:35 · The money (optional)

**Do:** **Demo ▾ → Money: payments and ledger ↗** (opens a new tab with JSON).

**Point at:** `balances`: `customer:user_1` −2,800 and `escrow` +2,800. "Double-entry ledger. Every movement and its status change is one database transaction. Providers are paid by a nightly job only after the provider reports the visit completed, plus a 24-hour dispute window." Close the tab.

### 4:35–5:00 · Evaluation and close

**Do:** switch to `docs/EVALUATION_REPORT.md`.

**Say:** "Everything you just saw is tested offline for free with one command, `python -m evals.pipeline`. 551 tests. 44 scenarios, each checked against 10 safety rules after every run: no booking without approval, no duplicates, no unverified claims, money matches bookings, forgotten memory never sent. 64 recorded live Gemini cases, replayed for free. A blind red team, and 19 real bugs written up with their regression tests."

**Close:** "The model proposes, code acts. Thanks for watching."

## If you run long

Cut in this order, and the run still fits the brief:
1. The declined payment.
2. The money page.
3. The bank-can't-confirm section. Instead, after the proactive card, just click **Confirm** and **Pay** to finish the replacement.

The shortest version (about 3:30) is: intro, request, injection, booking with timeout, repeated "yes", provider cancellation, evaluation.

## Extras (only if you have time, or for a second take)

- **Data theft, instant and free.** **Demo ▾ → Try saying → Data-theft attempt**, then send. Expect: "I can't help with that: I don't share internal system details, keys or other customers' information … Note: this request was logged; 2 more will pause this account." The trace shows `restricted_request_flagged` with no model call. Use **Unblock this user** if you push it to 3.
- **Another user.** Switch the user picker from Kavya Reddy to Rohan Mehta: the greeting moves to Indiranagar from 10:00, with his own memory and history.
- **Customer view.** Click **Hide trace** to show the plain customer layout, then **Show trace**.
- **Two errands in one message.** **Try saying → Two errands in one message**, then "options for both". Costs a few more model calls.

## If something goes wrong on camera

That's fine, and arguably better. Narrate what the trace shows: every exit path ends with a message that states the true state (booked, not booked, or "I can't tell yet"). If the model is slow, say "this is the model thinking; everything after it is code."
