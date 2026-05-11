"""Henter spotpriser fra hvakosterstrommen.no API."""
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional
from config import CONFIG


@dataclass
class PricePoint:
    timestamp: datetime
    price_ore_kwh: float  # Ex VAT
    price_nok_kwh: float  # Inc VAT


class PriceFetcher:
    BASE_URL = "https://www.hvakosterstrommen.no/api/v1/prices"

    def __init__(self, price_area: str = CONFIG.price_area):
        self.price_area = price_area

    def _fetch_day(self, year: int, month: int, day: int) -> List[PricePoint]:
        """Fetch prices for a single day."""
        url = f"{self.BASE_URL}/{year}/{month:02d}-{day:02d}_{self.price_area}.json"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            points = []
            for item in data:
                ts = datetime.fromisoformat(item["time_start"].replace("Z", "+00:00"))
                ore = item["NOK_per_kWh"] * 100  # Convert to øre
                nok = item["NOK_per_kWh"] * CONFIG.vat
                points.append(PricePoint(timestamp=ts, price_ore_kwh=ore, price_nok_kwh=nok))
            return points
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to fetch prices: {e}")

    def get_prices(self, hours: int = 24) -> List[PricePoint]:
        """Get prices for next N hours (today + tomorrow if available)."""
        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        points = self._fetch_day(today.year, today.month, today.day)

        # Try to get tomorrow's prices if available (usually after 13:00)
        try:
            tomorrow_points = self._fetch_day(tomorrow.year, tomorrow.month, tomorrow.day)
            points.extend(tomorrow_points)
        except RuntimeError:
            pass

        # Filter future hours only
        future = [p for p in points if p.timestamp >= now]
        return future[:hours]

    def get_current_price(self) -> Optional[PricePoint]:
        """Get price for current hour."""
        prices = self._fetch_day(datetime.now().year, datetime.now().month, datetime.now().day)
        now = datetime.now()
        for p in prices:
            if p.timestamp.hour == now.hour:
                return p
        return None


if __name__ == "__main__":
    pf = PriceFetcher()
    prices = pf.get_prices(48)
    for p in prices[:5]:
        print(f"{p.timestamp.strftime('%H:%M')}: {p.price_ore_kwh:.1f} øre ({p.price_nok_kwh:.3f} kr)")
