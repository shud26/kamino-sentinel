#!/usr/bin/env python3
"""Tests for the report-recognition logic in daily_report.py.

Run: python3 contrib/test_daily_report.py

Only `has_verdict` is covered, because that one predicate decides whether a scheduled run is
reported as a success or a failure — and it got this wrong in a way that mattered. The original
version tested for the verdict tokens as substrings anywhere in the text. The plugin's
fetch-failure message contains the token NO-POSITION in the sentence that denies it, so an API
outage was accepted as a clean verdict and the run exited 0. The scheduler logged green for a
failure: the exact confusion the sentinel exists to prevent, committed by the sentinel.

The strings below are not invented. `FETCH_FAILURE` is copied from a real runtime-trace entry
produced by calling the tool with an invalid wallet.
"""
import importlib.util
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("daily_report", os.path.join(_HERE, "daily_report.py"))
daily_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_report)
has_verdict = daily_report.has_verdict

# Verbatim from ~/.zeroclaw/data/state/runtime-trace.jsonl, attributes.output, after calling
# kamino_sentinel with wallet="INVALIDWALLET123". Note the "Error: " prefix added by the host
# and the NO-POSITION token inside the denial.
FETCH_FAILURE = (
    "Error: [UNKNOWN] Kamino 조회 실패 — INVALIDWALLET123\n"
    "포지션 상태를 확인하지 못했습니다. "
    "이것은 NO-POSITION(포지션 없음)이 아닙니다.\n"
    "원인: 3회 시도 후 실패 — HTTP 400"
)

CASES = [
    # (name, text, expected)
    ("ok report",
     "[OK] Kamino sentinel — 4DNPMDrqt6\n- #1: deposit $97.12 | borrow $25.01 "
     "| LTV 25.8% -> liq 75.0% (cushion 49.2%p)", True),
    ("warn report",
     "[WARN] Kamino sentinel — 4DNPMDrqt6\n! #1: deposit $97.12 | borrow $80.00", True),
    ("danger report",
     "[DANGER] Kamino sentinel — 4DNPMDrqt6\n!! #1: deposit $97.12 | borrow $90.00\n"
     "cushion <= 5.0%p: consider repaying or adding collateral NOW.", True),
    ("no-position report", "[NO-POSITION] no Kamino obligations for 4DNPMDrqt6", True),
    ("leading whitespace is tolerated", "\n  [OK] Kamino sentinel — 4DNPMDrqt6", True),

    ("fetch failure is not a verdict", FETCH_FAILURE, False),
    ("model apology mentioning a verdict",
     "I'm sorry, I could not retrieve a NO-POSITION result for that wallet.", False),
    ("verdict tag buried mid-text is not a report",
     "The tool would normally answer with [OK] here, but it did not run.", False),
    ("empty", "", False),
    ("whitespace only", "   \n  ", False),
    ("none", None, False),
]


def _trace_line(ts, output=None, error_reason=None, tool="kamino_sentinel"):
    attrs = {"tool": tool}
    if output is not None:
        attrs["output"] = output
    if error_reason is not None:
        attrs["error_reason"] = error_reason
    return json.dumps({"@timestamp": ts, "message": "tool_call_result", "attributes": attrs})


OK_LINE = "[OK] Kamino sentinel — 4DNPMDrqt6\n- #1: deposit $94.84 | borrow $25.02"
ERR_LINE = ("[UNKNOWN] Kamino lookup failed — INVALIDWALLET123\n"
            "Could not read the position. This is NOT a no-position result.")


def scan_cases():
    """(name, trace lines, since, expected_ok, expected_err) for scan_trace."""
    return [
        ("success is returned as ok",
         [_trace_line("2026-08-02T10:00:00Z", output=OK_LINE)],
         "2026-08-02T09:00:00", OK_LINE, None),

        # The whole point: a failed call must never arrive in the ok slot.
        ("failure never lands in the ok slot",
         [_trace_line("2026-08-02T10:00:00Z", output="Error: " + ERR_LINE, error_reason=ERR_LINE)],
         "2026-08-02T09:00:00", None, ERR_LINE),

        ("entries older than the run are ignored",
         [_trace_line("2026-08-02T08:00:00Z", output=OK_LINE)],
         "2026-08-02T09:00:00", None, None),

        ("newest success wins",
         [_trace_line("2026-08-02T10:00:00Z", output="[OK] older"),
          _trace_line("2026-08-02T11:00:00Z", output=OK_LINE)],
         "2026-08-02T09:00:00", OK_LINE, None),

        ("another tool's result is not ours",
         [_trace_line("2026-08-02T10:00:00Z", output=OK_LINE, tool="something_else")],
         "2026-08-02T09:00:00", None, None),

        # A run that fails and then succeeds on retry must still surface the success.
        ("success after a failure is still a success",
         [_trace_line("2026-08-02T10:00:00Z", output="Error", error_reason=ERR_LINE),
          _trace_line("2026-08-02T10:00:05Z", output=OK_LINE)],
         "2026-08-02T09:00:00", OK_LINE, ERR_LINE),
    ]


def run_scan_tests():
    failures = []
    for name, lines, since, exp_ok, exp_err in scan_cases():
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            got_ok, got_err = daily_report.scan_trace(path, since)
        finally:
            os.unlink(path)
        good = (got_ok == exp_ok) and (got_err == exp_err)
        if not good:
            failures.append(name)
        print("%-4s %-42s" % ("PASS" if good else "FAIL", name))
    return failures


def run_prompt_tests():
    failures = []
    cases = [
        ("no wallet uses the default prompt",
         daily_report.build_prompt(None) == daily_report.PROMPT),
        ("wallet is interpolated verbatim",
         "INVALIDWALLET123" in daily_report.build_prompt("INVALIDWALLET123")),
        ("wallet prompt still suppresses commentary",
         "no commentary" in daily_report.build_prompt("INVALIDWALLET123")),
    ]
    for name, good in cases:
        if not good:
            failures.append(name)
        print("%-4s %-42s" % ("PASS" if good else "FAIL", name))
    return failures


def main():
    failures = []
    for name, text, expected in CASES:
        got = has_verdict(text)
        status = "PASS" if got == expected else "FAIL"
        if got != expected:
            failures.append(name)
        print("%-4s %-42s expected=%-5s got=%s" % (status, name, expected, got))

    print()
    failures += run_scan_tests()
    print()
    failures += run_prompt_tests()

    total = len(CASES) + len(scan_cases()) + 3
    print()
    if failures:
        print("%d failed: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all %d passed" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
