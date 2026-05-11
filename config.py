"""Konfigurasjon for Victron Energy Trader."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    # Victron MQTT
    victron_host: str = os.getenv("VICTRON_HOST", "192.168.1.100")
    victron_port: int = int(os.getenv("VICTRON_PORT", "1883"))
    victron_username: str = os.getenv("VICTRON_USERNAME", "")
    victron_password: str = os.getenv("VICTRON_PASSWORD", "")

    # Market
    price_area: str = os.getenv("PRICE_AREA", "NO1")
    vat: float = float(os.getenv("VAT", "1.25"))

    # Battery
    battery_capacity_kwh: float = float(os.getenv("BATTERY_CAPACITY_KWH", "10"))
    battery_max_charge_kw: float = float(os.getenv("BATTERY_MAX_CHARGE_KW", "5"))
    battery_max_discharge_kw: float = float(os.getenv("BATTERY_MAX_DISCHARGE_KW", "5"))
    battery_efficiency: float = float(os.getenv("BATTERY_EFFICIENCY", "0.95"))
    min_soc: float = float(os.getenv("MIN_SOC", "10"))
    max_soc: float = float(os.getenv("MAX_SOC", "90"))

    # Strategy
    min_price_diff_nok: float = float(os.getenv("MIN_PRICE_DIFF_NOK", "0.30"))
    forecast_hours: int = int(os.getenv("FORECAST_HOURS", "24"))

    # Paths
    db_path: str = os.getenv("DB_PATH", "./data/profit.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


CONFIG = Config()
