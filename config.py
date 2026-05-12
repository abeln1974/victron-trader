"""Konfigurasjon for Victron Energy Trader."""
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

OSLO_TZ = ZoneInfo("Europe/Oslo")

load_dotenv()


@dataclass(frozen=True)
class Config:
    # Victron Modbus-TCP
    victron_host: str = os.getenv("VICTRON_HOST", "192.168.1.100")
    victron_modbus_port: int = int(os.getenv("VICTRON_MODBUS_PORT", "502"))
    victron_unit_id: int = int(os.getenv("VICTRON_UNIT_ID", "246"))

    # Market
    price_area: str = os.getenv("PRICE_AREA", "NO1")
    vat: float = float(os.getenv("VAT", "1.25"))

    # Battery
    # SmartShunt: 800Ah × 57V (charged) = 45.6 kWh brutto
    # Discharge floor 10% (Victron), men vi bruker 20% for NMC-levetid
    battery_capacity_kwh: float = float(os.getenv("BATTERY_CAPACITY_KWH", "45.6"))
    battery_max_charge_kw: float = float(os.getenv("BATTERY_MAX_CHARGE_KW", "10"))
    battery_max_discharge_kw: float = float(os.getenv("BATTERY_MAX_DISCHARGE_KW", "10"))
    battery_efficiency: float = float(os.getenv("BATTERY_EFFICIENCY", "0.95"))
    min_soc: float = float(os.getenv("MIN_SOC", "20"))   # NMC: 20% (Victron floor 10%)
    max_soc: float = float(os.getenv("MAX_SOC", "90"))   # NMC: unngå langvarig >90%

    # Solar (Fronius Primo 5kW AC-coupled)
    solar_max_kw: float = float(os.getenv("SOLAR_MAX_KW", "5.0"))
    solar_threshold_kw: float = float(os.getenv("SOLAR_THRESHOLD_KW", "0.5"))  # Min sol for å unngå nett-lading
    solar_system_efficiency: float = float(os.getenv("SOLAR_SYSTEM_EFFICIENCY", "0.85"))
    solar_fallback_hours: float = float(os.getenv("SOLAR_EFFECTIVE_HOURS", "4.0"))  # Brukes hvis API feiler

    # Lokasjon (for Open-Meteo sol-prognose — MET Norway MEPS 2.5km)
    site_lat: float = float(os.getenv("SITE_LAT", "60.14"))   # Ringerike
    site_lon: float = float(os.getenv("SITE_LON", "10.25"))   # Ringerike

    # Peak-shaving (Føie AS kapasitetstrinn)
    peak_limit_kw: float = float(os.getenv("PEAK_LIMIT_KW", "9.5"))  # Buffer til 10kW-trinnet
    peak_reserve_kwh: float = float(os.getenv("PEAK_RESERVE_KWH", "5.0"))

    # EVCS elbil-lader (via Home Assistant)
    evcs_entity_prefix: str = os.getenv("EVCS_ENTITY_PREFIX", "evcs_hq2309vtvnf")
    evcs_min_current_a: int = int(os.getenv("EVCS_MIN_CURRENT_A", "6"))   # Min ladestrøm (A)
    evcs_max_current_a: int = int(os.getenv("EVCS_MAX_CURRENT_A", "16"))  # Max vi tillater (A)
    evcs_phases: int = int(os.getenv("EVCS_PHASES", "1"))                 # Antall faser (EVCS HQ2309VTVNF er 1-fase)

    # Strategy
    min_price_diff_nok: float = float(os.getenv("MIN_PRICE_DIFF_NOK", "1.60"))  # Arbitrasje lønnsomt kun ved spot >233 øre eks mva
    forecast_hours: int = int(os.getenv("FORECAST_HOURS", "24"))

    # Paths
    db_path: str = os.getenv("DB_PATH", "./data/profit.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


CONFIG = Config()
