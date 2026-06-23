#!/usr/bin/env python3
"""Copilot CLI token & cost usage report.

Parses `assistant_usage` telemetry events from the local Copilot CLI logs,
aggregates tokens by model over a requested timeframe, and reports cost.

Billing basis (GitHub official): usage is metered in GitHub AI Credits where
1 AI credit = $0.01 USD. The telemetry field `total_nano_aiu` is credits in
nano units, so AIU = total_nano_aiu / 1e9 and USD = AIU / 100. This AIU value
is GitHub's own billed amount, so it is used as the authoritative cost and
stays correct even for models not in the local rate card below.

Usage:
  usage_report.py [TIMEFRAME] [--from ISO] [--to ISO] [--logs DIR] [--json]

TIMEFRAME (relative window ending now, UTC):
  30m, 12h, 48h, 7d, 2w  | today | yesterday   (default: 24h)
Explicit window overrides TIMEFRAME:
  --from 2026-06-19T00:00:00 --to 2026-06-20T00:00:00
"""
import argparse, glob, json, os, re, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Per-1M-token USD rates: (uncached_input, cached_input, cache_write, output).
# Cross-check only; the authoritative cost is the telemetry AIU. Update from
# https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
RATES = {
    "claude-opus-4.8":   (5.00, 0.50, 6.25, 25.00),
    "claude-opus-4.7":   (5.00, 0.50, 6.25, 25.00),
    "claude-opus-4.6":   (5.00, 0.50, 6.25, 25.00),
    "claude-opus-4.5":   (5.00, 0.50, 6.25, 25.00),
    "claude-sonnet-4.6": (3.00, 0.30, 3.75, 15.00),
    "claude-sonnet-4.5": (3.00, 0.30, 3.75, 15.00),
    "claude-haiku-4.5":  (1.00, 0.10, 1.25,  5.00),
    "gpt-5.5":           (5.00, 0.50, 0.00, 30.00),
    "gpt-5.4":           (2.50, 0.25, 0.00, 15.00),
    "gpt-5.4-mini":      (0.75, 0.075, 0.00, 4.50),
    "gpt-5.4-nano":      (0.20, 0.02, 0.00,  1.25),
    "gpt-5.3-codex":     (1.75, 0.175, 0.00, 14.00),
    "gpt-5-mini":        (0.25, 0.025, 0.00, 2.00),
    "gemini-2.5-pro":    (1.25, 0.125, 0.00, 10.00),
    "gemini-3.1-pro":    (2.00, 0.20, 0.00, 12.00),
    "gemini-3-flash":    (0.50, 0.05, 0.00,  3.00),
}

MARK = "[Telemetry] cli.telemetry:"
TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)')
REL_RE = re.compile(r'^(\d+)\s*([mhdw])$')

TOKEN_KEYS = ("input_tokens", "input_tokens_uncached", "output_tokens",
              "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
              "total_nano_aiu", "cost")


def parse_window(args):
    now = datetime.now(timezone.utc)
    if args.from_:
        start = datetime.fromisoformat(args.from_).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.to).replace(tzinfo=timezone.utc) if args.to else now
        return start, end
    tf = (args.timeframe or "24h").strip().lower()
    if tf == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if tf == "yesterday":
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return end - timedelta(days=1), end
    m = REL_RE.match(tf)
    if not m:
        sys.exit(f"Unrecognized timeframe '{tf}'. Use e.g. 30m, 12h, 48h, 7d, 2w, today, yesterday.")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
             "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    return now - delta, now


def iter_usage_events(log_dir):
    """Yield (timestamp_str, event_dict) for each assistant_usage telemetry block."""
    for path in sorted(glob.glob(os.path.join(log_dir, "*.log"))):
        try:
            f = open(path, "r", errors="replace")
        except OSError:
            continue
        with f:
            cur_ts, buf, depth = None, None, 0
            for line in f:
                if buf is None:
                    m = TS_RE.match(line)
                    if m and MARK in line:
                        cur_ts = m.group(1)
                        continue
                    if cur_ts is not None and line.startswith("{"):
                        buf = [line]
                        depth = line.count("{") - line.count("}")
                        if depth > 0:
                            continue
                    else:
                        continue
                else:
                    buf.append(line)
                    depth += line.count("{") - line.count("}")
                    if depth > 0:
                        continue
                block = "".join(buf)
                buf = None
                try:
                    obj = json.loads(block)
                except Exception:
                    cur_ts = None
                    continue
                if isinstance(obj, dict) and obj.get("kind") == "assistant_usage":
                    yield cur_ts, obj
                cur_ts = None


def aggregate(log_dir, start, end):
    s_iso, e_iso = start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")
    agg = defaultdict(lambda: defaultdict(float))
    counts = defaultdict(int)
    seen = set()
    for ts, obj in iter_usage_events(log_dir):
        if not ts or not (s_iso <= ts[:19] <= e_iso):
            continue
        props, mets = obj.get("properties", {}), obj.get("metrics", {})
        eid = props.get("event_id")
        if eid:
            if eid in seen:
                continue
            seen.add(eid)
        model = props.get("model", "unknown")
        counts[model] += 1
        for k in TOKEN_KEYS:
            v = mets.get(k)
            if isinstance(v, (int, float)):
                agg[model][k] += v
    return agg, counts


def ratecard_usd(model, a):
    r = RATES.get(model)
    if not r:
        return None
    ri, rc, rw, ro = r
    unc = a["input_tokens_uncached"] or (a["input_tokens"] - a["cache_read_tokens"] - a["cache_write_tokens"])
    return (unc / 1e6 * ri + a["cache_read_tokens"] / 1e6 * rc
            + a["cache_write_tokens"] / 1e6 * rw + a["output_tokens"] / 1e6 * ro)


def render(agg, counts, start, end, as_json=False):
    rows = []
    tot = defaultdict(float)
    for model in sorted(agg, key=lambda m: -agg[m]["total_nano_aiu"]):
        a = agg[model]
        aiu = a["total_nano_aiu"] / 1e9
        row = {
            "model": model, "calls": counts[model],
            "input_tokens": int(a["input_tokens"]),
            "cache_read_tokens": int(a["cache_read_tokens"]),
            "cache_write_tokens": int(a["cache_write_tokens"]),
            "output_tokens": int(a["output_tokens"]),
            "reasoning_tokens": int(a["reasoning_tokens"]),
            "ai_units": round(aiu, 2), "usd": round(aiu / 100, 2),
            "ratecard_usd": (round(ratecard_usd(model, a), 2)
                             if ratecard_usd(model, a) is not None else None),
        }
        rows.append(row)
        for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens",
                  "output_tokens", "reasoning_tokens", "total_nano_aiu"):
            tot[k] += a[k]
        tot["calls"] += counts[model]
    total = {
        "calls": int(tot["calls"]), "input_tokens": int(tot["input_tokens"]),
        "cache_read_tokens": int(tot["cache_read_tokens"]),
        "cache_write_tokens": int(tot["cache_write_tokens"]),
        "output_tokens": int(tot["output_tokens"]),
        "reasoning_tokens": int(tot["reasoning_tokens"]),
        "ai_units": round(tot["total_nano_aiu"] / 1e9, 2),
        "usd": round(tot["total_nano_aiu"] / 1e9 / 100, 2),
    }
    if as_json:
        print(json.dumps({"window_utc": {"from": start.isoformat(), "to": end.isoformat()},
                          "by_model": rows, "total": total}, indent=2))
        return

    cr_pct = (100 * total["cache_read_tokens"] / total["input_tokens"]) if total["input_tokens"] else 0
    print(f"# Copilot CLI usage report")
    print(f"\n**Window (UTC):** {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')}")
    print(f"**Basis:** 1 AI Unit = 1 GitHub AI credit = $0.01 USD (from telemetry `total_nano_aiu`).\n")
    if not rows:
        print("_No assistant_usage events found in this window._")
        return
    print("| Model | Calls | Input | Cache-read | Cache-write | Output | Reasoning | AI Units | USD |")
    print("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for r in rows:
        print(f"| {r['model']} | {r['calls']} | {r['input_tokens']:,} | {r['cache_read_tokens']:,} | "
              f"{r['cache_write_tokens']:,} | {r['output_tokens']:,} | {r['reasoning_tokens']:,} | "
              f"{r['ai_units']:,.2f} | ${r['usd']:,.2f} |")
    print(f"| **TOTAL** | **{total['calls']}** | **{total['input_tokens']:,}** | "
          f"**{total['cache_read_tokens']:,}** | **{total['cache_write_tokens']:,}** | "
          f"**{total['output_tokens']:,}** | **{total['reasoning_tokens']:,}** | "
          f"**{total['ai_units']:,.2f}** | **${total['usd']:,.2f}** |")
    print(f"\n- **Total cost: {total['ai_units']:,.2f} AI Units = ${total['usd']:,.2f}**")
    print(f"- Cache-read share of input: **{cr_pct:.1f}%** "
          f"({total['cache_read_tokens']:,} of {total['input_tokens']:,})")
    if rows:
        top = rows[0]
        share = (100 * top["usd"] / total["usd"]) if total["usd"] else 0
        print(f"- Top model: **{top['model']}** at ${top['usd']:,.2f} ({share:.0f}% of spend)")


def main():
    p = argparse.ArgumentParser(description="Copilot CLI token & cost usage report")
    p.add_argument("timeframe", nargs="?", default="24h",
                   help="relative window: 30m,12h,48h,7d,2w,today,yesterday (default 24h)")
    p.add_argument("--from", dest="from_", help="explicit start, ISO UTC e.g. 2026-06-19T00:00:00")
    p.add_argument("--to", dest="to", help="explicit end, ISO UTC (default now)")
    p.add_argument("--logs", default=os.path.expanduser("~/.copilot/logs"),
                   help="log directory (default ~/.copilot/logs)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = p.parse_args()
    start, end = parse_window(args)
    if not os.path.isdir(args.logs):
        sys.exit(f"Log directory not found: {args.logs}")
    agg, counts = aggregate(args.logs, start, end)
    render(agg, counts, start, end, as_json=args.json)


if __name__ == "__main__":
    main()
