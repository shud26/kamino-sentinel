# Discord showcase post — #solana-bounty

Post as a sequence of messages (each part is under Discord's 2,000-character limit).
Replace `<VIDEO LINK>` before posting. Nothing else needs editing.

---

## PART 1 — the hook

**kamino-sentinel — a lending-liquidation watchdog that has been waking me up at 08:00 every day since 2026-07-22**

🎥 <VIDEO LINK> (2:5x)
📦 https://github.com/shud26/kamino-sentinel (MIT)

**What it does.** Every morning at 08:00 KST an unattended ZeroClaw cron run reads my Kamino
obligations from Solana mainnet, computes the **cushion** — the percentage points between my
current LTV and my liquidation LTV — and pushes one line to Telegram:

```
[OK] Kamino sentinel — 4DNPMDrqt6UgyApJ12RqqV9KqBboLNgDEsWVmnHvmkqh
- #1: deposit $97.12 | borrow $25.01 | LTV 25.8% -> liq 75.0% (cushion 49.2%p)
```

Verdicts are `OK / WARN / DANGER / NO-POSITION`, thresholds are mine to configure, and a
`DANGER` line appends "repay or add collateral NOW".

**Who it's for.** Anyone carrying a Kamino borrow who does not want to remember to look.
Lending positions fail quietly — collateral drifts down, interest accrues, one volatile night
does the rest. A dashboard needs you to open it. A sentinel assumes you won't.

**Am I running it?** Yes, against a real wallet with a real borrow, unattended, on an
always-on Mac mini. Not a demo run for this post. The run history is in the repo, including
the six days it was quietly broken.

---

## PART 2 — custody & threat model

**Custody tier: T0 (Read).** It reads. It holds **no keys of any kind** — no signing key, and
not even an RPC key, because Kamino's public REST API takes no authentication. There is no code
path that builds, signs, or submits a transaction; the dependency list is `serde`, `serde_json`,
`wit-bindgen`, `waki`. Capability surface is exactly `permissions = ["http_client", "config_read"]`.
The only operator data it touches is a wallet **address** (public on-chain data) and two numbers.

Because it cannot move funds, the prompt-injection-into-payment scenario doesn't apply. What
*does* exist, stated rather than skipped:

• **Injection can change what gets reported, not what gets moved.** The `wallet` argument is
model-supplied, so a hostile message could make the agent query a stranger's address. Result: a
confusing report, not a loss. The scheduled path avoids it structurally — the cron passes no
argument, so the config wallet is used and my address never enters a prompt.

• **Injection cannot rewrite or suppress the report.** The delivery script takes the report from
the **tool result in the runtime trace**, not from the model's reply. No amount of persuasion in
the model's context can soften a `DANGER` line, and a run with no verdict sends an explicit
`[UNKNOWN]` alert instead of silence. The delivered text is not something the model gets a vote on.

• **One third party is trusted: Kamino's API.** Wrong numbers upstream mean a wrong verdict. The
mitigation is limited to failing loudly when it's unreachable rather than degrading to
`NO-POSITION` — a fetch failure must never look like "you have no position".

• **Limits.** It's a once-a-day cron, so its worst case is a position that goes from healthy to
liquidated between two runs. It reports; it cannot intervene.

---

## PART 3 — why a plugin and not a skill (correct layering)

Fair challenge: this fetches one HTTP endpoint, and the bounty explicitly rejects "thin
single-RPC-call wrappers padded into WASM". So what earns the compile step here is not the
fetch — it's the decision layer.

**1. The verdict must not be model-generated, and I have the receipts.** Asked to relay the exact
tool output, a 14B model called the tool correctly and then paraphrased the result into a friendly
summary *with the `[OK]` verdict removed*. A 7B model looped on the call until the circuit breaker
stopped it. If `OK/WARN/DANGER` lived in a skill's instructions, the alert's reliability would be a
function of which model I could afford to run that month. In the plugin it's `classify()`, pinned
by unit tests, byte-identical every run. A watchdog whose verdict depends on model temperament is
not a watchdog.

**2. Output shaping** (bounty trap #3). The obligations response for a *single* position is
**17.5 KB of JSON ≈ 4,400 tokens** — full per-reserve arrays, 13 `refreshedStats` fields, addresses,
tags. The plugin returns three lines, about 40 tokens. A skill wrapping `http_request` would push
the whole document through the context window on every scheduled call, every day, and bill me for it.

**3. The arithmetic has edge cases worth testing** — cushion computation, `warn >= danger`
enforcement against malformed config, and the deposit-only case (no borrow → no liquidation → `OK`
regardless of thresholds). That belongs in tests, not in prose an LLM interprets.

The HTTP call really is tier-1 shaped. The **classifier** is what's compiled, because an alerting
tool should be deterministic.

---

## PART 4 — craft, and the bug that ran for six days

**Structure.** `src/sentinel.rs` is pure logic — config parsing, classification, report rendering.
No wasm, no HTTP, **10 unit tests** run natively. `src/lib.rs` is a thin
`#[cfg(target_family = "wasm")]` shim: WIT bindings, the Kamino fetch over `waki`, arg handling.
This is the reference layout the plugin guide asks for.

**ZeroClaw features used:** tool plugin (wit/v0), `http_client` + `config_read` permissions, config
injection via `__config`, the cron scheduler, a Telegram channel, and the runtime trace as a data
source.

**What I had to build:** the plugin, plus `contrib/daily_report.py` — because the obvious cron setup
does not work, which is the most useful thing I learned.

**A scheduled agent does not get the channel registry.** `send_via` inside a cron prompt fails with
`Channel 'telegram.default' not found. Available: []` — **while the cron records `ok`**. The report
is generated, nothing is delivered, the status is green. Binding the channel to the agent in config
does *not* fix it; I tried that first and re-verified with a one-shot scheduled run. It ran that way
for **six days**. I found it by checking the chat's `message_id`, which was 3 — at most two messages
had ever arrived.

The fix splits the steps: `zeroclaw agent -m` produces, `zeroclaw channel send` delivers, and the
script exits non-zero on failure so a green scheduler status finally *means* something. It's Python
because the default risk profile allowlists `python3` but not `sh` — no policy loosening needed.

---

## PART 5 — the same bug, again, inside my own fix

Then it happened **again, to me, inside my own fix**: the script tested for verdict tokens as
substrings anywhere in the text, and the plugin's fetch-failure message contains the token
`NO-POSITION` inside the sentence that *denies* it. So a Kamino outage would have been delivered as
a clean verdict and exited 0. Found by calling the tool with an invalid wallet and reading the trace.
Fixed by anchoring to the bracketed tag at the start of line 1 and skipping trace entries carrying
`error_reason`. Pinned by `contrib/test_daily_report.py`, whose failure fixture is the real trace
string.

This project's thesis is that a green status is not evidence. It shipped code that made a green
status out of a failure — twice. Writing the rule down does not exempt you from it.

---

## PART 6 — reproduce it in an evening

Host must be source-built — the plugin host is not in the release binaries:
`cargo build --release --features plugins-wasm,plugins-wasm-cranelift`

```sh
git clone https://github.com/shud26/kamino-sentinel && cd kamino-sentinel
cargo test                                    # 10 tests, native, no network
zeroclaw plugin install ./dist/kamino-sentinel   # prebuilt component ships in dist/
```

`~/.zeroclaw/config.toml` (secrets redacted — this is the whole config):

```toml
[plugins.entries.kamino-sentinel.config]
wallet = "YOUR_SOLANA_WALLET"
warn_cushion = "10"     # %p — WARN at or below
danger_cushion = "5"    # %p — DANGER at or below
```

```sh
zeroclaw cron add '0 8 * * *' \
  'python3 /path/to/kamino-sentinel/contrib/daily_report.py --recipient <YOUR_CHAT_ID>' \
  --tz Asia/Seoul --agent <agent>
```

**Gotchas you'd otherwise hit** (all in the README):
• `zeroclaw config set` prompts on a tty — on a headless box edit config.toml directly, then
re-validate it parses. The CLI re-serializes config and scatters hand edits.
• The scheduler kills a shell cron job at **120 s** (`SHELL_JOB_TIMEOUT_SECS`, hardcoded). A 14B
model returned the tool result in 74 s then narrated for five more minutes → killed with nothing
delivered. The script stops the agent the moment the tool result lands: **361 s → 35 s**.
• Release binaries are portable between Apple Silicon Macs on the same OS — moving hosts needed no
rebuild, only the binary, `dist/`, and the config.
• Verify delivery, not job status. Per-run history lives in the `cron_runs` table of
`~/.zeroclaw/data/cron/jobs.db`; `zeroclaw cron list` only keeps the last result.

No registry PR opened, per the bounty rules. Happy to answer anything here.
