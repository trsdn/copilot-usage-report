#!/usr/bin/env python3
"""Dependency-free smoke test for usage_report.py.

Builds a synthetic ~/.copilot-style log in a temp dir, then exercises the
markdown, JSON and HTML output paths and asserts the totals are correct and the
HTML is self-contained (no external http(s) asset references). Run with:

    python3 tests/smoke.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "copilot-usage-report", "scripts", "usage_report.py")

# (model, calls, input, cache_read, cache_write, output, reasoning, aiu)
FIXTURE = [
    ("claude-opus-4.8",   100, 10_000_000, 9_000_000, 600_000, 120_000, 12_000, 2000.0),
    ("gpt-5.5",            40,  3_000_000, 2_500_000,       0,  40_000,  8_000,  500.0),
    ("gpt-5-mini",         10,    200_000,   140_000,       0,   6_000,  2_000,    4.0),
]
WINDOW = ["--from", "2026-06-20T00:00:00", "--to", "2026-06-21T00:00:00"]


def build_log(path):
    lines, eid = [], 0
    for model, calls, inp, cr, cw, out, rsn, aiu in FIXTURE:
        for _ in range(calls):
            eid += 1
            frac = 1.0 / calls
            obj = {
                "kind": "assistant_usage",
                "properties": {"model": model, "event_id": f"evt-{eid}"},
                "metrics": {
                    "input_tokens": round(inp * frac),
                    "input_tokens_uncached": round((inp - cr - cw) * frac),
                    "output_tokens": round(out * frac),
                    "cache_read_tokens": round(cr * frac),
                    "cache_write_tokens": round(cw * frac),
                    "reasoning_tokens": round(rsn * frac),
                    "total_nano_aiu": round(aiu * 1e9 * frac),
                    "cost": 0,
                },
            }
            ts = f"2026-06-20T0{eid % 9}:{eid % 60:02d}:00.000Z"
            lines.append(f"{ts} [Telemetry] cli.telemetry:")
            lines.append(json.dumps(obj))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def run(args, logs):
    cmd = [sys.executable, SCRIPT, *WINDOW, "--logs", logs, *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}: {p.stderr}\n{p.stdout}"
    return p.stdout


def main():
    failures = []

    def check(name, cond):
        print(f"{'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            failures.append(name)

    # py_compile: the script must at least parse/compile.
    import py_compile
    try:
        py_compile.compile(SCRIPT, doraise=True)
        check("usage_report.py compiles", True)
    except py_compile.PyCompileError as e:
        check(f"usage_report.py compiles ({e})", False)

    with tempfile.TemporaryDirectory() as d:
        logs = os.path.join(d, "logs")
        os.makedirs(logs)
        build_log(os.path.join(logs, "sample.log"))

        total_calls = sum(f[1] for f in FIXTURE)         # 150
        total_aiu = sum(f[7] for f in FIXTURE)           # 2504.0
        total_usd = round(total_aiu / 100, 2)            # 25.04

        # JSON path
        data = json.loads(run(["--json"], logs))
        check("json: 3 models", len(data["by_model"]) == 3)
        check("json: total calls", data["total"]["calls"] == total_calls)
        check("json: total AI units", abs(data["total"]["ai_units"] - total_aiu) < 0.5)
        check("json: total USD", abs(data["total"]["usd"] - total_usd) < 0.01)
        check("json: sorted by spend desc",
              [r["model"] for r in data["by_model"]]
              == ["claude-opus-4.8", "gpt-5.5", "gpt-5-mini"])

        # Markdown path
        md = run([], logs)
        check("markdown: has report header", "# Copilot CLI usage report" in md)
        check("markdown: has TOTAL row", "**TOTAL**" in md)
        check("markdown: shows total USD", f"${total_usd:,.2f}" in md)

        # HTML path
        html = run(["--html"], logs)
        check("html: doctype", html.lstrip().lower().startswith("<!doctype html>"))
        check("html: has summary cards", 'class="card hero"' in html)
        check("html: has spend bars", "class='bar'" in html)
        check("html: shows total USD", f"${total_usd:,.2f}" in html)
        ext = re.findall(r'(?:src|href)\s*=\s*["\']https?://', html)
        check("html: self-contained (no external assets)", not ext)

        # Empty window must not crash.
        empty = run(["--from", "2000-01-01T00:00:00", "--to", "2000-01-02T00:00:00"], logs)
        check("empty window: graceful", "No assistant_usage events found" in empty)

    if failures:
        print(f"\n{len(failures)} check(s) failed.")
        sys.exit(1)
    print("\nAll smoke checks passed.")


if __name__ == "__main__":
    main()
