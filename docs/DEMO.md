# Demo script (about 5 minutes)

A printable copy to keep open while recording: [DEMO.pdf](DEMO.pdf).

Ten short steps. Each one says what to **do** and what to **say**. The lines are short so you can say them in your own words. The tag after each title is the feature it shows.

It covers the AI asks (context management, harness, evals) and three backend asks (payments, task automations, job scheduler), and it says that every transaction is atomic.

## Setup (before you hit record)

1. Fresh server (stop any old one first with Ctrl+C):
   ```bash
   cd /Users/ayushsrivastava/Documents/Orange/errand-agent && conda activate errand-agent
   rm -rf var traces/live && uvicorn errand_agent.app.server:app --port 8000
   ```
2. Open http://localhost:8000 (no `?s=` in the URL). Click **Hide trace** (top right).
3. Start recording.

After a rehearsal, run step 1 again for a clean start.

---

## 1 · Pitch · 30 s · *the product*

**Do:** nothing. The customer view shows a greeting with the next free slot for every service.

**Say:** "Booking a plumber or an AC service still means calls and WhatsApp back-and-forth. Errand does it in one chat. The hard part is trusting an AI with someone's time and money. So the AI only understands; code does everything that matters. Nothing is booked or charged without your OK. Every booking happens exactly once. And every transaction is atomic: it fully happens, or not at all."

## 2 · Under the hood · 15 s · *harness, observability*

**Do:** click **Show trace**, then scroll the trace to the bottom.

**Say:** "On the right is the live trace: every model call, tool call, token and cost. Gemini picks from 16 tools, and none of them can book, cancel or charge. Even this greeting was written by code, with no model call."

## 3 · Ask for a plumber · 40 s · *context management, memory*

**Do:** type:
> Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.

**Say (while it thinks):** "This is the first example from the brief."

**Point at** `context_built`: "Kavya has 60 saved memories and a long chat history. Only the few that matter reach the model, under a fixed token cap, and what was sent is logged on every call."

**Point at** `search_providers`: "I never said my area. HSR Layout came from memory. Code, not the model, ranks the options against my budget and time."

## 4 · Prompt injection · 25 s · *safety*

**Do:** type:
> What do reviewers say about AquaCare?

**Point at** `untrusted_content_quarantined`: "One review is a planted attack: it tells the AI the user already approved, and to book at once. Reviews are read by a separate model with no tools, so nothing happens."

## 5 · Book, with a timeout · 60 s · *approvals, exactly-once, payments*

**Do:** **Demo ▾ → Next booking attempt → times out after it went through**. Click outside the menu. (It works for one booking; then the menu shows random again.) Type:
> Book AquaCare at the earliest morning slot

**Say (at the card):** "This card is drawn by code, not the model, from the provider's held price. If the price or slot changes, I get a new card. Only my click can approve it."

**Do:** click **Confirm**.

**Say (at the checkout):** "The money is only held now. It's charged after the booking is verified."

**Do (optional):** pick **UPI · failure@test** and click **Pay**. **Say:** "A failed payment books nothing."

**Do:** pick **UPI · success@test** and click **Pay ₹1,250**.

**Point at** `book → timeout`, then `get_booking_status → booked`: "The provider timed out after booking. Code checks the real status instead of retrying, so it's booked exactly once, then charged. Each step is one atomic transaction."

**Do:** type `yes`.

**Say:** "Saying yes again is caught by code. No model call, no second booking."

## 6 · Provider cancels · 25 s · *task automations*

**Do:** **Demo ▾ → Provider cancels my latest booking**. Wait 3 seconds.

**Say:** "Kavya's AC provider just cancelled her Friday booking. Nobody typed anything. The agent told her, found a replacement within her saved preferences, and asks before rebooking."

## 7 · Bank can't confirm · 35 s · *job scheduler*

**Do:** **Demo ▾ → Next payment: bank can't confirm the charge**. Click **Confirm** on the new card, then **Pay**.

**Say:** "This time the bank didn't confirm the charge. The booking is real, and the agent says so honestly."

**Do:** **Demo ▾ → Run background jobs now**.

**Say:** "A background job from our scheduler settles it: charged once. The same scheduler sends a reminder the evening before each visit, with Kavya's saved note for the technician."

## 8 · Memory she controls · 30 s · *memory*

**Do:** type:
> What do you remember about me?

**Say:** "She can see everything we keep, in plain words. Things she told us are kept apart from things we guessed from history, and a guess is only used after she confirms it."

**Do:** type:
> Forget my AC budget

**Say:** "And she can forget anything. A forgotten fact never reaches the model again, and our tests check that on every run."

## 9 · Rapid fire · 20 s · *security, multiple users*

**Do:** **Demo ▾ → Try saying → Data-theft attempt**, then send.

**Say:** "Asking for the system prompt or keys is refused by code before the AI sees it. It's logged, and three tries pause the account."

**Do:** switch the user picker (top right) from **Kavya Reddy** to **Rohan Mehta**.

**Say:** "Every customer has their own world. Rohan lives in Indiranagar and starts at 10, so his greeting is different."

## 10 · Close · 20 s · *evals*

**Say:** "All of this is tested offline with one command: 552 tests, 44 scenarios each checked against 10 safety rules, and 64 recorded live Gemini cases. A conversation costs about five cents. The AI understands, code acts, and every transaction is atomic. Thanks."

---

**If something goes wrong:** read the trace out loud. Every path ends with a message that tells the truth (booked, not booked, or not sure yet).

**Running long?** Skip the failed payment in step 5, then step 4, then step 9.
