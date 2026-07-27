//! kamino-sentinel — ZeroClaw tool plugin watching Kamino lending positions.
//! Pure logic lives in `sentinel.rs` (natively testable); this file is thin glue.

pub mod sentinel;

#[cfg(target_family = "wasm")]
mod component {
    wit_bindgen::generate!({
        path: "wit/v0",
        world: "tool-plugin",
        features: ["plugins-wit-v0"],
    });

    use std::collections::HashMap;

    use crate::sentinel::{report, Position, SentinelConfig};
    use exports::zeroclaw::plugin::plugin_info::Guest as PluginInfo;
    use exports::zeroclaw::plugin::tool::{Guest as Tool, ToolResult};

    const MARKET: &str = "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF";
    const API: &str = "https://api.kamino.finance";

    struct Sentinel;

    #[derive(serde::Deserialize)]
    struct ExecuteArgs {
        #[serde(default)]
        wallet: String,
        #[serde(rename = "__config", default)]
        config: HashMap<String, String>,
    }

    /// 한 번의 조회. 실패가 재시도할 가치가 있는지(`retryable`)를 함께 돌려준다.
    fn http_get_json_once(url: &str) -> Result<serde_json::Value, (String, bool)> {
        let resp = waki::Client::new()
            .get(url)
            .connect_timeout(std::time::Duration::from_secs(10))
            .send()
            // 연결 자체가 안 된 것은 대개 일시적이다(타임아웃·DNS·리셋).
            .map_err(|e| (format!("request failed: {e}"), true))?;
        let status = resp.status_code();
        let body = resp
            .body()
            .map_err(|e| (format!("body read failed: {e}"), true))?;
        if status != 200 {
            // 5xx 와 429 는 서버 사정이라 다시 물어볼 값어치가 있다.
            // 4xx(잘못된 지갑 주소 등)는 몇 번을 물어도 같은 답이 온다.
            let retryable = crate::sentinel::is_retryable(status);
            return Err((
                format!(
                    "HTTP {status}: {}",
                    String::from_utf8_lossy(&body[..body.len().min(200)])
                ),
                retryable,
            ));
        }
        serde_json::from_slice(&body).map_err(|e| (format!("json parse failed: {e}"), false))
    }

    /// Kamino API 는 간헐적으로 520(Cloudflare origin error)을 뱉고, 정상 응답도 6초쯤 걸린다.
    /// 한 번 실패했다고 플러그인이 고장난 것처럼 보이면 안 되므로 짧게 물러났다 다시 묻는다.
    /// 그래도 안 되면 "조회 실패"라고 분명히 말한다. **포지션이 없는 것과는 완전히 다른 상태다.**
    fn http_get_json(url: &str) -> Result<serde_json::Value, String> {
        const ATTEMPTS: u32 = 3;
        let mut last = String::new();
        for attempt in 1..=ATTEMPTS {
            match http_get_json_once(url) {
                Ok(v) => return Ok(v),
                Err((e, retryable)) => {
                    last = e;
                    if !retryable || attempt == ATTEMPTS {
                        break;
                    }
                    // 1s, 2s 백오프
                    std::thread::sleep(std::time::Duration::from_secs(attempt as u64));
                }
            }
        }
        Err(format!("{ATTEMPTS}회 시도 후 실패 — {last}"))
    }

    fn num(v: &serde_json::Value) -> f64 {
        match v {
            serde_json::Value::String(s) => s.parse().unwrap_or(0.0),
            serde_json::Value::Number(n) => n.as_f64().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    fn fetch_positions(wallet: &str) -> Result<Vec<Position>, String> {
        let url = format!("{API}/kamino-market/{MARKET}/users/{wallet}/obligations?env=mainnet-beta");
        let v = http_get_json(&url)?;
        let arr = v.as_array().ok_or("unexpected response shape")?;
        Ok(arr
            .iter()
            .map(|ob| {
                let s = &ob["refreshedStats"];
                Position {
                    deposit_usd: num(&s["userTotalDeposit"]),
                    borrow_usd: num(&s["userTotalBorrow"]),
                    ltv_pct: num(&s["loanToValue"]) * 100.0,
                    liquidation_ltv_pct: num(&s["liquidationLtv"]) * 100.0,
                }
            })
            .collect())
    }

    impl PluginInfo for Sentinel {
        fn plugin_name() -> String {
            "kamino-sentinel".to_string()
        }
        fn plugin_version() -> String {
            "0.1.0".to_string()
        }
    }

    impl Tool for Sentinel {
        fn name() -> String {
            "kamino_sentinel".to_string()
        }

        fn description() -> String {
            "Check the health of Kamino (Solana lending) loan positions: LTV, liquidation threshold and cushion, with an OK/WARN/DANGER verdict. Uses the operator-configured wallet when no wallet argument is given, so it can run unattended on a schedule.".to_string()
        }

        fn parameters_schema() -> String {
            serde_json::json!({
                "type": "object",
                "properties": {
                    "wallet": {
                        "type": "string",
                        "description": "Solana wallet address to check. Omit to use the operator-configured default wallet."
                    }
                },
                "required": []
            })
            .to_string()
        }

        fn execute(args: String) -> Result<ToolResult, String> {
            let parsed: ExecuteArgs = match serde_json::from_str(&args) {
                Ok(a) => a,
                Err(e) => {
                    return Ok(ToolResult {
                        success: false,
                        output: String::new(),
                        error: Some(format!("invalid arguments: {e}")),
                    });
                }
            };
            let cfg = SentinelConfig::from_section(&parsed.config);
            let wallet = if parsed.wallet.trim().is_empty() {
                cfg.wallet.clone()
            } else {
                parsed.wallet.trim().to_string()
            };
            if wallet.is_empty() {
                return Ok(ToolResult {
                    success: false,
                    output: String::new(),
                    error: Some(
                        "no wallet: pass a wallet argument or configure plugins.entries.kamino-sentinel.config.wallet"
                            .to_string(),
                    ),
                });
            }
            match fetch_positions(&wallet) {
                Ok(positions) => Ok(ToolResult {
                    success: true,
                    output: report(&wallet, &positions, &cfg),
                    error: None,
                }),
                // 조회 실패를 "포지션 없음"으로 오해하면 안 된다. 담보가 위험한데 조용한 것이
                // 이 도구에서 제일 나쁜 실패 모드라, 상태를 명시적으로 구분해 알린다.
                //
                // ⚠️ 안내문은 반드시 `error` 에 담는다. 호스트가 실패를 다룰 때
                //    `r.error.unwrap_or_else(|| r.output)` 로 **error 를 우선**하고 output 은 버린다.
                //    (zeroclaw runtime/agent/tool_execution.rs) output 에만 쓰면 사용자에게 안 간다.
                Err(e) => {
                    let msg = format!(
                        "[UNKNOWN] Kamino 조회 실패 — {wallet}\n\
                         포지션 상태를 확인하지 못했습니다. 이것은 NO-POSITION(포지션 없음)이 아닙니다.\n\
                         담보가 위험한 상태일 수도 있으니 직접 확인하세요.\n\
                         원인: {e}"
                    );
                    Ok(ToolResult {
                        success: false,
                        output: msg.clone(),
                        error: Some(msg),
                    })
                }
            }
        }
    }

    export!(Sentinel);
}
