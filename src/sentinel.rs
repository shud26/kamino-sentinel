//! Pure logic — no wasm, no HTTP. Natively testable with `cargo test`.

use std::collections::HashMap;

pub const DEFAULT_WARN_CUSHION: f64 = 10.0; // %p to liquidation LTV
pub const DEFAULT_DANGER_CUSHION: f64 = 5.0;

/// Operator config resolved from the plugin's `__config` section.
/// Empty map (unconfigured / no config_read) must produce safe defaults.
pub struct SentinelConfig {
    pub wallet: String,
    pub warn_cushion: f64,
    pub danger_cushion: f64,
}

impl SentinelConfig {
    pub fn from_section(section: &HashMap<String, String>) -> Self {
        let num = |k: &str, d: f64| {
            section
                .get(k)
                .and_then(|v| v.parse::<f64>().ok())
                .filter(|v| v.is_finite() && *v >= 0.0)
                .unwrap_or(d)
        };
        let mut warn = num("warn_cushion", DEFAULT_WARN_CUSHION);
        let danger = num("danger_cushion", DEFAULT_DANGER_CUSHION);
        if warn < danger {
            warn = danger; // warn must trigger at or before danger
        }
        Self {
            wallet: section.get("wallet").cloned().unwrap_or_default(),
            warn_cushion: warn,
            danger_cushion: danger,
        }
    }
}

#[derive(Debug, PartialEq, Clone, Copy)]
pub enum Level {
    Ok,
    Warn,
    Danger,
    NoPosition,
}

impl Level {
    pub fn tag(self) -> &'static str {
        match self {
            Level::Ok => "OK",
            Level::Warn => "WARN",
            Level::Danger => "DANGER",
            Level::NoPosition => "NO-POSITION",
        }
    }
}

/// One obligation's numbers, already parsed out of the API response.
pub struct Position {
    pub deposit_usd: f64,
    pub borrow_usd: f64,
    pub ltv_pct: f64,
    pub liquidation_ltv_pct: f64,
}

impl Position {
    pub fn cushion(&self) -> f64 {
        self.liquidation_ltv_pct - self.ltv_pct
    }

    pub fn classify(&self, cfg: &SentinelConfig) -> Level {
        if self.borrow_usd <= 0.0 {
            return Level::Ok; // deposit-only position cannot be liquidated
        }
        let c = self.cushion();
        if c <= cfg.danger_cushion {
            Level::Danger
        } else if c <= cfg.warn_cushion {
            Level::Warn
        } else {
            Level::Ok
        }
    }
}

/// Overall level = worst position's level.
pub fn overall(positions: &[Position], cfg: &SentinelConfig) -> Level {
    if positions.is_empty() {
        return Level::NoPosition;
    }
    let mut worst = Level::Ok;
    for p in positions {
        let l = p.classify(cfg);
        worst = match (worst, l) {
            (_, Level::Danger) | (Level::Danger, _) => Level::Danger,
            (_, Level::Warn) | (Level::Warn, _) => Level::Warn,
            _ => Level::Ok,
        };
    }
    worst
}

/// Compact report designed to read well both in chat and in a Telegram ping.
pub fn report(wallet: &str, positions: &[Position], cfg: &SentinelConfig) -> String {
    let level = overall(positions, cfg);
    let head = match level {
        Level::NoPosition => format!("[{}] no Kamino obligations for {wallet}", level.tag()),
        _ => format!("[{}] Kamino sentinel — {wallet}", level.tag()),
    };
    let mut lines = vec![head];
    for (i, p) in positions.iter().enumerate() {
        let mark = match p.classify(cfg) {
            Level::Danger => "!!",
            Level::Warn => "!",
            _ => "-",
        };
        lines.push(format!(
            "{mark} #{n}: deposit ${dep:.2} | borrow ${bor:.2} | LTV {ltv:.1}% -> liq {liq:.1}% (cushion {c:.1}%p)",
            n = i + 1,
            dep = p.deposit_usd,
            bor = p.borrow_usd,
            ltv = p.ltv_pct,
            liq = p.liquidation_ltv_pct,
            c = p.cushion(),
        ));
    }
    if matches!(level, Level::Danger) {
        lines.push(format!(
            "cushion <= {:.1}%p: consider repaying or adding collateral NOW.",
            cfg.danger_cushion
        ));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> SentinelConfig {
        SentinelConfig::from_section(&HashMap::new())
    }

    fn pos(ltv: f64, liq: f64, borrow: f64) -> Position {
        Position { deposit_usd: 1000.0, borrow_usd: borrow, ltv_pct: ltv, liquidation_ltv_pct: liq }
    }

    #[test]
    fn empty_config_gives_defaults() {
        let c = cfg();
        assert_eq!(c.warn_cushion, DEFAULT_WARN_CUSHION);
        assert_eq!(c.danger_cushion, DEFAULT_DANGER_CUSHION);
        assert!(c.wallet.is_empty());
    }

    #[test]
    fn classify_levels() {
        let c = cfg();
        assert_eq!(pos(50.0, 75.0, 500.0).classify(&c), Level::Ok); // cushion 25
        assert_eq!(pos(68.0, 75.0, 500.0).classify(&c), Level::Warn); // cushion 7
        assert_eq!(pos(72.0, 75.0, 500.0).classify(&c), Level::Danger); // cushion 3
    }

    #[test]
    fn deposit_only_is_always_ok() {
        let c = cfg();
        assert_eq!(pos(0.0, 75.0, 0.0).classify(&c), Level::Ok);
    }

    #[test]
    fn overall_is_worst() {
        let c = cfg();
        let ps = vec![pos(50.0, 75.0, 500.0), pos(72.0, 75.0, 500.0)];
        assert_eq!(overall(&ps, &c), Level::Danger);
        assert_eq!(overall(&[], &c), Level::NoPosition);
    }

    #[test]
    fn warn_never_below_danger() {
        let mut m = HashMap::new();
        m.insert("warn_cushion".to_string(), "3".to_string());
        m.insert("danger_cushion".to_string(), "8".to_string());
        let c = SentinelConfig::from_section(&m);
        assert!(c.warn_cushion >= c.danger_cushion);
    }

    #[test]
    fn bad_config_values_fall_back() {
        let mut m = HashMap::new();
        m.insert("warn_cushion".to_string(), "banana".to_string());
        m.insert("danger_cushion".to_string(), "-5".to_string());
        let c = SentinelConfig::from_section(&m);
        assert_eq!(c.warn_cushion, DEFAULT_WARN_CUSHION);
        assert_eq!(c.danger_cushion, DEFAULT_DANGER_CUSHION);
    }

    #[test]
    fn report_mentions_danger_action() {
        let c = cfg();
        let r = report("WALLET", &[pos(72.0, 75.0, 500.0)], &c);
        assert!(r.contains("[DANGER]"));
        assert!(r.contains("NOW"));
    }
}

/// 어떤 HTTP 실패를 다시 물어볼 값어치가 있는가.
/// lib.rs 의 재시도 판정과 같은 규칙 (WASM 바인딩 없이 검증하려고 여기 둔다).
pub fn is_retryable(status: u16) -> bool {
    status >= 500 || status == 429
}

#[cfg(test)]
mod retry_tests {
    use super::is_retryable;

    #[test]
    fn server_errors_and_rate_limit_are_retryable() {
        // 실제로 겪은 것: Kamino 가 520(Cloudflare origin error)을 간헐적으로 뱉는다.
        for s in [500, 502, 503, 504, 520, 429] {
            assert!(is_retryable(s), "{s} 는 재시도 대상이어야 한다");
        }
    }

    #[test]
    fn client_errors_are_not_retryable() {
        // 잘못된 지갑 주소 같은 건 몇 번을 물어도 같은 답이 온다.
        for s in [400, 401, 403, 404, 422] {
            assert!(!is_retryable(s), "{s} 는 재시도하면 안 된다");
        }
    }

    #[test]
    fn success_is_not_retryable() {
        assert!(!is_retryable(200));
    }
}
