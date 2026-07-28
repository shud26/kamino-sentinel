#!/usr/bin/env python3
"""Run kamino-sentinel once and deliver the report to a chat channel.

Why this exists
---------------
A ZeroClaw agent spawned by the cron scheduler does not get the channel registry
attached, so `send_via` inside a scheduled agent prompt fails with

    Channel '<name>' not found. Available: []

while the cron run is still recorded as `ok`. The report is generated and logged, and
nothing is delivered. Binding the channel to the agent in config does not change this;
the binding applies to the gateway/dashboard context, not to scheduler-spawned turns.

This script splits the two steps and uses the CLI paths that do have channel access:

    zeroclaw agent -m ...      -> produce the report
    zeroclaw channel send ...  -> deliver it

Failure policy (the point of a sentinel)
----------------------------------------
Never stay silent, and never let a failure look like a clean verdict. If the report
cannot be produced, or does not carry a recognised verdict, an explicit UNKNOWN alert
is delivered instead and the process exits non-zero so the scheduler records a failure
as well. Silence and a green status are both unacceptable outcomes for a watchdog.

Python rather than shell because ZeroClaw's default risk profile allowlists `python3`
for scheduled commands but not `sh`, so this runs without loosening the host policy.

Usage
-----
    python3 contrib/daily_report.py --recipient <chat id>

    --bin        path to the zeroclaw binary   (default: zeroclaw, or $ZC_BIN)
    --agent      agent alias to run as         (default: spike,    or $ZC_AGENT)
    --channel    channel id for `channel send` (default: telegram, or $ZC_CHANNEL)
    --recipient  chat id / recipient           (required,          or $ZC_RECIPIENT)
"""
import argparse
import os
import subprocess
import sys

PROMPT = ("Call the kamino_sentinel tool with no arguments. "
          "Reply with the exact tool output text only, no commentary.")

UNKNOWN_PREFIX = ("[UNKNOWN] Kamino sentinel could not read the position. "
                  "This is NOT a NO-POSITION result. Check manually.")

# A delivered message must carry one of these. Without the check, an apology or a stray
# sentence from the model would go out looking like a position report.
VERDICTS = ("[OK]", "[WARN]", "[DANGER]", "NO-POSITION")

AGENT_TIMEOUT_S = 300
SEND_TIMEOUT_S = 60


def parse_args():
    p = argparse.ArgumentParser(description="Deliver a kamino-sentinel report.")
    p.add_argument("--bin", default=os.environ.get("ZC_BIN", "zeroclaw"))
    p.add_argument("--agent", default=os.environ.get("ZC_AGENT", "spike"))
    p.add_argument("--channel", default=os.environ.get("ZC_CHANNEL", "telegram"))
    p.add_argument("--recipient", default=os.environ.get("ZC_RECIPIENT"))
    a = p.parse_args()
    if not a.recipient:
        p.error("--recipient (or ZC_RECIPIENT) is required")
    return a


def deliver(args, body):
    """Send one message. Returns True on success."""
    try:
        r = subprocess.run(
            [args.bin, "channel", "send", body,
             "--channel-id", args.channel, "--recipient", args.recipient],
            capture_output=True, text=True, timeout=SEND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print("delivery timed out", file=sys.stderr)
        return False
    if r.returncode != 0:
        print("delivery failed: %s" % (r.stderr or r.stdout).strip()[:300], file=sys.stderr)
        return False
    return True


def produce(args):
    """Run the sentinel once. Returns (report_text, ok)."""
    try:
        r = subprocess.run(
            [args.bin, "agent", "--agent", args.agent, "-m", PROMPT],
            capture_output=True, text=True, timeout=AGENT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "agent run timed out after %ds" % AGENT_TIMEOUT_S, False
    text = (r.stdout or "").strip()
    if r.returncode != 0:
        return (r.stderr or text).strip(), False
    if not any(v in text for v in VERDICTS):
        return text, False
    return text, True


def main():
    args = parse_args()
    report, ok = produce(args)

    if not ok:
        detail = report[:500] if report else "(no output)"
        deliver(args, "%s\n\n%s" % (UNKNOWN_PREFIX, detail))
        print("sentinel run failed; UNKNOWN alert dispatched", file=sys.stderr)
        return 1

    if not deliver(args, report):
        # The report is good but nobody received it. Surface it in the scheduler log
        # so the failure is not invisible.
        print("report produced but delivery failed:\n%s" % report, file=sys.stderr)
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
