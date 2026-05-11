"""Henter spotpriser fra hvakosterstrommen.no (primær) med Nordpool direkte som fallback."""
import requests
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import List, Optional
from config import CONFIG, OSLO_TZ


@dataclass
class PricePoint:
    timestamp: datetime
    price_ore_kwh: float  # Ex VAT
    price_nok_kwh: float  # Inc VAT


class PriceFetcher:
    PRIMARY_URL  = "https://www.hvakosterstrommen.no/api/v1/prices"
    # Nordpool offisielt day-ahead API (ingen auth nødvendig for day-ahead)
    NORDPOOL_URL = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"

    def __init__(self, price_area: str = CONFIG.price_area):
        self.price_area = price_area

    def _fetch_day(self, year: int, month: int, day: int) -> List[PricePoint]:
        """Fetch prices - prøv hvakosterstrommen.no først, Nordpool direkte som fallback."""
        try:
            return self._fetch_hvakoster(year, month, day)
        except RuntimeError:
            return self._fetch_nordpool(year, month, day)

    def _fetch_hvakoster(self, year: int, month: int, day: int) -> List[PricePoint]:
        """Primær: hvakosterstrommen.no (enkel proxy for Nordpool)."""
        url = f"{self.PRIMARY_URL}/{year}/{month:02d}-{day:02d}_{self.price_area}.json"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            return self._parse_hvakoster(data)
        except requests.RequestException as e:
            raise RuntimeError(f"hvakosterstrommen.no feilet: {e}")

    def _parse_hvakoster(self, data: list) -> List[PricePoint]:
        points = []
        for item in data:
            ts  = datetime.fromisoformat(item["time_start"].replace("Z", "+00:00")).astimezone(OSLO_TZ)
            ore = item["NOK_per_kWh"] * 100
            nok = item["NOK_per_kWh"] * CONFIG.vat
            points.append(PricePoint(timestamp=ts, price_ore_kwh=ore, price_nok_kwh=nok))
        return points

    def _fetch_nordpool(self, year: int, month: int, day: int) -> List[PricePoint]:
        """Fallback: Nordpool offisielt day-ahead API."""
        date_str = f"{year}-{month:02d}-{day:02d}"
        params = {
            "market": "DayAhead",
            "deliveryArea": self.price_area,
            "currency": "NOK",
            "date": date_str,
        }
        try:
            resp = requests.get(self.NORDPOOL_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return self._parse_nordpool(data)
        except requests.RequestException as e:
            raise RuntimeError(f"Nordpool API feilet: {e}")

    def _parse_nordpool(self, data: dict) -> List[PricePoint]:
        """Parse Nordpool day-ahead API respons."""
        points = []
        for entry in data.get("multiAreaEntries", []):
            ts_str = entry.get("deliveryStart") or entry.get("deliveryPeriod", {}).get("start", "")
            if not ts_str:
                continue
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(OSLO_TZ)
            # Nordpool returnerer NOK/MWh → konverter til øre/kWh
            area_prices = entry.get("entryPerArea", {})
            price_mwh = area_prices.get(self.price_area, 0)
            ore = price_mwh / 10   # MWh → kWh → øre
            nok = ore / 100 * CONFIG.vat
            points.append(PricePoint(timestamp=ts, price_ore_kwh=ore, price_nok_kwh=nok))
        return points

    def get_prices(self, hours: int = 24) -> List[PricePoint]:
        """Get prices for next N hours (today + tomorrow if available)."""
        now = datetime.now(OSLO_TZ)
        today = now.date()
        tomorrow = today + timedelta(days=1)

        points = self._fetch_day(today.year, today.month, today.day)

        # Try to get tomorrow's prices if available (usually after 13:00)
        try:
            tomorrow_points = self._fetch_day(tomorrow.year, tomorrow.month, tomorrow.day)
            points.extend(tomorrow_points)
        except RuntimeError:
            pass

        # Filtrer: inkluder nåværende time (sammenlign kun på time-nivå)
        now_hour = now.replace(minute=0, second=0, microsecond=0)
        future = [p for p in points if p.timestamp >= now_hour]
        return future[:hours]

    def get_current_price(self) -> Optional[PricePoint]:
        """Get price for current hour."""
        now_oslo = datetime.now(OSLO_TZ)
        prices = self._fetch_day(now_oslo.year, now_oslo.month, now_oslo.day)
        for p in prices:
            if p.timestamp.hour == now_oslo.hour:
                return p
        return None


if __name__ == "__main__":
    pf = PriceFetcher()
    prices = pf.get_prices(48)
    for p in prices[:5]:
        print(f"{p.timestamp.strftime('%H:%M %Z')}: {p.price_ore_kwh:.1f} øre ({p.price_nok_kwh:.3f} kr)")
