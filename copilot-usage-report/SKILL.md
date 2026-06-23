---
name: "copilot-usage-report"
description: "Generate a Copilot CLI token & cost usage report for a requested timeframe. Use when the user asks how many tokens / AI Units / AI credits / cost were used over a period (e.g. 'last 48h', 'today', 'last 7 days'). Reports input/output/cache-read/cache-write tokens per model and converts to GitHub AI Credits / USD."
domain: "observability"
confidence: "high"
source: "earned"
tools:
  - name: "bash"
---

Generate a token-and-cost usage report for GitHub Copilot CLI over a user-specified timeframe.

## Where the data comes from

Copilot CLI writes `assistant_usage` telemetry events to `~/.copilot/logs/*.log`. Each
event has `properties.model` and a `metrics` block:

```
"metrics": {
  "input_tokens": ..., "input_tokens_uncached": ..., "output_tokens": ...,
  "cache_read_tokens": ..., "cache_write_tokens": ..., "reasoning_tokens": ...,
  "total_nano_aiu": ..., "cost": ..., "duration": ...
}
```

Key relationships:

- `input_tokens = input_tokens_uncached + cache_read_tokens + cache_write_tokens`.
- **AI Units (AIU)** `= total_nano_aiu / 1e9`. This is GitHub's billed amount.
- Billing basis (GitHub official): **1 AI Unit = 1 GitHub AI credit = $0.01 USD**, so
  `USD = AIU / 100`. AIU is authoritative and stays correct even for models not in the
  local rate card.
- `cost` is a legacy premium-request counter — **do not** report it as dollars.
- **Cache write** is a real, separately-billed operation for Anthropic models only
  (first time context is cached); OpenAI/Google rate cards have no cache-write charge,
  so their `cache_write_tokens` is 0.

## How to run

The skill ships a parser. Run it with the user's timeframe:

```bash
python3 .copilot/skills/copilot-usage-report/scripts/usage_report.py <TIMEFRAME>
```

`<TIMEFRAME>` accepts:

- Relative window ending now (UTC): `30m`, `12h`, `48h`, `7d`, `2w` (default `24h`).
- `today` or `yesterday`.
- Explicit window: `--from 2026-06-19T00:00:00 --to 2026-06-20T00:00:00` (ISO, UTC).

Other flags: `--json` for machine-readable output, `--logs DIR` to point at a different
log directory (default `~/.copilot/logs`).

Examples:

```bash
python3 .copilot/skills/copilot-usage-report/scripts/usage_report.py 48h
python3 .copilot/skills/copilot-usage-report/scripts/usage_report.py 7d
python3 .copilot/skills/copilot-usage-report/scripts/usage_report.py today
python3 .copilot/skills/copilot-usage-report/scripts/usage_report.py --from 2026-06-01T00:00:00 --to 2026-06-08T00:00:00 --json
```

## Output

A markdown table broken down per model — calls, input, cache-read, cache-write, output,
reasoning tokens, AI Units, and USD — plus a TOTAL row and a short summary (total cost,
cache-read share of input, top-spend model). Present this table to the user verbatim.

## Workflow for the agent

1. Confirm the timeframe. If the user was vague ("recently"), pick a sensible default
   (`24h`) and state the assumption, or ask.
2. Run the script with the timeframe. Log files can total >1GB; the parser streams them,
   so a run takes ~10-30s. Use a generous `initial_wait`.
3. Show the markdown report. If the user wants raw numbers for another tool, re-run with
   `--json`.

## Maintenance

The optional rate-card cross-check (`ratecard_usd`) lives in `RATES` inside the script.
AIU is the source of truth, but if you add the rate card for a new model, copy per-1M
rates from the official page:
https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
