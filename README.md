# kamino-sentinel

A ZeroClaw tool plugin that watches your [Kamino](https://kamino.finance) (Solana lending) positions and answers one question every day before you've had coffee: **am I anywhere near liquidation?**

```
[WARN] Kamino sentinel — 9xQe...k3Fp
! #1: deposit $1042.55 | borrow $612.30 | LTV 58.7% -> liq 65.0% (cushion 6.3%p)
```

It reports LTV, liquidation threshold, and the **cushion** (percentage points between your current LTV and the liquidation LTV) for every obligation in the wallet, with an overall `OK / WARN / DANGER / NO-POSITION` verdict. Paired with ZeroClaw's cron scheduler it runs unattended, and it can push the report to a Telegram channel (see [Field log](#field-log) for what that delivery path actually did in practice).

Built for the Superteam bounty *"Build Solana-native plugins for ZeroClaw"*, and run on a daily unattended cron while it was being built (see [Field log](#field-log)).

## Why a sentinel, not a dashboard

Lending positions fail quietly: collateral drifts down, a borrow accrues interest, and one volatile night does the rest. A dashboard requires you to remember to look. A sentinel assumes you won't — it decides `OK / WARN / DANGER` itself using thresholds you configure, and only demands attention when the cushion is thin:

- `OK` — cushion above your warn threshold (default 10 %p), or a deposit-only position (cannot be liquidated)
- `WARN` — cushion at or below `warn_cushion`
- `DANGER` — cushion at or below `danger_cushion` (default 5 %p); the report appends a "repay or add collateral NOW" line
- `NO-POSITION` — wallet has no obligations in the market

## Design

```
src/sentinel.rs   pure logic: config parsing, classification, report rendering
                  — no wasm, no HTTP, natively testable (7 unit tests)
src/lib.rs        thin glue: WIT bindings, Kamino REST fetch (waki), arg handling
```

Decisions worth stating:

- **Stateless by contract.** ZeroClaw tool plugins get a fresh store per call, so the plugin holds no state at all; every invocation fetches live data from Kamino's public API (`/kamino-market/{market}/users/{wallet}/obligations`).
- **Config-fallback wallet.** The `wallet` argument is optional; when omitted, the operator-configured wallet is used. This is what makes an *unattended* cron run possible — the model doesn't need to know your address, and your address never appears in a prompt.
- **Fail soft, fail loud.** Malformed config values fall back to safe defaults (`warn >= danger` is enforced); HTTP/parse failures return a proper tool error instead of a fake verdict.

## Build

Requires Rust with the `wasm32-wasip2` target (`rustup target add wasm32-wasip2`).

```sh
cargo test                                        # pure-logic tests, native
cargo build --release --target wasm32-wasip2      # the component
cp target/wasm32-wasip2/release/kamino_sentinel.wasm dist/kamino-sentinel/kamino-sentinel.wasm
```

A prebuilt component ships in `dist/kamino-sentinel/` next to its `manifest.toml`:

```toml
name = "kamino-sentinel"
capabilities = ["tool"]
permissions = ["http_client", "config_read"]
```

## Install

Your ZeroClaw host must be built with the WASM plugin features (`cargo build --release --features plugins-wasm,plugins-wasm-cranelift` — the plugin host is not in the prebuilt release binaries).

```sh
zeroclaw plugin install ./dist/kamino-sentinel
```

Then configure the default wallet and thresholds in `~/.zeroclaw/config.toml`:

```toml
[plugins.entries.kamino-sentinel.config]
wallet = "YOUR_SOLANA_WALLET"
warn_cushion = "10"     # %p — WARN at or below this cushion
danger_cushion = "5"    # %p — DANGER at or below this cushion
```

(`zeroclaw config set` prompts on a tty for these fields; on a headless box, edit the file directly and re-validate it parses.)

Ad-hoc use from any agent chat:

> "Check my Kamino health" → the model calls `kamino_sentinel`, optionally with an explicit `wallet`.

## Unattended daily ping (cron + Telegram)

With a Telegram channel bound in ZeroClaw:

```sh
zeroclaw cron add "0 8 * * *" --tz Asia/Seoul \
  --agent spike --uses-memory false \
  "Call the kamino_sentinel tool with no arguments. Then use the send_via tool to send the exact report text to the telegram channel, recipient <YOUR_CHAT_ID>. Do not add commentary."
```

**This does not deliver anything, and the cron will still report `ok`.** Read on before using it.

### Why the obvious setup silently fails

An agent spawned by the cron scheduler does not get the channel registry attached. `send_via`
inside a scheduled prompt fails with

```
Channel 'telegram.default' not found. Available: []
```

while the run is still recorded as `ok` with the report as its output. The report is generated
and logged, and nothing is delivered.

The channel is not the problem: `zeroclaw channel doctor` reports `healthy`, `zeroclaw channel send`
works, and a direct call to the Telegram API for the same bot and chat succeeds. **Binding the channel
to the agent in config does not fix it either** — that was tried and re-verified with a one-shot run,
and the scheduled turn still saw `Available: []`. The binding applies to the gateway/CLI context, not
to scheduler-spawned turns (`Registered 2 channel(s) for CLI agent` appears for a CLI run and never
for a scheduled one).

### What does work

Split the two steps and use the CLI paths that do have channel access. `contrib/daily_report.py`
does exactly that: it runs the sentinel once via `zeroclaw agent -m`, takes the report from the
**tool result recorded in the runtime trace** rather than from the model's reply, and delivers it
with `zeroclaw channel send`.

Reading the trace instead of the model's answer is not fussiness. Asking a model to "reply with the
exact tool output" holds on a large model and breaks on a small one: a 14B model called the tool
correctly and then paraphrased the result into a friendly summary with the `[OK]` verdict dropped,
and a 7B model looped on the call until the host's circuit breaker stopped the run. Taking the tool
result directly makes the delivered text byte-identical to what the plugin produced, and makes the
whole thing work on whatever model the host can afford.

The script also stops the agent the moment that result appears, because everything after the tool
call is the model narrating. That is what keeps the job inside the scheduler's budget — see below.

```sh
zeroclaw cron add '0 8 * * *' \
  'python3 /path/to/kamino-sentinel/contrib/daily_report.py --recipient <YOUR_CHAT_ID>' \
  --tz Asia/Seoul --agent <agent>
```

It is Python rather than shell because ZeroClaw's default risk profile allowlists `python3` for
scheduled commands but not `sh`, so this needs no loosening of the host security policy.

**Mind the 120 second budget.** ZeroClaw kills a shell cron job at 120 s
(`SHELL_JOB_TIMEOUT_SECS` in `crates/zeroclaw-runtime/src/cron/scheduler.rs`, not configurable).
On a small always-on box that is easy to blow: a 14B local model returned the tool result in 74 s
and then spent another five minutes talking, so the run was killed at 120 s with nothing delivered
even though the report had been ready the whole time. Stopping at the tool result cuts the same job
to **35 s** on a 7B model, with delivery included. Tune `--deadline` (default 95 s) if your host is
slower.

Its failure policy matters more than the happy path. If the run fails, times out, or returns text
without a recognised verdict, it delivers an explicit `[UNKNOWN] ... This is NOT a NO-POSITION result`
alert **and** exits non-zero so the scheduler records a failure too. Silence and a green status are
both wrong answers for a watchdog. Both paths are exercised before shipping:

```
$ python3 contrib/daily_report.py --recipient <id>
[OK] Kamino sentinel — 4DNPMDrqt6…
- #1: deposit $94.30 | borrow $25.00 | LTV 26.5% -> liq 75.0% (cushion 48.5%p)
exit 0

$ python3 contrib/daily_report.py --recipient <id> --agent does-not-exist
sentinel run failed; UNKNOWN alert dispatched
exit 1
```

Verify the scheduled path itself rather than assuming, because a green cron status does not mean
the message arrived:

```sh
zeroclaw cron once 1m '<same command>' --agent <agent>
```

Then confirm the chat's `message_id` advanced. The one-shot is deliberate — a recurring job would
make you wait until tomorrow — but note that ZeroClaw **deletes a one-shot job and its run history
once it fires**, so the scheduler keeps no record of it afterwards and `message_id` is the only
evidence left.

For the recurring job, the per-run history does persist, though not anywhere the CLI will show you:
`zeroclaw cron list` prints only the most recent result. The rest is in SQLite.

```sh
sqlite3 -header -column ~/.zeroclaw/data/cron/jobs.db \
  "SELECT started_at, status, duration_ms FROM cron_runs ORDER BY started_at DESC LIMIT 10;"
```

With this script as the command, that history is worth something. `status` is `ok` only when the
command exited 0 (the `output` column carries the raw `status=exit status: 0`), and the script only
exits 0 after `zeroclaw channel send` succeeded. A row reading `ok` is a delivery receipt, not just
proof that something ran — which is precisely what the same column did **not** mean back when the
job was an agent prompt calling `send_via`.

## Field log

This plugin is dogfooded. A launchd-managed ZeroClaw daemon on a Mac has been firing the 08:00 KST cron since 2026-07-22.

Being precise about what that log shows, because it changed twice and because one earlier version of
this section claimed more than the evidence supported:

- **2026-07-22 to 07-26** — the cron ran daily and reported `NO-POSITION`. This exercised the scheduler, the config-fallback wallet path and the API round-trip, but not the LTV/cushion math against live numbers.
- **from 2026-07-26** — pointed at a wallet holding an actual Kamino obligation, so the daily report carries real deposit/borrow/LTV/cushion values.
- **2026-07-28 — the Telegram delivery step was found to have never worked from the cron.** Every scheduled run generated the correct report and recorded `ok`, and `send_via` failed underneath with `Channel 'telegram.default' not found. Available: []`. The channel itself was fine: `zeroclaw channel doctor` reported `healthy`, and a direct Telegram API call to the same bot and chat succeeded. The `message_id` returned by that direct call was 3, so at most two messages had ever reached the chat — inconsistent with the daily delivery an earlier version of this section claimed.
- **2026-07-28 — fixed, and the fix is not the obvious one.** Binding the channel to the agent in config was tried first and re-verified with a one-shot scheduled run; the scheduled turn still saw `Available: []`. Delivery now runs through `contrib/daily_report.py`, which produces the report with `zeroclaw agent -m` and delivers it with `zeroclaw channel send`. Verified end to end through the scheduler, not just by hand: a one-shot cron run at 14:22 UTC produced the report and the next `message_id` in the chat was 10, with 9 being the message that run delivered.
- **2026-07-30 and 07-31 — two consecutive unattended days on the new host, both delivered.** These are the first scheduled runs where a green status is evidence rather than decoration: `daily_report.py` exits non-zero when delivery fails, so the scheduler recording `exit status: 0` *is* the delivery receipt. `zeroclaw cron list` only keeps the most recent result; the per-run history lives in the `cron_runs` table of `~/.zeroclaw/data/cron/jobs.db`:

  ```
  started_at (UTC)                  status  duration_ms
  2026-07-29T23:00:02+00:00         ok      32827
  2026-07-30T23:00:01+00:00         ok      36695
  ```

  Both are 08:00 KST starts on the always-on box, and the 33–37 s durations are where the timing quoted below comes from.

**So what this field log establishes:** the scheduler fires, the plugin runs unattended against a live wallet, the report is correct, and since 2026-07-28 it actually arrives — on hardware that does not sleep, for every scheduled run since the move. For the six days before that, the unattended *notification* half was broken and looked healthy.

Two more limits worth stating rather than hiding:

- **A green cron status is not delivery.** The run that failed to send is recorded `ok` with the report as its output. Anything built on top of this should check the delivery result, not the job status. Ironically this is the same failure mode the plugin itself is written to avoid: a fetch failure must not be reported as a clean verdict, which is why a failed fetch returns a tool error rather than `NO-POSITION`.
- **A laptop is the wrong host, and it has been moved off one.** The 2026-07-28 run started on time at 08:08 KST and took 2 h 17 min to finish because the machine slept mid-run. On 2026-07-29 the whole setup moved to an always-on Mac mini (M2, 16 GB): the release binary is portable between Apple Silicon Macs as long as the OS matches, so no rebuild was needed, only the binary, the plugin `dist/`, and the config. The smaller box cannot host a 30B model, which is what surfaced both the paraphrasing problem and the 120 s budget above; it now runs `qwen2.5:7b` and the daily job finishes in about 35 s. Verified through the scheduler on the new host, again by `message_id`.

Live output against that obligation:

```
[OK] Kamino sentinel — 4DNPMDrqt6UgyApJ12RqqV9KqBboLNgDEsWVmnHvmkqh
- #1: deposit $97.46 | borrow $25.00 | LTV 25.7% -> liq 75.0% (cushion 49.3%p)
```

The position is deliberately conservative, so it classifies as `OK` and will keep doing so unless SOL drops by roughly two thirds. To confirm the classifier actually fires on live data rather than only in tests, the thresholds were temporarily raised so that the same real position crossed each boundary:

```
warn_cushion=55 danger_cushion=50
[DANGER] Kamino sentinel — 4DNPMDrqt6…
!! #1: deposit $97.46 | borrow $25.00 | LTV 25.6% -> liq 75.0% (cushion 49.4%p)
cushion <= 50.0%p: consider repaying or adding collateral NOW.

warn_cushion=55 danger_cushion=40
Kamino sentinel — 4DNPMDrqt6…
! #1: deposit $97.46 | borrow $25.00 | LTV 25.7% -> liq 75.0% (cushion 49.3%p)
```

Thresholds were restored to `warn_cushion=10 / danger_cushion=5` afterwards. The boundary arithmetic itself is pinned by unit tests in `src/sentinel.rs`.

The build-out (first Rust install → hello component → live-data spike → this sentinel, in one day) is written up on my blog: [shud26.com](https://shud26.com).

## License

MIT
