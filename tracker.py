"""
Portfolio Tracker for the Prediction Market Arbitrage Bot.

Tracks open positions, settled trades, and P&L using SQLite storage.
Also maintains CSV logging for backward compatibility.
"""

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from platform_base import ArbOpportunity


class PortfolioTracker:
    """Tracks trades, positions, and P&L in SQLite + CSV."""

    def __init__(self, db_path: str = "trades.db", csv_path: str = "opportunities.csv"):
        self.db_path = db_path
        self.csv_path = csv_path
        self._init_db()
        self._ensure_csv()

    def _init_db(self):
        """Create database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_key TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                home_team TEXT,
                away_team TEXT,
                kickoff TEXT,
                league TEXT,
                outcome_a TEXT,
                platform_a TEXT,
                market_id_a TEXT,
                price_a REAL,
                outcome_b TEXT,
                platform_b TEXT,
                market_id_b TEXT,
                price_b REAL,
                rejected_outcome TEXT,
                rejected_price REAL,
                stake REAL,
                status TEXT DEFAULT 'open'
            );

            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_key TEXT NOT NULL,
                result TEXT NOT NULL,
                settled_at TEXT NOT NULL,
                trade_id INTEGER,
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            );

            CREATE TABLE IF NOT EXISTS bets (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                match_key       TEXT NOT NULL,
                date            TEXT NOT NULL,
                kickoff_iso     TEXT DEFAULT '',
                home_team       TEXT NOT NULL,
                away_team       TEXT NOT NULL,
                best_home       REAL NOT NULL,
                best_draw       REAL NOT NULL,
                best_away       REAL NOT NULL,
                rejected        TEXT NOT NULL,
                rejected_price  REAL NOT NULL,
                result          TEXT NOT NULL DEFAULT 'PENDING',
                placed_at       TEXT NOT NULL,
                polymarket_url  TEXT DEFAULT '',
                kalshi_url      TEXT DEFAULT '',
                poly_market_id  TEXT DEFAULT '',
                kalshi_market_id TEXT DEFAULT '',
                stake           REAL DEFAULT 0,
                shares          INTEGER DEFAULT 0,
                covered_a       TEXT DEFAULT '',
                covered_b       TEXT DEFAULT '',
                platform_a      TEXT DEFAULT '',
                platform_b      TEXT DEFAULT '',
                price_a         REAL DEFAULT 0,
                price_b         REAL DEFAULT 0,
                best_home_platform TEXT DEFAULT '',
                best_draw_platform TEXT DEFAULT '',
                best_away_platform TEXT DEFAULT ''
            );
        """)
        # Migrate existing databases that predate the stake column
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN stake REAL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN shares INTEGER DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN covered_a TEXT DEFAULT ''")
            conn.execute("ALTER TABLE bets ADD COLUMN covered_b TEXT DEFAULT ''")
            conn.execute("ALTER TABLE bets ADD COLUMN platform_a TEXT DEFAULT ''")
            conn.execute("ALTER TABLE bets ADD COLUMN platform_b TEXT DEFAULT ''")
            conn.execute("ALTER TABLE bets ADD COLUMN price_a REAL DEFAULT 0")
            conn.execute("ALTER TABLE bets ADD COLUMN price_b REAL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN kickoff_iso TEXT DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN winning_outcome TEXT DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN best_home_platform TEXT DEFAULT ''")
            conn.execute("ALTER TABLE bets ADD COLUMN best_draw_platform TEXT DEFAULT ''")
            conn.execute("ALTER TABLE bets ADD COLUMN best_away_platform TEXT DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        conn.close()

    def _ensure_csv(self):
        """Create CSV file with headers if it doesn't exist."""
        path = Path(self.csv_path)
        if not path.exists():
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Timestamp", "Match", "League", "Outcome_A", "Platform_A",
                    "Price_A", "Outcome_B", "Platform_B", "Price_B",
                    "Rejected", "Rejected_Price", "Stake",
                ])

    def record_opportunity(self, opp: ArbOpportunity):
        """Record a detected opportunity (even if not executed)."""
        timestamp = datetime.now(timezone.utc).isoformat()

        # CSV log
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp,
                f"{opp.home_team} vs {opp.away_team}",
                opp.league,
                opp.outcome_a,
                opp.platform_a,
                f"{opp.price_a:.4f}",
                opp.outcome_b,
                opp.platform_b,
                f"{opp.price_b:.4f}",
                opp.rejected_outcome,
                f"{opp.rejected_price:.4f}",
                f"{0.0:.2f}",
            ])

    def record_trade(self, opp: ArbOpportunity, stake: float) -> int:
        """Record an executed trade. Returns the trade ID."""
        timestamp = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            """INSERT INTO trades (
                match_key, timestamp, home_team, away_team, kickoff, league,
                outcome_a, platform_a, market_id_a, price_a,
                outcome_b, platform_b, market_id_b, price_b,
                rejected_outcome, rejected_price, stake, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opp.match_key, timestamp, opp.home_team, opp.away_team,
                opp.kickoff.isoformat() if opp.kickoff else None, opp.league,
                opp.outcome_a, opp.platform_a, opp.market_id_a, opp.price_a,
                opp.outcome_b, opp.platform_b, opp.market_id_b, opp.price_b,
                opp.rejected_outcome, opp.rejected_price, stake, "open",
            ),
        )
        conn.commit()
        trade_id = cursor.lastrowid
        conn.close()
        return trade_id

    def record_settlement(self, match_key: str, result: str, pnl: float):
        """Record a match settlement and close the trade."""
        timestamp = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)

        # Find the open trade for this match
        row = conn.execute(
            "SELECT id FROM trades WHERE match_key = ? AND status = 'open' LIMIT 1",
            (match_key,),
        ).fetchone()
        trade_id = row[0] if row else None

        conn.execute(
            "INSERT INTO settlements (match_key, result, settled_at, trade_id) "
            "VALUES (?, ?, ?, ?)",
            (match_key, result, timestamp, trade_id),
        )

        if trade_id:
            conn.execute(
                "UPDATE trades SET status = 'settled' WHERE id = ?",
                (trade_id,),
            )

        conn.commit()
        conn.close()



    def get_open_positions(self) -> list[dict]:
        """Get all currently open positions."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open'"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_pnl_summary(self) -> dict:
        """Get P&L summary across all settled trades."""
        conn = sqlite3.connect(self.db_path)

        row = conn.execute(
            """
            SELECT 
                COUNT(*),
                COALESCE(SUM(
                    CASE 
                        WHEN s.result = t.rejected_outcome THEN -t.stake
                        ELSE (t.stake / (t.price_a + t.price_b)) - t.stake
                    END
                ), 0),
                SUM(CASE WHEN s.result != t.rejected_outcome THEN 1 ELSE 0 END),
                SUM(CASE WHEN s.result = t.rejected_outcome THEN 1 ELSE 0 END)
            FROM settlements s
            JOIN trades t ON s.trade_id = t.id
            """
        ).fetchone()

        total_settled_trades = row[0] if row else 0
        total_pnl = row[1] if row else 0
        wins = row[2] if row else 0
        losses = row[3] if row else 0

        total_trades = conn.execute(
            "SELECT COUNT(*) FROM trades"
        ).fetchone()[0]

        open_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        ).fetchone()[0]

        total_staked = conn.execute(
            "SELECT COALESCE(SUM(stake), 0) FROM trades WHERE status = 'settled'"
        ).fetchone()[0]

        conn.close()

        return {
            "total_pnl": total_pnl,
            "wins": wins,
            "losses": losses,
            "total_trades": total_trades,
            "open_trades": open_trades,
            "total_staked": total_staked,
            "roi": total_pnl / total_staked if total_staked > 0 else 0,
            "win_rate": wins / (wins + losses) if (wins + losses) > 0 else 0,
        }

    # ================================================================
    # Web simulator bets
    # ================================================================

    _BET_COLUMNS = [
        "match_key", "date", "kickoff_iso", "home_team", "away_team",
        "best_home", "best_draw", "best_away",
        "rejected", "rejected_price",
        "result", "placed_at",
        "polymarket_url", "kalshi_url",
        "poly_market_id", "kalshi_market_id",
        "stake", "shares",
        "covered_a", "covered_b", "platform_a", "platform_b", "price_a", "price_b",
        "best_home_platform", "best_draw_platform", "best_away_platform",
    ]

    def add_bet(self, data: dict) -> int:
        """Insert a simulator bet. Returns the new bet ID."""
        conn = sqlite3.connect(self.db_path)
        cols = self._BET_COLUMNS
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)
        values = tuple(data.get(c, "") for c in cols)
        cursor = conn.execute(
            f"INSERT INTO bets ({col_names}) VALUES ({placeholders})", values
        )
        conn.commit()
        bet_id = cursor.lastrowid
        conn.close()
        return bet_id

    def get_all_bets(self) -> list[dict]:
        """Return all simulator bets, newest first."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM bets ORDER BY placed_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_pending_bets(self) -> list[dict]:
        """Return all bets with result='PENDING'."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM bets WHERE result = 'PENDING'"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def update_bet_kickoff(self, bet_id: int, kickoff_iso: str):
        """Persist kickoff info discovered after the bet was originally saved."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE bets SET kickoff_iso = ? WHERE id = ?",
            (kickoff_iso, bet_id),
        )
        conn.commit()
        conn.close()

    def update_bet_result(self, bet_id: int, result: str, winning_outcome: str = ""):
        """Update the result and winning outcome of a bet."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE bets SET result = ?, winning_outcome = ? WHERE id = ?",
            (result, winning_outcome, bet_id),
        )
        conn.commit()
        conn.close()

    def delete_bet(self, bet_id: int):
        """Delete a simulator bet."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
        conn.commit()
        conn.close()

    def get_bets_pnl(self) -> dict:
        """Compute P&L summary for simulator bets."""
        conn = sqlite3.connect(self.db_path)
        passes = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(
                (stake / (
                    CASE rejected
                        WHEN 'home' THEN best_draw + best_away
                        WHEN 'draw' THEN best_home + best_away
                        WHEN 'away' THEN best_home + best_draw
                        ELSE 1.0
                    END
                )) - stake
            ), 0) FROM bets WHERE result = 'PASS'
            """
        ).fetchone()
        fails = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(stake), 0) FROM bets WHERE result = 'FAIL'"
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(stake), 0) FROM bets WHERE result = 'PENDING'"
        ).fetchone()
        conn.close()

        total = passes[1] - fails[1]
        return {
            "total": round(total, 2),
            "passes": passes[0],
            "pass_amount": round(passes[1], 2),
            "fails": fails[0],
            "fail_amount": round(fails[1], 2),
            "pending": pending[0],
            "pending_amount": round(pending[1], 2),
        }

    def migrate_csv_bets(self, csv_path: str):
        """One-time import from bets.csv if the bets table is empty."""
        path = Path(csv_path)
        if not path.exists():
            return

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        if count > 0:
            conn.close()
            return

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                conn.execute(
                    """INSERT INTO bets (
                        match_key, date, kickoff_iso, home_team, away_team,
                        best_home, best_draw, best_away,
                        rejected, rejected_price,
                        result, placed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["match_key"], r["date"], r.get("kickoff_iso", ""), r["home_team"], r["away_team"],
                        float(r["best_home"]), float(r["best_draw"]), float(r["best_away"]),
                        r["rejected"], float(r["rejected_price"]),
                        r["result"], r["placed_at"],
                    ),
                )
        conn.commit()
        conn.close()
