"""Sol-prognoser fra Open-Meteo (MET Norway MEPS 2.5 km modell).

Open-Meteo bruker MET Norway MetCoOp MEPS som kilde for Skandinavia — samme modell
som yr.no, men eksponerer shortwave_radiation (W/m²) som met.no ikke tilbyr direkte.
Gratis, ingen API-nøkkel, oppdateres hver time.
"""
import json
import logging
import urllib.request
import urllib.error
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import CONFIG, OSLO_TZ

log = logging.getLogger(__name__)

_BASE_URL = "https://api.open-meteo.com/v1/forecast"


def _fetch_radiation(lat: float, lon: float) -> dict:
    """Hent shortwave_radiation per time fra Open-Meteo MEPS."""
    params = (
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=shortwave_radiation"
        f"&models=metno_seamless"
        f"&forecast_days=2"
        f"&timezone=Europe%2FOslo"
    )
    url = _BASE_URL + params
    req = urllib.request.Request(url, headers={"User-Agent": "victron-trader/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def get_solar_kwh_tomorrow(lat: float, lon: float,
                            panel_peak_kw: float,
                            system_efficiency: float = 0.85) -> float:
    """
    Returner estimert sol-produksjon i kWh for i morgen (05:00–21:00).

    panel_peak_kw: Inverter maks effekt (f.eks 5.0 kW Fronius Primo)
    system_efficiency: System-virkningsgrad inkl. panel-temp, kabler, inverter (default 0.85)
    Returnerer 0.0 ved feil (fallback til statisk reserve i optimizer).
    """
    try:
        data = _fetch_radiation(lat, lon)
        times = data["hourly"]["time"]
        swrad = data["hourly"]["shortwave_radiation"]

        tomorrow_str = (date.today() + __import__('datetime').timedelta(days=1)).isoformat()
        # times starter kl 00:00 i dag = index 0, i morgen = index 24
        total_kwh = 0.0
        for i, t in enumerate(times):
            if not t.startswith(tomorrow_str):
                # Hopp over i dag — vi vil ha i morgen
                continue
            hour = int(t[11:13])
            if hour < 5 or hour > 21:
                continue
            wm2 = swrad[i] or 0.0
            # W/m² → kW (normalisert mot 1000 W/m² referanse × peak_kw × effektivitet)
            kw = min(panel_peak_kw, wm2 / 1000.0 * panel_peak_kw * system_efficiency)
            total_kwh += kw  # 1 time per datapunkt = kWh

        log.info("Open-Meteo sol-prognose i morgen: %.1f kWh (%.1f eff. timer)",
                 total_kwh, total_kwh / panel_peak_kw if panel_peak_kw else 0)
        return total_kwh

    except urllib.error.URLError as e:
        log.warning("Open-Meteo ikke tilgjengelig: %s — bruker statisk sol-reserve", e)
        return 0.0
    except Exception as e:
        log.warning("Sol-prognose feil: %s — bruker statisk sol-reserve", e)
        return 0.0


def get_solar_reserve_pct(lat: float, lon: float,
                           panel_peak_kw: float,
                           battery_capacity_kwh: float,
                           system_efficiency: float = 0.85,
                           fallback_hours: float = 4.0,
                           max_reserve_pct: float = 40.0) -> float:
    """
    Beregn sol-reserve i prosent av batterikapasitet for i morgen.

    Returnerer prosentandel SOC som skal reserveres for sol-lading.
    Ved API-feil returneres fallback (statisk beregning basert på fallback_hours).
    """
    solar_kwh = get_solar_kwh_tomorrow(lat, lon, panel_peak_kw, system_efficiency)

    if solar_kwh > 0:
        reserve_pct = min(max_reserve_pct, (solar_kwh / battery_capacity_kwh) * 100)
        log.info("Dynamisk sol-reserve: %.1f%% SOC (%.1f kWh prognose i morgen)",
                 reserve_pct, solar_kwh)
        return reserve_pct
    else:
        # Fallback: statisk beregning
        fallback_kwh = panel_peak_kw * fallback_hours
        reserve_pct = min(max_reserve_pct, (fallback_kwh / battery_capacity_kwh) * 100)
        log.info("Statisk sol-reserve (fallback): %.1f%% SOC (%.1f kWh estimat)",
                 reserve_pct, fallback_kwh)
        return reserve_pct


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    lat = CONFIG.site_lat
    lon = CONFIG.site_lon
    kwh = get_solar_kwh_tomorrow(lat, lon, CONFIG.solar_max_kw)
    reserve = get_solar_reserve_pct(lat, lon, CONFIG.solar_max_kw, CONFIG.battery_capacity_kwh)
    print(f"Sol i morgen: {kwh:.1f} kWh")
    print(f"Sol-reserve:  {reserve:.1f}% SOC")
    print(f"Lademål natt: {CONFIG.max_soc - reserve:.1f}% SOC")
