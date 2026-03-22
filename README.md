# BetUpset

BetUpset is a cross-platform soccer Dutching scanner with a web dashboard for reviewing opportunities, placing bets, and tracking outcomes across Polymarket and Kalshi.

## What BetUpset Does

BetUpset scans prediction markets on both platforms, groups equivalent events, then looks for three-outcome soccer markets where the cheapest outcome can be rejected and the remaining two outcomes can be covered across platforms at a combined cost below 1.0.

The core strategy is a selective Dutching setup rather than pure three-way arbitrage: the app rejects the least attractive outcome and covers the other two when pricing and safety filters are favorable.

For each surfaced opportunity the app calculates:

- best available prices per outcome
- rejected outcome and reject price
- ROI if either covered outcome wins
- score and win probability estimates
- per-platform stake split and liquidity information

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a local `.env` file from `.env.example` and fill in your credentials and key paths.

## Main Entry Points

### Web app

```bash
python web.py
python web.py --demo
```

The web app exposes:

- `GET /` — frontend dashboard
- `GET /api/scan` and `POST /api/scan` — scan results
- `GET /api/bets` and `POST /api/bets` — tracked bets
- `POST /api/bets/execute` — guarded live execution
- `GET /api/balances` — platform balances
- `POST /api/resolve` — bet auto-resolution

### CLI monitor

```bash
python main.py
python main.py --once
python main.py --demo
```

Continuously scans, detects Dutching opportunities, records them, and emits alerts. Use `--once` for a single pass, or `--demo` with synthetic data.

## Configuration

All runtime settings live in `config.yaml`. The config loader (`config.py`) substitutes `${ENV_VAR}` references with actual environment values and falls back to sensible defaults if the file is missing.

### Strategy

Controls when the scanner considers an opportunity worth surfacing.

| Key | Default | Description |
|-----|---------|-------------|
| `strategy.min_gap` | `0.03` | Minimum price gap (3 cents) to consider a trade |
| `strategy.max_reject_prob` | `0.25` | Never reject an outcome above 15% implied probability |
| `strategy.safety_factor` | `0.60` | Only enter if reject probability < gap × safety factor |
| `strategy.bet_fraction` | `0.10` | Risk 10% of current bankroll per trade |

### Risk Limits

Hard caps that prevent over-exposure regardless of what the strategy suggests.

| Key | Default | Description |
|-----|---------|-------------|
| `risk.max_exposure_per_match` | `50` | Max USD on any single match |
| `risk.max_total_exposure` | `3000` | Max USD across all open trades |
| `risk.stop_loss_pct` | `0.20` | Pause trading if bankroll drops 20% from starting value |
| `risk.max_matchday_exposure_pct` | `0.15` | Max 15% of bankroll on any single matchday |

### Bankroll

| Key | Default | Description |
|-----|---------|-------------|
| `bankroll.starting` | `10000` | Starting bankroll in USD used for position sizing |

### Scanner

| Key | Default | Description |
|-----|---------|-------------|
| `scanner.interval_seconds` | `60` | Seconds between scan cycles |
| `scanner.leagues` | See below | List of league slugs to scan |

Default leagues: `premier-league`, `la-liga`, `champions-league`, `serie-a`, `bundesliga`, `ligue-1`.

### Execution Safeguards

The web execution path enforces these checks server-side so execution safety does not depend on browser-side controls alone.

| Key | Default | Description |
|-----|---------|-------------|
| `execution.dry_run_only` | `false` | Block real order placement, keep simulation mode only |
| `execution.max_stake_per_trade` | `50` | Hard cap (USD) on any single execution attempt |
| `execution.max_scan_age_seconds` | `600` | Reject execution if the scan snapshot is older than this |
| `execution.max_liquidity_fraction` | `0.05` | Max fraction of covered liquidity allowed per platform |

In addition, live per-platform balance verification runs before any order is placed.

### Platforms

Each platform has an `enabled` flag and credential fields that reference environment variables.

```yaml
platforms:
  polymarket:
    enabled: true
    private_key_path: "${POLYMARKET_PEM_PATH}"
  kalshi:
    enabled: true
    api_key_id: "${KALSHI_API_KEY}"
    private_key_path: "${KALSHI_PEM_PATH}"
  azuro:
    enabled: false
  oddsapi:
    enabled: false
```

### Alerts

| Key | Default | Description |
|-----|---------|-------------|
| `alerts.console` | `true` | Print alerts to stdout |
| `alerts.telegram_bot_token` | — | Telegram bot token for push notifications |
| `alerts.telegram_chat_id` | — | Telegram chat ID to send alerts to |

### Output

| Key | Default | Description |
|-----|---------|-------------|
| `output.csv_path` | `opportunities.csv` | CSV file for logged opportunities |
| `output.db_path` | `trades.db` | SQLite database for trade tracking |

## Environment Variables

BetUpset expects credentials to come from the environment rather than from committed source files.

```bash
KALSHI_API_KEY=your-kalshi-api-key
KALSHI_PEM_PATH=kalshi_bet.pem
POLYMARKET_API_KEY=your-polymarket-api-key
POLYMARKET_PEM_PATH=polymarket.pem
APP_PASSWORD=
APP_SESSION_SECRET=change-me-if-you-enable-password-auth
TELEGRAM_TOKEN=              # optional, for alert notifications
TELEGRAM_CHAT_ID=            # optional, for alert notifications
```

If `APP_PASSWORD` is set, the web dashboard requires that password before it will load scans, bets, balances, or resolution actions. The login state is stored in a signed session cookie.

## Project Structure

```text
.
├── web.py                 # FastAPI backend and web entrypoint
├── static/index.html      # Single-page frontend
├── scan_service.py        # Shared scan logic for web/TUI
├── main.py                # CLI monitor loop
├── scanner.py             # Platform scanning pipeline
├── matching.py            # Cross-platform match grouping
├── detector.py            # Arbitrage detection
├── risk.py                # Risk checks and stake rules
├── tracker.py             # SQLite and CSV-backed tracking
├── polymarket_client.py   # Polymarket integration
├── kalshi_client.py       # Kalshi integration
├── config.py              # Typed config loader and defaults
├── config.yaml            # Runtime configuration
└── tests/                 # Unit tests
```

## Testing

```bash
pytest tests -v
```
