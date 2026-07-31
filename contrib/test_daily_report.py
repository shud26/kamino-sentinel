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
import os
import sys

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


def main():
    failures = []
    for name, text, expected in CASES:
        got = has_verdict(text)
        status = "PASS" if got == expected else "FAIL"
        if got != expected:
            failures.append(name)
        print("%-4s %-42s expected=%-5s got=%s" % (status, name, expected, got))

    print()
    if failures:
        print("%d failed: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all %d passed" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
