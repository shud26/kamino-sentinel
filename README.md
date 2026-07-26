# kamino-sentinel

A ZeroClaw tool plugin that watches your [Kamino](https://kamino.finance) (Solana lending) positions and answers one question every day before you've had coffee: **am I anywhere near liquidation?**

```
[WARN] Kamino sentinel — 9xQe...k3Fp
! #1: deposit $1042.55 | borrow $612.30 | LTV 58.7% -> liq 65.0% (cushion 6.3%p)
```

It reports LTV, liquidation threshold, and the **cushion** (percentage points between your current LTV and the liquidation LTV) for every obligation in the wallet, with an overall `OK / WARN / DANGER / NO-POSITION` verdict. Paired with ZeroClaw's cron scheduler and Telegram channel, it runs unattended and pings you every morning.

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

Run the daemon (`zeroclaw daemon` — e.g. under launchd/systemd) and the report lands in Telegram every morning at 08:00.

## Field log

This plugin is dogfooded. A launchd-managed ZeroClaw daemon on a Mac has been firing the 08:00 KST cron since 2026-07-22, with the report delivered over Telegram.

Being precise about what that log shows, because it changed partway through:

- **2026-07-22 to 07-26** — the cron ran daily and reported `NO-POSITION`. This exercised the scheduler, the config-fallback wallet path, the API round-trip and the Telegram delivery, but not the LTV/cushion math against live numbers.
- **from 2026-07-26** — pointed at a wallet holding an actual Kamino obligation, so the daily report carries real deposit/borrow/LTV/cushion values.

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
