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

The verdict comes from the tool, not from the model
---------------------------------------------------
The model is only a trigger for the tool call. Its prose is not the report and is never
delivered. Asking a model to "reply with the exact tool output" works on a large model
and fails on a small one: a 14B model called the tool correctly and then paraphrased the
result into a friendly summary with the `[OK]` verdict dropped, and a 7B model looped on
the call until the host's circuit breaker stopped it.

So the report is read back from the tool result recorded in ZeroClaw's runtime trace,
filtered to entries newer than the moment this run started. Whatever the model says
afterwards is ignored. That keeps the delivered text byte-identical to what the plugin
produced and makes the script work with whatever model the host can afford to run.

Once that result appears the agent is killed rather than waited on, because everything
after the tool call is the model talking to itself. This is also what makes the job fit
the scheduler's budget: ZeroClaw kills a shell cron job at 120 s (hardcoded in
`SHELL_JOB_TIMEOUT_SECS`), and on a small always-on box a local model can spend six
minutes narrating after a tool call that returned in seventy seconds. Waiting for the
model to finish is what blows the budget; the report itself is ready long before.

A pleasant side effect: a model that loops on the tool call no longer matters either,
since the first result ends the run.

Failure policy (the point of a sentinel)
----------------------------------------
Never stay silent, and never let a failure look like a clean verdict. If the report
cannot be produced, or does not carry a recognised verdict, an explicit UNKNOWN alert
is delivered instead and the process exits non-zero so the scheduler records a failure
as well. Silence and a green status are both unacceptable outcomes for a watchdog.

The failure text comes from the tool too
----------------------------------------
The success path was already read from the trace so the model could not rewrite it, but
the failure path was not: a failed tool call was skipped, the run fell through to the
model's reply, and the alert went out carrying whatever prose the model had produced
about the failure. On a 7B model that prose was a friendly paraphrase, and the run also
burned ten tool iterations getting there.

So a failed tool call is now recognised the moment it lands, exactly like a successful
one. The plugin's own `error_reason` becomes the alert body, and the agent is stopped
right there. The alert a human reads is therefore always the plugin's wording, whether
the news is good or bad.

Usage
-----
    python3 contrib/daily_report.py --recipient <chat id>
    python3 contrib/daily_report.py --recipient <chat id> --wallet <bad address>

The second form is how the failure path is demonstrated on demand: it drives the same
code path the 8 AM job uses, against a wallet the upstream API rejects.

Python rather than shell because ZeroClaw's default risk profile allowlists `python3`
for scheduled commands but not `sh`, so this runs without loosening the host policy.

Usage
-----
    python3 contrib/daily_report.py --recipient <chat id>

    --bin        path to the zeroclaw binary   (default: zeroclaw, or $ZC_BIN)
    --agent      agent alias to run as         (default: spike,    or $ZC_AGENT)
    --channel    channel id for `channel send` (default: telegram, or $ZC_CHANNEL)
    --recipient  chat id / recipient           (required,          or $ZC_RECIPIENT)
    --trace      runtime trace path            (default: ~/.zeroclaw/data/state/runtime-trace.jsonl)
    --deadline   seconds to wait for the tool result (default: 95, or $ZC_DEADLINE)
    --wallet     override the configured wallet (default: none, use the config value)
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

PROMPT = ("Call the kamino_sentinel tool with no arguments. "
          "Reply with the exact tool output text only, no commentary.")

# Only the tool call differs; the reply instruction is identical because the reply is
# discarded either way. The wallet is interpolated rather than passed as a flag because
# the tool is reached through the agent, which is the point of the demonstration: the
# same path the scheduler uses, not a private back door.
PROMPT_WALLET = ("Call the kamino_sentinel tool with the wallet argument set exactly to "
                 "%s. Reply with the exact tool output text only, no commentary.")


def build_prompt(wallet):
    return PROMPT if not wallet else PROMPT_WALLET % wallet

UNKNOWN_PREFIX = ("[UNKNOWN] Kamino sentinel could not read the position. "
                  "This is NOT a NO-POSITION result. Check manually.")

# A report is identified by its bracketed verdict tag at the start of the first line, which is
# exactly how `report()` renders every verdict. Without the check, an apology or a stray sentence
# from the model would go out looking like a position report.
#
# Matching these as substrings anywhere in the text is NOT safe, and the unsafe version shipped:
# the plugin's own fetch-failure message contains the token NO-POSITION inside the sentence that
# denies it ("this is NOT a NO-POSITION result"), and the host records failed tool output with an
# "Error: " prefix. A bare `"NO-POSITION" in text` therefore accepted a fetch failure as a clean
# verdict and exited 0, so the scheduler logged a green run for an API outage — the precise
# confusion this tool exists to prevent, reproduced inside the tool itself.
VERDICT_TAGS = ("[OK]", "[WARN]", "[DANGER]", "[NO-POSITION]")

SEND_TIMEOUT_S = 60
TOOL_NAME = "kamino_sentinel"
DEFAULT_TRACE = "~/.zeroclaw/data/state/runtime-trace.jsonl"
# ZeroClaw kills a shell cron job at 120 s, so leave room for delivery afterwards.
DEFAULT_DEADLINE_S = 95


def has_verdict(text):
    """True only if `text` opens with a rendered verdict tag.

    Anchored to the first line so that prose merely mentioning a verdict — including the
    failure message that exists to say a result is *not* NO-POSITION — cannot pass.
    """
    if not text or not text.strip():
        return False
    first = text.strip().splitlines()[0]
    return any(first.startswith(tag) for tag in VERDICT_TAGS)


def parse_args():
    p = argparse.ArgumentParser(description="Deliver a kamino-sentinel report.")
    p.add_argument("--bin", default=os.environ.get("ZC_BIN", "zeroclaw"))
    p.add_argument("--agent", default=os.environ.get("ZC_AGENT", "spike"))
    p.add_argument("--channel", default=os.environ.get("ZC_CHANNEL", "telegram"))
    p.add_argument("--recipient", default=os.environ.get("ZC_RECIPIENT"))
    p.add_argument("--trace", default=os.environ.get("ZC_TRACE", DEFAULT_TRACE))
    p.add_argument("--deadline", type=float,
                   default=float(os.environ.get("ZC_DEADLINE", DEFAULT_DEADLINE_S)),
                   help="seconds to wait for the tool result before giving up")
    p.add_argument("--wallet", default=os.environ.get("ZC_WALLET"),
                   help="override the configured wallet (used to exercise the failure path)")
    a = p.parse_args()
    if not a.recipient:
        p.error("--recipient (or ZC_RECIPIENT) is required")
    a.trace = os.path.expanduser(a.trace)
    return a


def scan_trace(trace_path, since_iso):
    """Newest kamino_sentinel result recorded after `since_iso`, as (ok_text, err_text).

    Read from the trace rather than the model's reply so the text a human sees is exactly
    what the plugin produced, on both paths. Entries older than this run are ignored so a
    stale result can never be re-delivered as if it were today's.

    The host marks a failed tool call by setting `error_reason`. That is the discriminator,
    not the text: the failure message deliberately contains the token NO-POSITION inside
    the sentence denying it, so no amount of string matching on the body can be trusted to
    tell the two apart. Successes and failures are therefore kept in separate slots and
    never fall through to one another.
    """
    ok_best = None
    err_best = None
    try:
        with open(trace_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or TOOL_NAME not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("message") != "tool_call_result":
                    continue
                attrs = row.get("attributes") or {}
                if attrs.get("tool") != TOOL_NAME:
                    continue
                ts = row.get("@timestamp", "")
                if ts <= since_iso:
                    continue

                reason = attrs.get("error_reason")
                if reason and str(reason).strip():
                    if err_best is None or ts >= err_best[0]:
                        err_best = (ts, str(reason).strip())
                    continue

                out = attrs.get("output")
                if isinstance(out, str) and out.strip():
                    if ok_best is None or ts >= ok_best[0]:
                        ok_best = (ts, out.strip())
    except OSError:
        return None, None
    return (ok_best[1] if ok_best else None), (err_best[1] if err_best else None)


def tool_output_since(trace_path, since_iso):
    """Successful tool output only. Kept as the narrow form of `scan_trace`."""
    return scan_trace(trace_path, since_iso)[0]


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
    """Run the sentinel once. Returns (report_text, ok).

    Starts the agent, watches the trace, and stops as soon as the tool result lands.
    The model's own reply is only a fallback for hosts that do not write a trace, and
    it must still carry a verdict to be accepted.
    """
    started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    proc = subprocess.Popen(
        [args.bin, "agent", "--agent", args.agent, "-m", build_prompt(args.wallet)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)

    deadline = time.monotonic() + args.deadline
    found = None
    failed = None
    while time.monotonic() < deadline:
        ok_text, err_text = scan_trace(args.trace, started)
        if has_verdict(ok_text):
            found = ok_text
            break
        if err_text:
            # A failure is a result. Stop here instead of letting the model spend the
            # remaining budget narrating it, which is what used to happen.
            failed = err_text
            break
        if proc.poll() is not None:      # agent exited on its own
            break
        time.sleep(1.0)

    if proc.poll() is None:
        # Everything after the tool call is the model narrating. Stop paying for it.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    if found:
        return found, True

    if failed:
        return failed, False

    # No tool result. Fall back to whatever the agent printed, if it carries a verdict.
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = "", ""
    text = (out or "").strip()
    if has_verdict(text):
        return text, True
    detail = text or (err or "").strip()
    return ("no verdict recovered from trace or agent output within %ds\n%s"
            % (args.deadline, detail[:300])), False


def main():
    args = parse_args()
    report, ok = produce(args)

    if not ok:
        detail = report[:500] if report else "(no output)"
        deliver(args, "%s\n\n%s" % (UNKNOWN_PREFIX, detail))
        # Print the reason, not just the fact. The scheduler stores stdout/stderr with the
        # run, so this is the only place a human debugging a 3 AM failure will find out
        # what actually broke without going back to the raw trace.
        print("sentinel run failed; UNKNOWN alert dispatched", file=sys.stderr)
        print(detail, file=sys.stderr)
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
