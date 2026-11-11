# Demo script (about 5 minutes)

The brief asks for a 3–5 minute screen recording of one full booking, with the chat and the trace side by side, and rewards a failure handled live. This script opens with a short pitch in the customer view, then books one plumber visit end to end and handles live failures: a prompt injection in a review, a booking timeout after the provider had already booked, a provider cancellation and a bank that can't confirm a charge.

It also shows each of the asks we were given, by name: all three AI asks and three of the five backend asks, with every transaction atomic.

Every step below was dry-run on 11 Nov 2026 against the real model. Code-written messages (cards, checkout, booking receipts, proactive messages, refusals) come out word for word as quoted. Gemini's own replies vary a little in wording.

Each step has three parts:
- **Do**: what to click or type.
- **Say**: what to say while it runs (in your own words; these are suggestions).
- **Point at**: what to hover over or click in the trace.

## What the demo proves

| Ask | Where it shows | What to say |
|---|---|---|
| **AI · Context management** | 1:05 request (`context_built`) | 60 memories, a hard 6,000-token cap, only relevant current facts sent, and the sent context logged every turn |
| **AI · Harness** | 0:50 under the hood, 1:40 injection, 2:10 booking | The model only proposes. Code enforces approvals, limits, exactly-once booking, and review quarantine |
| **AI · Evals** | 4:35 close | One offline command: 551 tests, 44 scenarios × 10 safety rules, 64 recorded live cases, red team, judge |
| **Backend · Payment systems** | 2:10 booking, 4:20 money page | Authorise on confirm, capture only after a verified booking, void or refund otherwise, double-entry ledger |
| **Backend · Task automations** | 3:20 provider cancels, 3:50 reminders | The agent reacts to provider events with no prompt; reminders and payment follow-ups go out by themselves |
| **Backend · Job schedulers** | 3:50 Run background jobs | A durable `jobs` table: compare-and-set claims, leases, backoff, dedupe; reconciliation, reminders, nightly payouts |
| **Atomic transactions** | 0:00 pitch, 2:10 booking, 4:20 money page | Each money movement and its status change commit together or not at all; the database refuses a second capture or payout |
| Backend · Real-time traffic, scalable infra | 4:35 close (one line) | Not built; a documented path: Postgres, per-session locks, more workers, Redis for the trace stream |

## Before recording (10 minutes)

1. **Fresh state.** In a terminal, stop any running server (Ctrl+C), then:
   ```bash
   cd /Users/ayushsrivastava/Documents/Orange/errand-agent
   conda activate errand-agent
   rm -rf var traces/live
   uvicorn errand_agent.app.server:app --port 8000
   ```
   Do this again after every rehearsal: a rehearsal leaves bookings, payments and a data-theft flag behind.
2. **Browser.** Open http://localhost:8000 (no `?s=` in the URL, so you get a new chat). Make the window about 1400 px wide and zoom to 110% so the text reads on video.
3. **Start in the customer view.** Click **Hide trace** (top right) so the button says **Show trace**. You open the trace at 0:50, after the pitch.
4. **Trace scroll.** When you open the trace at 0:50, scroll the trace panel to the bottom once. After that it follows new lines on its own. If you scroll up to show something, scroll back to the bottom before the next step.
5. **Second tab** for the end: `docs/EVALUATION_REPORT.md` open in VS Code's Markdown preview (or on GitHub).
6. **Quiet screen.** Turn on Do Not Disturb, close other tabs and hide the bookmarks bar.
7. **Recorder.** Loom (or QuickTime) on the browser window with your mic. Upload as unlisted.
8. **Timing.** Each Gemini turn takes 5–10 seconds. Keep talking through the wait. Code-only steps (payments, booking, proactive follow-up, refusals) are instant.
9. **Hold timer.** A held slot lasts 10 minutes. Go from the booking card to **Pay** without a long pause.

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

### 0:00–0:50 · The pitch (customer view, trace hidden)

**Do:** nothing. The customer view is on screen: the greeting with the next free slot for each service.

**Say (about 125 words):**
"Booking a plumber or an AC service in Bangalore still means calls, WhatsApp back-and-forth, and hoping someone turns up. Errand turns that into one message: it finds the right provider at the right price, and books it when you say yes.

The hard part is trusting an AI with someone's time and money. So the AI understands people, and code does everything that matters. Nothing is booked or charged without the customer's OK. Every booking happens exactly once. And every transaction is atomic: it fully happens, or not at all.

Today: one real booking, live failures, and how we prove it works. On the AI side it covers context management, the harness and evals; on the backend, payments, task automations and a job scheduler."

### 0:50–1:05 · Under the hood · *AI: harness*

**Do:** click **Show trace**. Scroll the trace panel to the bottom.

**Say:** "Underneath, Gemini picks from 16 tools, and none of them can book, cancel or charge. That's the harness: the model proposes, code acts. On the right, the live trace shows every model call, tool call, token and cost."

**Point at (if time):** `greeting_built`: "even the greeting is written by code, with no model call."

### 1:05–1:40 · The request · *AI: context management*

**Do:** type and send:
> Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.

**Say while it runs (about 9 s):** "This is the first example from the brief."

**Expect:** three options. In the dry run these were AquaCare Plumbers (₹1,250, 4.8★, tomorrow 9:00), FlowFix Plumbing (₹1,100, 4.4★, today 9:00) and Kumar Plumbing Works (₹850, 4.1★).

**Point at:**
- `context_built`: "Context management. Kavya has 60 stored memories and a long chat history. The model gets a hard cap of 6,000 tokens. Only 4 memories were sent, then 6 once it knew this was a plumbing job. The rest were left out: forgotten, superseded, unconfirmed guesses, or irrelevant, like her pet's name. What was sent is logged on every call."
- `search_providers`: "I never said where I live. HSR Layout came from memory. Code, not the model, ranks the options against budget and time."
- `task_updated` (if time): "Every field needs a quote from the customer's own words. The model can't invent a budget."

### 1:40–2:10 · Prompt injection, handled live · *AI: harness*

**Do:** type and send:
> What do reviewers say about AquaCare?

**Say while it runs (about 5 s):** "One of AquaCare's reviews is a planted attack. It pretends to be a system note, says the user already approved a ₹4,999 booking, and tells the AI to book at once."

**Point at:**
- `model_call review_reader gemini-3.5-flash-lite`: "Reviews go to a second, cheaper model that has no tools at all."
- `untrusted_content_quarantined`: click it. "Review 1 is flagged: ignore-instructions, role spoof, fake approval. The main agent only sees a clean summary. Nothing was booked."

### 2:10–3:20 · One full booking, with a timeout after success · *Harness, payments, atomic*

**Do:** open **Demo ▾**, set **Next booking attempt** to **times out after it went through**, click outside the menu to close it.

**Say:** "Now the hard case. I'm telling the mock provider that the next booking will go through, but its reply will time out."

**Do:** type and send:
> Book AquaCare at the earliest morning slot

**Expect:** a "Please confirm this booking" card: AquaCare Plumbers (PLB-02), Plumber — kitchen tap, Thu 12 Nov 9:00 AM, ₹1,250, slot held for 10 minutes.

**Say:** "This card isn't model text. Code draws it from the provider's held quote and fingerprints provider, slot and price. If any of them changes, the approval is dead."

**Do:** click **Confirm**. A test-mode checkout opens.

**Say:** "Payments: the money is only authorised now. It's captured after the booking is verified, and released if the booking fails."

**Do (optional, only if you're under time):** choose **UPI · failure@test (always declined)** and click **Pay ₹1,250**. Expect: "The payment didn't go through … Nothing was charged or booked." Say: "A declined payment books nothing."

**Do:** choose **UPI · success@test** and click **Pay ₹1,250**.

**Expect:** "✓ Booked and verified … Booking ID: BK-0014 … Paid: ₹1,250 … The provider's system didn't answer in time, so I checked the booking status before telling you. It went through exactly once — no duplicate."

**Point at (read the trace top to bottom):**
1. `approval_consumed` and `action_attempt_started`: "These two are one atomic transaction. The approval is used up and the attempt, with its idempotency key, is recorded before we call the provider. Both happen or neither does."
2. `book → timeout`, then `get_booking_status → booked`: "The provider timed out. Instead of retrying blindly, code asks the source of truth. It did book, so no retry and no duplicate."
3. `payment_captured`: "Only now is the money taken. The capture, its ledger lines and the status change commit as one transaction."

**Do:** type and send `yes`.

**Expect (instant):** "That's already done (booking BK-0014). I didn't repeat it, so there's no duplicate."

**Say:** "A repeated yes is caught by code: no model call, no second booking."

### 3:20–3:50 · The provider cancels · *Backend: task automations*

**Say:** "Kavya also had an AC service booked for Friday. Now the AC provider cancels it."

**Do:** open **Demo ▾** and click **Provider cancels my latest booking**. Don't type anything.

**Expect (within 3 s, unprompted):** "Heads-up: CoolCare AC Services cancelled your AC servicing booking for Fri 13 Nov, 3:00 PM (Technician unavailable). Nothing has been rebooked yet. Based on your saved preferences (HSR Layout, not before 09:00, under ₹1,800), the best replacement is Breeze Masters (rated 4.4). I've held the slot; it will only be booked if you confirm." Then a new card: Breeze Masters, Fri 13 Nov 2:00 PM, ₹1,550.

**Say:** "Task automation. Nobody typed anything. The agent caught the provider's event, told her, found a replacement inside her saved preferences, held it, and asked again. Nothing is rebooked until she confirms. This path is all code, so it cost nothing."

**Point at:** `proactive_decision`, then `search_providers` and `hold_slot` tagged `[proactive]`.

### 3:50–4:20 · A bank that can't confirm · *Backend: job scheduler*

**Do:** open **Demo ▾** and click **Next payment: bank can't confirm the charge**. Click **Confirm** on the Breeze Masters card, keep **UPI · success@test**, click **Pay ₹1,550**.

**Expect:** "✓ Booked and verified … Payment: authorised; the bank hasn't confirmed the charge yet. I'll check it and you'll never be charged twice."

**Say:** "The booking is real. The bank didn't answer, and the customer is told the truth."

**Do:** open **Demo ▾** and click **Run background jobs now**.

**Expect:** "Update: the bank has now confirmed your payment of ₹1,550 (pay_0002). You were charged once." Two evening-before visit reminders also appear, with her saved note for the technician ("Dog at home; keep the gate closed").

**Say:** "That's the job scheduler. Jobs live in a database table, so they survive restarts. Workers claim them with compare-and-set and a lease, and failures retry with backoff. It settled the payment exactly once and sent the reminders. A nightly job pays providers only after the visit is confirmed complete."

### 4:20–4:35 · The money · *Payments, atomic*

**Do:** **Demo ▾ → Money: payments and ledger ↗** (opens a new tab with JSON).

**Point at:** `balances`: `customer:user_1` −2,800 and `escrow` +2,800.

**Say:** "Double-entry ledger: customer to escrow, then escrow to provider after the visit. Every transaction is atomic. The database itself refuses a second capture or payout, and we crash-test every multi-step write: with transactions turned off, 9 of those 12 tests fail."

**Do:** close the tab.

### 4:35–5:00 · Evals and close · *AI: evals*

**Do:** switch to `docs/EVALUATION_REPORT.md`.

**Say:** "And evals. Everything you saw is tested offline, for free, with one command: 551 tests, 44 scenarios each checked against 10 safety rules, and 64 recorded live Gemini cases. Real-time traffic and scaling the infra are designed, not built. So: the AI understands, code acts, and every transaction is atomic. Thanks for watching."

## If you run long

Cut in this order:
1. The declined payment (already optional).
2. The `task_updated` point in the request step.
3. The money tab. Say its atomic line while `payment_captured` is on screen instead.

Keep the job-scheduler step: it is the only live proof of that ask. The shortest version (about 4:00) is pitch, under the hood, request, injection, booking, provider cancels, bank and jobs, close.

## Extras (only if you have time, or for a second take)

- **Data theft, instant and free.** **Demo ▾ → Try saying → Data-theft attempt**, then send. Expect: "I can't help with that: I don't share internal system details, keys or other customers' information … Note: this request was logged; 2 more will pause this account." The trace shows `restricted_request_flagged` with no model call. Use **Unblock this user** if you push it to 3.
- **Another user.** Switch the user picker from Kavya Reddy to Rohan Mehta: the greeting moves to Indiranagar from 10:00, with his own memory and history.
- **Two errands in one message.** **Try saying → Two errands in one message**, then "options for both". Costs a few more model calls.

## If something goes wrong on camera

That's fine, and arguably better. Narrate what the trace shows: every exit path ends with a message that states the true state (booked, not booked, or "I can't tell yet"). If the model is slow, say "this is the model thinking; everything after it is code."
