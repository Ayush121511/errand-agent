"""Seed data for context-management tests and the demo: 60 memories + a long prior chat.

The memory mix is deliberate, so the context gate has something to filter:
- relevant & current (explicit): area, earliest time, per-service budgets, preferred /
  avoided providers, language, access notes
- superseded old values (moved house, changed earliest time)
- forgotten facts (gate code, an old budget, a language)
- inferred-but-unconfirmed preferences (from booking history)
- facts that are true but irrelevant to booking a visit (cricket team, cuisine, ...)
"""

from __future__ import annotations

from datetime import timedelta

from errand_agent.core.clock import Clock
from errand_agent.core.models import User
from errand_agent.core.store import AgentStore
from errand_agent.memory.store import MemoryStore

PRIOR_SESSION = "S-prior"

# (key, value, kind, status, scope, evidence)
MEMORIES: list[tuple[str, str, str, str, str | None, str | None]] = [
    # --- relevant, current, explicit
    ("area", "HSR Layout", "explicit", "active", None, "I live in HSR Layout now"),
    ("earliest_time", "09:00", "explicit", "active", None, "never before 9 am please"),
    ("budget_max", "1500", "explicit", "active", "plumber", "keep plumbing under 1500"),
    ("budget_max", "1800", "explicit", "active", "ac_servicing", "AC service max 1800"),
    ("budget_max", "3000", "explicit", "active", "house_cleaning", "cleaning under 3k"),
    ("budget_max", "2500", "explicit", "active", "pest_control", "pest control up to 2500"),
    ("preferred_provider", "ELE-01", "explicit", "active", "electrician",
     "Ramesh from Spark Electricals is great"),
    ("preferred_provider", "CRP-01", "explicit", "active", "carpenter", "WoodWise did a good job"),
    ("avoid_provider", "PLB-05", "explicit", "active", "plumber", "never send QuickDrip again"),
    ("avoid_provider", "ELE-04", "explicit", "active", "electrician", "Shakti Electric was rude"),
    ("language", "Hindi or English", "explicit", "active", None, "Hindi or English is fine"),
    ("access_notes", "Dog at home; keep the gate closed", "explicit", "active", None,
     "we have a dog, keep the gate closed"),
    # --- superseded (old values replaced by corrections)
    ("area", "Indiranagar", "explicit", "superseded", None, "I'm in Indiranagar"),
    ("area", "Koramangala", "explicit", "superseded", None, "moved to Koramangala"),
    ("earliest_time", "08:00", "explicit", "superseded", None, "8 am is ok"),
    ("budget_max", "1200", "explicit", "superseded", "plumber", "plumber under 1200"),
    ("preferred_provider", "ELE-02", "explicit", "superseded", "electrician", "BrightLine is ok"),
    # --- forgotten (user asked to forget; must never reach the model)
    ("access_notes", "Gate code 4521, call on arrival", "explicit", "forgotten", None,
     "gate code is 4521"),
    ("language", "Kannada", "explicit", "forgotten", None, "Kannada please"),
    ("budget_max", "999", "explicit", "forgotten", "electrician", "electrician under 999"),
    ("preferred_provider", "ACS-04", "explicit", "forgotten", "ac_servicing", "Frost Fix is fine"),
    # --- inferred, unconfirmed (must not reach the model until confirmed)
    ("preferred_time_of_day", "morning", "inferred", "active", None,
     "3 past bookings all before noon"),
    ("latest_time", "19:00", "inferred", "active", None, "no booking after 7 pm so far"),
    ("preferred_provider", "ACS-01", "inferred", "active", "ac_servicing",
     "booked CoolCare twice"),
    ("budget_max", "1300", "inferred", "active", "carpenter", "average carpenter spend"),
    # --- true but irrelevant to arranging a visit
    ("favourite_cuisine", "South Indian", "explicit", "active", None, None),
    ("cricket_team", "RCB", "explicit", "active", None, None),
    ("birthday_month", "March", "explicit", "active", None, None),
    ("car_model", "Tata Nexon EV", "explicit", "active", None, None),
    ("gym_time", "06:30", "explicit", "active", None, None),
    ("newsletter", "opted out", "explicit", "active", None, None),
    ("pet_name", "Bruno", "explicit", "active", None, None),
    ("coffee", "filter coffee, no sugar", "explicit", "active", None, None),
    ("work_mode", "hybrid, office Tue/Thu", "explicit", "active", None, None),
    ("office_area", "Bellandur", "explicit", "active", None, None),
    ("kids_school_time", "07:45", "explicit", "active", None, None),
    ("movie_genre", "thrillers", "explicit", "active", None, None),
    ("music", "AR Rahman", "explicit", "active", None, None),
    ("phone_os", "Android", "explicit", "active", None, None),
    ("wifi_provider", "ACT Fibernet", "explicit", "active", None, None),
    ("weekend_plans", "usually out on Sundays", "inferred", "active", None, None),
    ("shopping", "prefers UPI", "inferred", "active", None, None),
    ("diet", "vegetarian", "explicit", "active", None, None),
    ("anniversary_month", "December", "explicit", "active", None, None),
    ("hometown", "Lucknow", "explicit", "active", None, None),
    ("vehicle_service", "Tata service centre, Hosur Road", "explicit", "active", None, None),
    ("plants", "balcony garden", "explicit", "active", None, None),
    ("reading", "non-fiction", "explicit", "active", None, None),
    ("travel", "Goa in January", "explicit", "active", None, None),
    ("tea", "masala chai", "explicit", "active", None, None),
    ("streaming", "Netflix", "explicit", "active", None, None),
    ("bank", "HDFC", "explicit", "active", None, None),
    ("laptop", "MacBook Air", "explicit", "active", None, None),
    ("football_team", "Bengaluru FC", "explicit", "active", None, None),
    ("doctor", "Dr. Rao, Narayana Health", "explicit", "active", None, None),
    ("gym", "Cult, HSR", "explicit", "active", None, None),
    ("cook_timing", "cook comes 8-9 am", "explicit", "forgotten", None, None),
    ("maid_days", "Mon-Sat", "explicit", "superseded", None, None),
    ("water_supply", "BWSSB, tanker backup", "explicit", "active", None, None),
    ("building", "Prestige apartments, block C", "explicit", "active", None, None),
]

_TOPICS = [
    ("Can you find me an electrician for a fan that's making noise?",
     "Sure. Spark Electricals has a 4.7 rating and a slot tomorrow at 11 AM for ₹600."),
    ("Ramesh from Spark Electricals is great, use him next time too.",
     "Noted — I'll prefer Ramesh (Spark Electricals) for electrical work."),
    ("I moved from Koramangala to HSR Layout last month.",
     "Got it, I'll search around HSR Layout from now on."),
    ("Please forget my gate code, I don't want it stored.",
     "Done — I've forgotten the gate code."),
    ("never send QuickDrip again, they were 2 hours late",
     "Understood, I'll avoid QuickDrip Services for plumbing."),
    ("What did the AC service cost last time?",
     "Your last AC service with CoolCare was ₹1,400."),
    ("Is there anyone for deep cleaning on Saturday?",
     "Sparkle Homes and Urban Dusters both have Saturday slots under ₹3,000."),
    ("Not this week, maybe later.", "No problem, just ask when you're ready."),
    ("RCB lost again yesterday, sigh.", "Ouch! Hopefully next season goes better."),
    ("Can a carpenter fix a wardrobe hinge?",
     "Yes — WoodWise and HandyWood both handle hinges and locks."),
]


def seed_user(store: AgentStore, memory: MemoryStore, clock: Clock, user_id: str = "user_1",
              conversation_turns: int = 110) -> None:
    """Insert the 60 memories and a long prior conversation for `user_id`."""
    base = clock.now() - timedelta(days=90)
    for i, (key, value, kind, status, scope, evidence) in enumerate(MEMORIES):
        ts = (base + timedelta(days=i)).isoformat()
        # Rows are inserted with their final status, so superseded/forgotten history does
        # not displace the active values.
        memory.add(user_id, key, value, kind, evidence=evidence, scope=scope,  # type: ignore[arg-type]
                   status=status, created_at=ts)  # type: ignore[arg-type]
    t0 = clock.now() - timedelta(days=60)
    for i in range(conversation_turns):
        user, agent = _TOPICS[i % len(_TOPICS)]
        ts = (t0 + timedelta(hours=8 * i)).isoformat()
        store.add_message(PRIOR_SESSION, user_id, "user", f"{user} (#{i + 1})", ts)
        store.add_message(PRIOR_SESSION, user_id, "assistant", agent, ts)


# ------------------------------------------------------------------ users
# Three demo customers. user_1 keeps the 60 memories and the long prior chat above (the
# context-management test bed); the others are lighter, with their own area, preferences
# and booking history, so the same request gives different, personal answers per user.
# Phone numbers are fictional.

PROFILES = [
    User(user_id="user_1", name="Ananya Rao", phone="+91 98450 12345", home_area="HSR Layout",
         address="Flat 4B, Lotus Residency, 27th Main", landmark="near BDA Complex",
         created_at="2026-05-02T10:00:00+05:30"),
    User(user_id="user_2", name="Rohan Mehta", phone="+91 99001 23456",
         home_area="Indiranagar", address="12, 5th Cross, Defence Colony",
         landmark="behind Indiranagar Club", created_at="2026-07-18T18:30:00+05:30"),
    User(user_id="user_3", name="Priya Nair", phone="+91 98861 34567", home_area="Whitefield",
         address="Villa 21, Palm Meadows", landmark="off Varthur Road",
         created_at="2026-09-09T09:15:00+05:30"),
]

# (key, value, scope, evidence): explicit, active
_USER_MEMORIES: dict[str, list[tuple[str, str, str | None, str]]] = {
    "user_2": [
        ("area", "Indiranagar", None, "I'm in Indiranagar"),
        ("earliest_time", "10:00", None, "nothing before 10 am, I work late"),
        ("budget_max", "1000", "electrician", "keep electrician visits under 1000"),
        ("budget_max", "3000", "house_cleaning", "cleaning up to 3000 is fine"),
        ("preferred_provider", "ELE-04", "electrician", "Shakti Electric did a great job"),
        ("language", "English", None, "English please"),
    ],
    "user_3": [
        ("area", "Whitefield", None, "we live in Whitefield"),
        ("earliest_time", "08:00", None, "8 am onwards works"),
        ("latest_time", "18:00", None, "nobody home after 6 pm"),
        ("budget_max", "2000", "ac_servicing", "AC service under 2000"),
        ("avoid_provider", "PST-01", "pest_control", "never send BugBusters again"),
        ("access_notes", "Gate pass needed at the security desk", None,
         "tell them to collect a gate pass at security"),
    ],
}

# (provider, technician, days from now, hour, status)
_USER_HISTORY: dict[str, list[tuple[str, str, int, int, str]]] = {
    "user_2": [
        ("ELE-04", "Shivu", -50, 11, "confirmed"),
        ("CLN-04", "Team Meena", -21, 10, "confirmed"),
        ("ELE-04", "Shivu", -6, 12, "confirmed"),
        ("PLB-01", "Suresh", 3, 10, "confirmed"),
    ],
    "user_3": [
        ("PST-02", "Team Gowda", -60, 9, "confirmed"),
        ("ACS-04", "Pappu", -30, 14, "confirmed"),
        ("CRP-02", "Raju", -12, 16, "cancelled"),
        ("ACS-04", "Pappu", 4, 9, "confirmed"),
    ],
}


def seed_all(store: AgentStore, memory: MemoryStore, clock: Clock, provider=None) -> None:
    """Profiles for the three demo users, their memories and booking history. Idempotent:
    anything already present is left alone."""
    with store.tx():  # all seeded or none: a crash mid-seed can't leave a half user
        for u in PROFILES:
            if store.get_user(u.user_id) is None:
                store.save_user(u)
        if not memory.all("user_1"):
            seed_user(store, memory, clock, "user_1")
        for uid, rows in _USER_MEMORIES.items():
            if memory.all(uid):
                continue
            for key, value, scope, evidence in rows:
                memory.add(uid, key, value, "explicit", evidence=evidence, scope=scope)
    if provider is not None:
        for uid, seeds in _USER_HISTORY.items():
            provider.seed_history(uid, seeds)
