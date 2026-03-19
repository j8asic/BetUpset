# BetUpset

BetUpset is a cross-platform soccer Dutching scanner with a web dashboard for reviewing opportunities, placing simulated bets, and tracking outcomes across Polymarket and Kalshi.

## Current Product Shape

The repo is organized around a web-first workflow:

- `web.py` serves the FastAPI backend and the single-page frontend in `static/index.html`
- `scan_service.py` contains the shared scan and row-formatting logic used by the web app
- `main.py` runs the scanner as a CLI monitor loop
- historical scripts are kept locally for reference and are intentionally excluded from the public GitHub repo

## What BetUpset Does

BetUpset scans both platforms, groups equivalent events, then looks for three-outcome markets where the cheapest outcome can be rejected and the remaining two outcomes can be covered across platforms at a combined cost below 1.0.

More precisely, the core strategy is a selective Dutching setup rather than pure three-way arbitrage: the app rejects the least attractive outcome and covers the other two outcomes when the pricing and safety filters are favorable.

For each surfaced opportunity the app calculates:

- best available prices per outcome
- rejected outcome and reject price
- ROI if either covered outcome wins
- score and win probability estimates
- per-platform stake split and liquidity information

## Main Entry Points

### Web app

```bash
python web.py
python web.py --demo
```

The web app exposes:

- `GET /` for the frontend
- `GET /api/scan` and `POST /api/scan` for scan results
- `GET /api/bets` and `POST /api/bets` for tracked bets
- `GET /api/balances` for platform balances
- `POST /api/resolve` for bet auto-resolution

### CLI monitor

```bash
python main.py
python main.py --once
python main.py --demo
```

This mode continuously scans, detects Dutching opportunities, records them, and emits alerts.

### Historical tools

Historical scripts are kept locally for reference, but they are not intended to be pushed to the public GitHub repo. That includes the `legacy/` folder and the old root-level `tui.py`.

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

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a local `.env` file from `.env.example` and fill in your credentials and key paths.

## Environment Variables

BetUpset expects credentials to come from the environment rather than from committed source files.

Typical variables:

```bash
KALSHI_API_KEY=your-kalshi-api-key
KALSHI_PEM_PATH=kalshi_bet.pem
POLYMARKET_API_KEY=your-polymarket-api-key
POLYMARKET_PEM_PATH=polymarket.pem
```

## Testing

```bash
pytest tests -v
```

## Local Archive

This workspace also contains local-only historical code, including `legacy/` and `tui.py`. Those files are ignored by `.gitignore` and are not part of the intended public repo contents.
