# Errand Agent

A chat agent that arranges home-service errands (book, cancel, reschedule) against mock provider tools.
The LLM understands the user and drives tool calls; application code enforces approvals, idempotency,
recovery, memory filtering and execution limits.

Work in progress. See [docs/PLAN.md](docs/PLAN.md) for the build plan.

## Setup

```bash
uv sync
uv run pytest
```
