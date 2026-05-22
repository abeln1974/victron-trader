"""Logging og beregning av inntjening."""
import sqlite3
from contextlib import closing
import os
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional
from config import CONFIG, OSLO_TZ


@dataclass
class Trade:
    timestamp: datetime
    action: str  # buy/sell
    energy_kwh: float
    price_nok_kwh: float
    net_profit_nok: float


class ProfitTracker:
    def __init__(self, db_path: str = CONFIG.db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()
        self._last_buy_price: float = self._load_last_buy_price()

    def _conn(self):
        return closing(sqlite3.connect(self.db_path))

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    energy_kwh REAL NOT NULL,
                    price_nok_kwh REAL NOT NULL,
                    net_profit_nok REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_plan (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    solar_kwh_forecast REAL NOT NULL,
                    solar_reserve_pct REAL NOT NULL,
                    charge_target_soc REAL NOT NULL,
                    storm_mode INTEGER NOT NULL,
                    soc_at_cycle REAL,
                    spot_nok_kwh REAL
                )
            """)
            conn.commit()

    def log_plan(self, solar_kwh_forecast: float, solar_reserve_pct: float,
                 charge_target_soc: float, storm_mode: bool,
                 soc: float = None, spot_nok_kwh: float = None):
        """Logg optimizer-plan ved hvert trade-cycle for etteranalyse."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO daily_plan
                   (timestamp, solar_kwh_forecast, solar_reserve_pct,
                    charge_target_soc, storm_mode, soc_at_cycle, spot_nok_kwh)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(OSLO_TZ).isoformat(),
                 solar_kwh_forecast, solar_reserve_pct,
                 charge_target_soc, int(storm_mode),
                 soc, spot_nok_kwh)
            )
            conn.commit()

    def _load_last_buy_price(self) -> float:
        """Last kjøpspris fra DB ved oppstart — overlever restart."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT price_nok_kwh FROM trades WHERE action='buy' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else 0.0

    def log_trade(self, action: str, energy_kwh: float, price_nok_kwh: float,
                  efficiency: float = CONFIG.battery_efficiency) -> float:
        """
        Logg handel og beregn netto profitt.
        Buy  → negativ (kostnad)
        Sell → positiv kun hvis salgspris > siste kjøpspris (reell arbitrasje)
        net_profit = (salgspris - siste_kjøpspris) × kWh × effektivitet
        """
        if action == "buy":
            net_profit = -energy_kwh * price_nok_kwh
            self._last_buy_price = price_nok_kwh
        elif action == "sell":
            buy_cost = self._last_buy_price or price_nok_kwh
            net_profit = energy_kwh * (price_nok_kwh - buy_cost) * efficiency
        else:
            net_profit = 0

        with self._conn() as conn:
            conn.execute(
                "INSERT INTO trades (timestamp, action, energy_kwh, price_nok_kwh, net_profit_nok) VALUES (?, ?, ?, ?, ?)",
                (datetime.now(OSLO_TZ).isoformat(), action, energy_kwh, price_nok_kwh, net_profit)
            )
            conn.commit()
        return net_profit

    def get_today_trades(self) -> List[Trade]:
        """Get all trades for today."""
        today = datetime.now(OSLO_TZ).strftime("%Y-%m-%d")
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT timestamp, action, energy_kwh, price_nok_kwh, net_profit_nok FROM trades WHERE date(timestamp) = ?",
                (today,)
            )
            rows = cursor.fetchall()
        return [Trade(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4]) for r in rows]

    def get_total_profit(self, days: Optional[int] = None) -> float:
        """Get total net profit for period."""
        with self._conn() as conn:
            if days:
                cutoff = (datetime.now(OSLO_TZ) - timedelta(days=days)).isoformat()
                cursor = conn.execute(
                    "SELECT SUM(net_profit_nok) FROM trades WHERE timestamp > ?",
                    (cutoff,)
                )
            else:
                cursor = conn.execute("SELECT SUM(net_profit_nok) FROM trades")
            result = cursor.fetchone()[0] or 0.0
        return result

    def get_stats(self) -> dict:
        """Get summary statistics."""
        today = datetime.now(OSLO_TZ).strftime("%Y-%m-%d")
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT action, SUM(energy_kwh), SUM(net_profit_nok) FROM trades WHERE date(timestamp) = ? GROUP BY action",
                (today,)
            )
            today_stats = {row[0]: {"kwh": row[1], "profit": row[2]} for row in cursor.fetchall()}

            cursor = conn.execute("SELECT SUM(net_profit_nok), COUNT(*) FROM trades")
            total_profit, trade_count = cursor.fetchone()

        return {
            "today_bought_kwh": today_stats.get("buy", {}).get("kwh", 0),
            "today_sold_kwh": today_stats.get("sell", {}).get("kwh", 0),
            "today_profit_nok": sum(s.get("profit", 0) for s in today_stats.values()),
            "total_profit_nok": total_profit or 0,
            "total_trades": trade_count or 0
        }

    def get_recent_trades(self, limit: int = 20) -> list:
        """Hent siste N handler for dashboard."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT timestamp, action, energy_kwh, price_nok_kwh, net_profit_nok "
                "FROM trades ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
            rows = cursor.fetchall()
        return [
            {
                "timestamp": row[0],
                "trade_type": row[1],
                "energy_kwh": row[2],
                "price_nok_kwh": row[3],
                "net_profit_nok": row[4],
            }
            for row in rows
        ]

    def get_hourly_trades(self, hours: int = 24) -> list:
        """Hent trades gruppert per time med sum kjøpt/solgt."""
        cutoff = (datetime.now(OSLO_TZ) - timedelta(hours=hours)).isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """SELECT
                    strftime('%Y-%m-%d %H:00', timestamp) as hour,
                    SUM(CASE WHEN action = 'buy' THEN energy_kwh ELSE 0 END) as bought_kwh,
                    SUM(CASE WHEN action = 'sell' THEN energy_kwh ELSE 0 END) as sold_kwh,
                    SUM(CASE WHEN action = 'buy' THEN 1 ELSE 0 END) as buy_count,
                    SUM(CASE WHEN action = 'sell' THEN 1 ELSE 0 END) as sell_count,
                    SUM(net_profit_nok) as net_profit
                FROM trades
                WHERE timestamp > ?
                GROUP BY hour
                ORDER BY hour DESC""",
                (cutoff,)
            )
            rows = cursor.fetchall()
        return [
            {
                "hour": row[0],
                "bought_kwh": round(row[1] or 0, 2),
                "sold_kwh": round(row[2] or 0, 2),
                "buy_count": int(row[3] or 0),
                "sell_count": int(row[4] or 0),
                "net_profit_nok": round(row[5] or 0, 2),
            }
            for row in rows
        ]


if __name__ == "__main__":
    tracker = ProfitTracker()

    tracker.log_trade("buy", 10, 0.5)
    tracker.log_trade("sell", 10, 1.2)

    stats = tracker.get_stats()
    print(f"Dagens fortjeneste: {stats['today_profit_nok']:.2f} kr")
    print(f"Total fortjeneste: {stats['total_profit_nok']:.2f} kr")
