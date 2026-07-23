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

    fn http_get_json(url: &str) -> Result<serde_json::Value, String> {
        let resp = waki::Client::new()
            .get(url)
            .connect_timeout(std::time::Duration::from_secs(8))
            .send()
            .map_err(|e| format!("request failed: {e}"))?;
        let status = resp.status_code();
        let body = resp.body().map_err(|e| format!("body read failed: {e}"))?;
        if status != 200 {
            return Err(format!(
                "HTTP {status}: {}",
                String::from_utf8_lossy(&body[..body.len().min(200)])
            ));
        }
        serde_json::from_slice(&body).map_err(|e| format!("json parse failed: {e}"))
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
                Err(e) => Ok(ToolResult { success: false, output: String::new(), error: Some(e) }),
            }
        }
    }

    export!(Sentinel);
}
