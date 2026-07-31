# 데모 영상 촬영 순서 (목표 2:50, 상한 3:00)

바운티 요구: *"A video, 3 minutes or less: real agent, real channel, your use case doing
the thing. No slides. Terminal + phone is perfect."* → **슬라이드 금지. 터미널 + 폰만.**

## 언제 찍을까

`cron_runs` 테이블이 **증거 샷의 핵심**인데 지금 2줄뿐이다(7/29 맥미니 이관 때 DB가 새로 생김).
하루 지날 때마다 한 줄씩 는다.

| 촬영일 | 표에 찍히는 줄 수 |
|---|---|
| 오늘(7/31) | 2 |
| 8/2 | 4 |
| **8/4** | **6** ← 권장 |
| 8/6 | 8 (마감 직전이라 위험) |

**8/3~8/5 아침 8시 이후 촬영 권장.** 표가 길수록 "매일 돌린다"가 그림으로 증명된다.
그 전날 밤에 리허설 1회 해둘 것.

## 준비물

- **터미널**: 맥미니 SSH 세션. 폰트 크게(18pt+), 창 넓게. `clear` 자주.
- **폰**: 텔레그램 `@Zeroclaw126_bot` 대화방 열어두기. 알림 소리 켜기(도착 순간이 그림).
- **맥북 터미널** 별도 하나 — `cargo test`용(맥미니엔 cargo 없음).
- 녹화: 화면 녹화 + 폰은 카메라로 잡거나 미러링. **폰 실물 화면이 더 설득력 있다.**

⚠️ **리허설 필수**: qwen이 가끔 빈 응답을 뱉는다. 본 촬영 전 1회 돌려서 확인.

---

## 샷 리스트

### 0:00–0:12 · 콜드 오픈 — 폰
텔레그램 대화방을 **위로 스크롤**해서 매일 도착한 리포트들을 훑는다.
같은 형식의 메시지가 날짜별로 쌓여 있는 화면.

> 나레이션: "This has been arriving every morning at 8 AM since July 22nd.
> I didn't open anything to get it."

**말 없이 스크롤만 해도 됨.** 이 12초가 유즈케이스 30% 배점을 가장 직접 때린다.

### 0:12–0:35 · 무엇을 보는가 — 터미널
리포트 한 줄을 띄워놓고 `cushion`을 가리킨다.

```
[OK] Kamino sentinel — 4DNPMDrqt6UgyApJ12RqqV9KqBboLNgDEsWVmnHvmkqh
- #1: deposit $97.12 | borrow $25.01 | LTV 25.8% -> liq 75.0% (cushion 49.2%p)
```

> "Cushion is the gap between my current LTV and the LTV that liquidates me.
> 49 points today. If it drops under 10 it says WARN, under 5 it says DANGER."

### 0:35–0:50 · 실제 스케줄러 발화 — 터미널
**손으로 스크립트를 돌리지 않는다.** 진짜 스케줄러를 쓴다.

```sh
zeroclaw cron once 1m \
  'python3 /Users/shud/kamino-sentinel/contrib/daily_report.py \
     --bin /Users/shud/zeroclaw-bin/zeroclaw --recipient <CHAT_ID>' \
  --agent spike
```

> "That's the same scheduler that runs the 8 AM job. One-shot, fires in a minute."

### 0:50–1:30 · 기다리는 동안 — 커스터디 (인서트)
**리포트 생성에 ~35초 걸린다. 빈 화면으로 버리지 말고 이 구간에 커스터디를 넣는다.**
배속 조작 없이 실제 대기 시간을 내용으로 채우는 것 = 정직하고 편집도 깔끔.

```sh
cat dist/kamino-sentinel/manifest.toml
```
→ `permissions = ["http_client", "config_read"]` 를 가리킨다.

```sh
grep -A6 '^\[dependencies\]' Cargo.toml
```
→ serde / serde_json / wit-bindgen / waki 넷뿐.

> "Custody tier T0. It reads. There is no signing key, no RPC key —
> Kamino's public API doesn't take one. There is no code path that can move funds."

### 1:30–1:45 · 도착 — 폰
알림음 → 폰 화면에 리포트 도착. **터미널과 폰을 한 프레임에** 잡으면 최고.

> "There it is. Same text the tool produced — the model doesn't get to rewrite it."

### 1:45–2:15 · 실패는 어떻게 보이는가 — 터미널
이게 다른 제출물과 갈리는 지점이다. **고장난 모습을 일부러 보여준다.**

```sh
zeroclaw agent --agent spike -m \
  "Call the kamino_sentinel tool with the wallet argument set exactly to INVALIDWALLET123."
```

트레이스에서 결과를 꺼내 보여준다:

```
[UNKNOWN] Kamino lookup failed — INVALIDWALLET123
Could not read the position. This is NOT a no-position result.
```

> "A failed lookup is not 'you have no position'. Those are different states, and for a
> watchdog, confusing them is the worst thing it can do. It fails loud, and the job exits
> non-zero so the scheduler records a failure too."

⚠️ 이 샷은 에이전트가 타임아웃될 수 있다(7b가 계속 떠듦). **트레이스에 결과가 뜨는 즉시 컷.**

### 2:15–2:40 · 매일 돈다는 증거 — 터미널
```sh
sqlite3 -header -column ~/.zeroclaw/data/cron/jobs.db \
  "SELECT started_at, status, duration_ms FROM cron_runs ORDER BY started_at DESC LIMIT 8;"
```

> "Every row is a real 8 AM run. And `ok` here means delivered, not just executed —
> the script exits non-zero if the Telegram send fails. That distinction cost me six days."

### 2:40–2:52 · 재현 — 맥북 터미널
```sh
cargo test
```
→ `10 passed` 가 뜨는 화면.

> "Pure logic, ten tests, no network. Clone it, install the component, set your wallet
> and two thresholds. That's the whole config."

### 2:52–3:00 · 마무리
화면에 레포 주소만.

```
github.com/shud26/kamino-sentinel
```

---

## 찍지 말 것

- 슬라이드·타이틀 카드·발표 화면 (요구사항이 명시적으로 금지)
- 지갑 주소 외 개인정보. **텔레그램 대화방의 다른 채팅이 보이지 않게** 할 것
- 봇 토큰, `.env`, `config.toml`의 시크릿 필드가 스치는 화면
- 배속 조작으로 35초를 3초처럼 보이게 하는 것 (인서트로 채우는 게 정직하고 더 나음)

## 대사가 막히면

영어 나레이션이 부담이면 **자막으로 대체해도 된다.** 요구는 "3분 안에 이해되게"이지
영어 발표가 아니다. 터미널 조작 + 폰 도착 + 짧은 자막이면 충분하다.
