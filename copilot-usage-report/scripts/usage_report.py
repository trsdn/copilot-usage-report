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
  usage_report.py [TIMEFRAME] [--from ISO] [--to ISO] [--logs DIR] [--json|--html] [--out FILE]

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


def compute(agg, counts):
    """Aggregate per-model rows (sorted by spend) and a TOTAL summary."""
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
    return rows, total


def render_json(rows, total, start, end):
    return json.dumps({"window_utc": {"from": start.isoformat(), "to": end.isoformat()},
                       "by_model": rows, "total": total}, indent=2)


def render_markdown(rows, total, start, end):
    out = []
    cr_pct = (100 * total["cache_read_tokens"] / total["input_tokens"]) if total["input_tokens"] else 0
    out.append("# Copilot CLI usage report")
    out.append(f"\n**Window (UTC):** {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')}")
    out.append(f"**Basis:** 1 AI Unit = 1 GitHub AI credit = $0.01 USD (from telemetry `total_nano_aiu`).\n")
    if not rows:
        out.append("_No assistant_usage events found in this window._")
        return "\n".join(out)
    out.append("| Model | Calls | Input | Cache-read | Cache-write | Output | Reasoning | AI Units | USD |")
    out.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for r in rows:
        out.append(f"| {r['model']} | {r['calls']} | {r['input_tokens']:,} | {r['cache_read_tokens']:,} | "
                   f"{r['cache_write_tokens']:,} | {r['output_tokens']:,} | {r['reasoning_tokens']:,} | "
                   f"{r['ai_units']:,.2f} | ${r['usd']:,.2f} |")
    out.append(f"| **TOTAL** | **{total['calls']}** | **{total['input_tokens']:,}** | "
               f"**{total['cache_read_tokens']:,}** | **{total['cache_write_tokens']:,}** | "
               f"**{total['output_tokens']:,}** | **{total['reasoning_tokens']:,}** | "
               f"**{total['ai_units']:,.2f}** | **${total['usd']:,.2f}** |")
    out.append(f"\n- **Total cost: {total['ai_units']:,.2f} AI Units = ${total['usd']:,.2f}**")
    out.append(f"- Cache-read share of input: **{cr_pct:.1f}%** "
               f"({total['cache_read_tokens']:,} of {total['input_tokens']:,})")
    top = rows[0]
    share = (100 * top["usd"] / total["usd"]) if total["usd"] else 0
    out.append(f"- Top model: **{top['model']}** at ${top['usd']:,.2f} ({share:.0f}% of spend)")
    return "\n".join(out)


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


HTML_CSS = """
:root{--bg:#f6f7f9;--fg:#1c2530;--mut:#5b6675;--line:#e3e7ec;--card:#fff;--accent:#2563eb;--accent2:#7c3aed}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 56px}
header h1{margin:0 0 4px;font-size:24px}
.meta{color:var(--mut);font-size:13px;margin:0}
.meta code{background:rgba(127,127,127,.14);padding:1px 5px;border-radius:5px;font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin:22px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:26px;font-weight:700;margin:6px 0 2px}
.card .v.top{font-size:18px;word-break:break-word}
.card .s{color:var(--mut);font-size:12px}
.card.hero{background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;color:#fff}
.card.hero .k,.card.hero .s{color:rgba(255,255,255,.85)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:13px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line)}
th{background:#fafbfc;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
th.n,td.n{text-align:right;font-variant-numeric:tabular-nums}
td.usd{font-weight:600}
td.model{position:relative;font-weight:600;min-width:180px}
td.model .bar{display:block;height:4px;margin-top:6px;width:var(--w);min-width:2px;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:3px}
tr.total td{font-weight:700;background:#fafbfc;border-bottom:0}
td.empty{text-align:center;color:var(--mut);padding:28px}
footer{color:var(--mut);font-size:12px;margin-top:18px}
@media(prefers-color-scheme:dark){:root{--bg:#0f1419;--fg:#e6e9ee;--mut:#9aa4b2;--line:#222a33;--card:#161c23}th,tr.total td{background:#12181f}.card .v{color:#fff}}
"""


def render_html(rows, total, start, end):
    cr_pct = (100 * total["cache_read_tokens"] / total["input_tokens"]) if total["input_tokens"] else 0
    window = f"{start.strftime('%Y-%m-%d %H:%M')} \u2192 {end.strftime('%Y-%m-%d %H:%M')} UTC"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    top = rows[0] if rows else None
    top_share = (100 * top["usd"] / total["usd"]) if (top and total["usd"]) else 0

    cards = f"""
      <div class="cards">
        <div class="card hero">
          <div class="k">Total cost</div>
          <div class="v">${total['usd']:,.2f}</div>
          <div class="s">{total['ai_units']:,.2f} AI Units</div>
        </div>
        <div class="card">
          <div class="k">Calls</div>
          <div class="v">{total['calls']:,}</div>
          <div class="s">across {len(rows)} model{'s' if len(rows) != 1 else ''}</div>
        </div>
        <div class="card">
          <div class="k">Cache-read share</div>
          <div class="v">{cr_pct:.1f}%</div>
          <div class="s">{total['cache_read_tokens']:,} of {total['input_tokens']:,} input</div>
        </div>
        <div class="card">
          <div class="k">Top model</div>
          <div class="v top">{_esc(top['model']) if top else '&mdash;'}</div>
          <div class="s">${top['usd']:,.2f} &middot; {top_share:.0f}% of spend</div>
        </div>
      </div>""" if rows else ""

    trows = []
    for r in rows:
        share = (100 * r["usd"] / total["usd"]) if total["usd"] else 0
        trows.append(
            "<tr>"
            f"<td class='model'>{_esc(r['model'])}"
            f"<span class='bar' style='--w:{share:.1f}%'></span></td>"
            f"<td class='n'>{r['calls']:,}</td>"
            f"<td class='n'>{r['input_tokens']:,}</td>"
            f"<td class='n'>{r['cache_read_tokens']:,}</td>"
            f"<td class='n'>{r['cache_write_tokens']:,}</td>"
            f"<td class='n'>{r['output_tokens']:,}</td>"
            f"<td class='n'>{r['reasoning_tokens']:,}</td>"
            f"<td class='n'>{r['ai_units']:,.2f}</td>"
            f"<td class='n usd'>${r['usd']:,.2f}</td>"
            "</tr>")
    body_rows = "\n".join(trows) if trows else (
        "<tr><td colspan='9' class='empty'>No assistant_usage events found in this window.</td></tr>")
    total_row = (
        "<tr class='total'><td>TOTAL</td>"
        f"<td class='n'>{total['calls']:,}</td>"
        f"<td class='n'>{total['input_tokens']:,}</td>"
        f"<td class='n'>{total['cache_read_tokens']:,}</td>"
        f"<td class='n'>{total['cache_write_tokens']:,}</td>"
        f"<td class='n'>{total['output_tokens']:,}</td>"
        f"<td class='n'>{total['reasoning_tokens']:,}</td>"
        f"<td class='n'>{total['ai_units']:,.2f}</td>"
        f"<td class='n usd'>${total['usd']:,.2f}</td></tr>") if rows else ""

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Copilot CLI usage report</title>\n<style>" + HTML_CSS + "</style>\n</head>\n"
        "<body>\n<div class=\"wrap\">\n<header>\n<h1>Copilot CLI usage report</h1>\n"
        f"<p class=\"meta\">Window: {_esc(window)} &middot; 1 AI Unit = 1 GitHub AI credit = $0.01 USD "
        "(from telemetry <code>total_nano_aiu</code>)</p>\n</header>\n"
        + cards +
        "\n<table>\n<thead><tr>"
        "<th>Model</th><th class='n'>Calls</th><th class='n'>Input</th><th class='n'>Cache-read</th>"
        "<th class='n'>Cache-write</th><th class='n'>Output</th><th class='n'>Reasoning</th>"
        "<th class='n'>AI Units</th><th class='n'>USD</th>"
        "</tr></thead>\n<tbody>\n" + body_rows + "\n" + total_row + "\n</tbody>\n</table>\n"
        f"<footer>Generated {generated} &middot; self-contained &amp; offline. "
        "AI Units from telemetry are authoritative; rate-card USD is a cross-check only.</footer>\n"
        "</div>\n</body>\n</html>\n")


def main():
    p = argparse.ArgumentParser(description="Copilot CLI token & cost usage report")
    p.add_argument("timeframe", nargs="?", default="24h",
                   help="relative window: 30m,12h,48h,7d,2w,today,yesterday (default 24h)")
    p.add_argument("--from", dest="from_", help="explicit start, ISO UTC e.g. 2026-06-19T00:00:00")
    p.add_argument("--to", dest="to", help="explicit end, ISO UTC (default now)")
    p.add_argument("--logs", default=os.path.expanduser("~/.copilot/logs"),
                   help="log directory (default ~/.copilot/logs)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    p.add_argument("--html", action="store_true", help="emit a self-contained HTML report")
    p.add_argument("--out", help="write the report to a file instead of stdout")
    args = p.parse_args()
    start, end = parse_window(args)
    if not os.path.isdir(args.logs):
        sys.exit(f"Log directory not found: {args.logs}")
    agg, counts = aggregate(args.logs, start, end)
    rows, total = compute(agg, counts)
    if args.json:
        text = render_json(rows, total, start, end)
    elif args.html:
        text = render_html(rows, total, start, end)
    else:
        text = render_markdown(rows, total, start, end)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        print(f"Wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
