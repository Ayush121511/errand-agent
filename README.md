# Errand Agent

A chat agent that arranges home-service errands (book, cancel, reschedule) against mock provider tools.
The LLM understands the user and drives tool calls; application code enforces approvals, idempotency,
recovery, memory filtering and execution limits.

Work in progress. See [docs/PLAN.md](docs/PLAN.md) for the build plan.

## Setup

With conda:

```bash
conda create -n errand-agent python=3.12 -y
conda activate errand-agent
pip install -e ".[dev]"
pytest
```

Or with uv: `uv sync && uv run pytest`.

The live agent needs a Gemini key: copy `.env.example` to `.env` and set `GEMINI_API_KEY`.
Tests and evals run fully offline and need no key.
