"""Logging og beregning av inntjening."""
import sqlite3
import os
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional
from config import CONFIG


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

    def _init_db(self):
        """Create tables if not exist."""
        conn = sqlite3.connect(self.db_path)
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
            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY,
                total_bought_kwh REAL,
                total_sold_kwh REAL,
                gross_profit_nok REAL,
                net_profit_nok REAL
            )
        """)
        conn.commit()
        conn.close()

    def log_trade(self, action: str, energy_kwh: float, price_nok_kwh: float, 
                  efficiency: float = CONFIG.battery_efficiency) -> float:
        """
        Log a trade and calculate net profit.
        Buy = negative profit (cost)
        Sell = positive profit (revenue), minus round-trip efficiency loss
        """
        if action == "buy":
            net_profit = -energy_kwh * price_nok_kwh
        elif action == "sell":
            # Account for efficiency loss on discharge
            net_profit = energy_kwh * price_nok_kwh * efficiency
        else:
            net_profit = 0

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO trades (timestamp, action, energy_kwh, price_nok_kwh, net_profit_nok) VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), action, energy_kwh, price_nok_kwh, net_profit)
        )
        conn.commit()
        conn.close()
        return net_profit

    def get_today_trades(self) -> List[Trade]:
        """Get all trades for today."""
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT timestamp, action, energy_kwh, price_nok_kwh, net_profit_nok FROM trades WHERE date(timestamp) = ?",
            (today,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [Trade(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4]) for r in rows]

    def get_total_profit(self, days: Optional[int] = None) -> float:
        """Get total net profit for period."""
        conn = sqlite3.connect(self.db_path)
        if days:
            cursor = conn.execute(
                "SELECT SUM(net_profit_nok) FROM trades WHERE datetime(timestamp) > datetime('now', '-{} days')".format(days)
            )
        else:
            cursor = conn.execute("SELECT SUM(net_profit_nok) FROM trades")
        result = cursor.fetchone()[0] or 0.0
        conn.close()
        return result

    def get_stats(self) -> dict:
        """Get summary statistics."""
        conn = sqlite3.connect(self.db_path)
        
        # Today
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = conn.execute(
            "SELECT action, SUM(energy_kwh), SUM(net_profit_nok) FROM trades WHERE date(timestamp) = ? GROUP BY action",
            (today,)
        )
        today_stats = {row[0]: {"kwh": row[1], "profit": row[2]} for row in cursor.fetchall()}
        
        # All time
        cursor = conn.execute("SELECT SUM(net_profit_nok), COUNT(*) FROM trades")
        total_profit, trade_count = cursor.fetchone()
        
        conn.close()
        
        return {
            "today_bought_kwh": today_stats.get("buy", {}).get("kwh", 0),
            "today_sold_kwh": today_stats.get("sell", {}).get("kwh", 0),
            "today_profit_nok": sum(s.get("profit", 0) for s in today_stats.values()),
            "total_profit_nok": total_profit or 0,
            "total_trades": trade_count or 0
        }


    def get_recent_trades(self, limit: int = 20) -> list:
        """Hent siste N handler for dashboard."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT timestamp, action, energy_kwh, price_nok_kwh, net_profit_nok "
            "FROM trades ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
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


if __name__ == "__main__":
    tracker = ProfitTracker()
    
    # Simulate some trades
    tracker.log_trade("buy", 10, 0.5)
    tracker.log_trade("sell", 10, 1.2)
    
    stats = tracker.get_stats()
    print(f"Dagens fortjeneste: {stats['today_profit_nok']:.2f} kr")
    print(f"Total fortjeneste: {stats['total_profit_nok']:.2f} kr")
