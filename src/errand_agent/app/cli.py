"""Terminal chat with the live agent.

    uv run python -m errand_agent.app.cli [--session S-0001] [--trace]

Commands: /trace (toggle trace lines), /bookings, /memory, /force <outcome>,
/provider-cancel, /poll, /new, /quit. Plain "yes" confirms the pending card.
"""

from __future__ import annotations

import argparse
import json

from errand_agent.app.factory import build_live

DIM, BOLD, CYAN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


def short(ev: dict) -> str:
    t = ev["type"]
    if t == "tool_call":
        return f"tool {ev['tool']}({json.dumps(ev.get('args'))[:80]}) ok={ev['ok']} " \
               f"{ev.get('error') or ''} {ev['latency_ms']}ms"
    if t == "model_call":
        return f"model {ev.get('model')} in={ev.get('input_tokens')} out={ev.get('output_tokens')}"\
               f" ${ev.get('cost_usd')} calls={[c['name'] for c in ev.get('tool_calls', [])]}"
    if t == "state_transition":
        return f"state {ev['from_']} → {ev['to']} ({ev['reason']})"
    if t == "context_built":
        return f"context {ev['total_tokens']}/{ev['budget_tokens']} tok, " \
               f"memories {ev['memory_included']}"
    return t + " " + json.dumps({k: v for k, v in ev.items() if k not in (
        "seq", "gseq", "ts", "session_id", "run_id", "elapsed_ms", "type")})[:120]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    ap.add_argument("--user", help="user_1 / user_2 / user_3 (default user_1)")
    ap.add_argument("--trace", action="store_true")
    args = ap.parse_args()
    app = build_live()
    agent = app.agent
    s = agent.open_session(args.session, user_id=args.user)
    show_trace = {"on": args.trace}

    def on_event(ev: dict) -> None:
        if show_trace["on"] and ev["type"] not in ("assistant_message", "user_message"):
            print(f"{DIM}  · {short(ev)}{RESET}")

    agent.tracer(s.session_id).subscribe(on_event)
    print(f"{BOLD}Errand agent{RESET} — session {s.session_id} "
          f"(trace file: {agent.tracer(s.session_id).path})")
    for sid, msgs in app.recovered.items():
        for m in msgs:
            print(f"{YELLOW}[recovery in {sid}] {m}{RESET}")
    if not agent.store.messages(s.user_id, s.session_id):  # new chat: greet first
        print(f"{BOLD}agent>{RESET} {agent.greet(s.session_id)}")
    while True:
        try:
            text = input(f"{CYAN}you>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/trace":
            show_trace["on"] = not show_trace["on"]
            continue
        if text == "/bookings":
            for b in app.provider.list_bookings(s.user_id)["bookings"]:
                print(f"  {b['booking_id']} {b['service']:<14} {b['provider_name']:<22} "
                      f"{b['slot']} {b['status']}")
            continue
        if text == "/memory":
            for m in agent.memory.all(s.user_id):
                print(f"  {m.memory_id} {m.kind:<8} {m.status:<10} {m.key}={m.value}")
            continue
        if text.startswith("/force "):
            app.provider.config.forced_book_outcomes.append(text.split()[1])  # type: ignore
            print(f"  next book() outcome forced: {text.split()[1]}")
            continue
        if text == "/provider-cancel":
            up = [b for b in app.provider.list_bookings(s.user_id)["bookings"]
                  if b["when"] == "upcoming" and b["status"] == "confirmed"]
            if up:
                app.provider.simulate_provider_cancellation(up[0]["booking_id"],
                                                            "Technician unavailable")
                print(f"  provider cancelled {up[0]['booking_id']} (agent not told yet)")
            text = "/poll"
        if text == "/poll":
            for sid, msgs in agent.check_proactive().items():
                for m in msgs:
                    print(f"{YELLOW}agent (proactive, {sid})>{RESET} {m}")
            continue
        if text == "/new":
            s = agent.open_session()
            agent.tracer(s.session_id).subscribe(on_event)
            print(f"  new session {s.session_id}")
            continue
        for reply in agent.handle_message(s.session_id, text):
            print(f"{BOLD}agent>{RESET} {reply}")
        for sid, msgs in agent.check_proactive().items():
            for m in msgs:
                print(f"{YELLOW}agent (proactive, {sid})>{RESET} {m}")


if __name__ == "__main__":
    main()
