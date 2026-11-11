# Demo cheat sheet (about 4 minutes)

Six steps. Each one says what to **do** and what to **say**. The lines are short so you can say them in your own words.

It covers the AI asks (context management, harness, evals) and three backend asks (payments, task automations, job scheduler), and it says that every transaction is atomic.

## Setup (before you hit record)

1. In a terminal (stop any old server first with Ctrl+C):
   ```bash
   cd /Users/ayushsrivastava/Documents/Orange/errand-agent && conda activate errand-agent
   rm -rf var traces/live && uvicorn errand_agent.app.server:app --port 8000
   ```
2. Open http://localhost:8000. Click **Hide trace** (top right).
3. Start recording.

After a rehearsal, run step 1 again for a clean start.

---

## 1 · Pitch · 30 s

**Do:** nothing.

**Say:** "Booking a plumber or an AC service still means calls and WhatsApp back-and-forth. Errand does it in one chat. The hard part is trusting an AI with someone's time and money. So the AI only understands; code does everything that matters. Nothing is booked or charged without your OK. Every booking happens exactly once. And every transaction is atomic: it fully happens, or not at all."

## 2 · Ask for a plumber · 40 s · *context management*

**Do:** click **Show trace**, then scroll the trace to the bottom. Type:
> Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.

**Say (while it thinks):** "On the right is the live trace: every model call, tool, token and cost."

**Point at** `context_built`: "Kavya has 60 saved memories. Only the few that matter reach the model, under a fixed token cap. I never said my area; it came from memory."

## 3 · Prompt injection · 25 s · *harness*

**Do:** type:
> What do reviewers say about AquaCare?

**Point at** `untrusted_content_quarantined`: "One review tells the AI to book without asking. Reviews are read by a separate model with no tools, so nothing happens."

## 4 · Book, with a timeout · 60 s · *harness, payments*

**Do:** **Demo ▾ → Next booking attempt → times out after it went through**. Click outside the menu. (It works for one booking; then the menu shows random again.) Type:
> Book AquaCare at the earliest morning slot

**Say (at the card):** "This card is drawn by code, not the model. Only my click can approve it."

**Do:** click **Confirm**, then **Pay ₹1,250**.

**Say (at the checkout):** "The money is only held now. It's charged after the booking is verified."

**Point at** `book → timeout`, then `get_booking_status → booked`: "The provider timed out after booking. Code checks the real status instead of retrying, so it's booked exactly once, then charged. Each step is one atomic transaction."

## 5 · Provider cancels · 50 s · *task automations, job scheduler*

**Do:** **Demo ▾ → Provider cancels my latest booking**. Wait 3 seconds.

**Say:** "The AC provider just cancelled Kavya's Friday booking. Nobody typed anything. The agent told her, found a replacement within her preferences, and asks before rebooking."

**Do:** **Demo ▾ → Next payment: bank can't confirm the charge**. Click **Confirm**, then **Pay**. Then **Demo ▾ → Run background jobs now**.

**Say:** "This time the bank didn't confirm the charge, and the agent says so honestly. A background job from our scheduler settles it: charged once. The same scheduler sends the visit reminders."

## 6 · Close · 20 s · *evals*

**Say:** "All of this is tested offline with one command: 552 tests, 44 scenarios, 64 recorded live cases. The AI understands, code acts. Thanks."

---

**If something goes wrong:** read the trace out loud. Every path ends with a message that tells the truth (booked, not booked, or not sure yet).

**Running long?** Skip step 3.
