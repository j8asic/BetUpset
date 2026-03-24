"""
Configuration for the BetUpset Prediction Market Arbitrage Bot.

Loads settings from config.yaml with environment variable substitution.
Falls back to hardcoded defaults if YAML is unavailable.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ============================================================
# DATACLASSES
# ============================================================

@dataclass
class StrategyConfig:
    min_gap: float = 0.03
    max_reject_prob: float = 0.25
    bet_fraction: float = 0.10


@dataclass
class RiskConfig:
    max_exposure_per_match: float = 50.0
    max_total_exposure: float = 3000.0
    max_matchday_exposure_pct: float = 0.15



@dataclass
class ScannerConfig:
    interval_seconds: int = 60


@dataclass
class ExecutionConfig:
    dry_run_only: bool = False
    max_stake_per_trade: float = 50.0
    max_scan_age_seconds: int = 600
    max_liquidity_fraction: float = 0.05


@dataclass
class PlatformConfig:
    enabled: bool = False
    # Platform-specific credential fields (populated from YAML)
    credentials: dict[str, str] = field(default_factory=dict)


@dataclass
class AlertsConfig:
    console: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class OutputConfig:
    csv_path: str = "opportunities.csv"
    db_path: str = "trades.db"


@dataclass
class AppConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


# ============================================================
# YAML LOADING
# ============================================================

def _substitute_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} references with actual environment variable values."""
    def replacer(match):
        var_name = match.group(1)
        return os.environ.get(var_name, "")
    return re.sub(r'\$\{(\w+)\}', replacer, value)


def _walk_and_substitute(obj):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: _walk_and_substitute(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_walk_and_substitute(item) for item in obj]
    return obj


def load_config(path: str = "config.yaml") -> AppConfig:
    """
    Load configuration from YAML file with env var substitution.
    Returns AppConfig with defaults for any missing values.
    """
    config = AppConfig()

    config_path = Path(path)
    if not config_path.exists():
        print(f"[Config] {path} not found, using defaults")
        _set_default_platforms(config)
        return config

    if not HAS_YAML:
        print("[Config] pyyaml not installed, using defaults")
        _set_default_platforms(config)
        return config

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw:
        _set_default_platforms(config)
        return config

    raw = _walk_and_substitute(raw)

    # Strategy
    if "strategy" in raw:
        s = raw["strategy"]
        config.strategy = StrategyConfig(
            min_gap=s.get("min_gap", 0.03),
            max_reject_prob=s.get("max_reject_prob", 0.25),
            bet_fraction=s.get("bet_fraction", 0.10),
        )

    # Risk
    if "risk" in raw:
        r = raw["risk"]
        config.risk = RiskConfig(
            max_exposure_per_match=r.get("max_exposure_per_match", 50),
            max_total_exposure=r.get("max_total_exposure", 3000),
            max_matchday_exposure_pct=r.get("max_matchday_exposure_pct", 0.15),
        )

    # Scanner
    if "scanner" in raw:
        sc = raw["scanner"]
        config.scanner = ScannerConfig(
            interval_seconds=sc.get("interval_seconds", 60),
        )

    # Execution
    if "execution" in raw:
        e = raw["execution"]
        config.execution = ExecutionConfig(
            dry_run_only=e.get("dry_run_only", False),
            max_stake_per_trade=e.get("max_stake_per_trade", 50.0),
            max_scan_age_seconds=e.get("max_scan_age_seconds", 600),
            max_liquidity_fraction=e.get("max_liquidity_fraction", 0.05),
        )

    # Platforms
    if "platforms" in raw:
        for name, pdata in raw["platforms"].items():
            if isinstance(pdata, dict):
                enabled = pdata.get("enabled", False)
                creds = {k: v for k, v in pdata.items() if k != "enabled"}
                config.platforms[name] = PlatformConfig(
                    enabled=enabled,
                    credentials=creds,
                )
    _set_default_platforms(config)

    # Alerts
    if "alerts" in raw:
        a = raw["alerts"]
        config.alerts = AlertsConfig(
            console=a.get("console", True),
            telegram_bot_token=a.get("telegram_bot_token", ""),
            telegram_chat_id=a.get("telegram_chat_id", ""),
        )

    # Output
    if "output" in raw:
        o = raw["output"]
        config.output = OutputConfig(
            csv_path=o.get("csv_path", "opportunities.csv"),
            db_path=o.get("db_path", "trades.db"),
        )

    return config


def _set_default_platforms(config: AppConfig):
    """Ensure all platform entries exist with defaults."""
    defaults = {
        "polymarket": True,
        "kalshi": True,
    }
    for name, default_enabled in defaults.items():
        if name not in config.platforms:
            config.platforms[name] = PlatformConfig(enabled=default_enabled)


# ============================================================
# LEGACY CONSTANTS AND UTILITIES (to be removed/refactored)
# ============================================================

DEMO_MATCHES = [
    {"title": "Liverpool FC – Chelsea FC", "league": "Premier League", "country": "England",
     "home_prob": 0.45, "draw_prob": 0.30, "away_prob": 0.10, "market_type": "1X2"},
    {"title": "Manchester City – Burnley FC", "league": "Premier League", "country": "England",
     "home_prob": 0.50, "draw_prob": 0.28, "away_prob": 0.08, "market_type": "1X2"},
    {"title": "Real Madrid – Barcelona", "league": "La Liga", "country": "Spain",
     "home_prob": 0.40, "draw_prob": 0.35, "away_prob": 0.20, "market_type": "1X2"},
    {"title": "Bayern Munich – Borussia Dortmund", "league": "Bundesliga", "country": "Germany",
     "home_prob": 0.35, "draw_prob": 0.30, "away_prob": 0.25, "market_type": "1X2"},
    {"title": "AC Milan – Inter Milan", "league": "Serie A", "country": "Italy",
     "home_prob": 0.42, "draw_prob": 0.32, "away_prob": 0.12, "market_type": "1X2"},
    {"title": "PSG – Lyon", "league": "Ligue 1", "country": "France",
     "home_prob": 0.55, "draw_prob": 0.25, "away_prob": 0.07, "market_type": "1X2"},
    {"title": "Ajax – PSV", "league": "Eredivisie", "country": "Netherlands",
     "home_prob": 0.38, "draw_prob": 0.33, "away_prob": 0.18, "market_type": "1X2"},
    {"title": "Juventus – Napoli", "league": "Serie A", "country": "Italy",
     "home_prob": 0.36, "draw_prob": 0.34, "away_prob": 0.22, "market_type": "1X2"},
    {"title": "Arsenal – Tottenham", "league": "Premier League", "country": "England",
     "home_prob": 0.44, "draw_prob": 0.31, "away_prob": 0.11, "market_type": "1X2"},
    {"title": "Atletico Madrid – Sevilla", "league": "La Liga", "country": "Spain",
     "home_prob": 0.48, "draw_prob": 0.29, "away_prob": 0.09, "market_type": "1X2"},
]
