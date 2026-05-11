"""Main controller for Victron Energy Trader."""
import os
import sys
import time
import signal
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import CONFIG, OSLO_TZ
from price_fetcher import PriceFetcher
from optimizer import Optimizer, Action
from victron_modbus import VictronModbus
from profit_tracker import ProfitTracker
from ha_qubino import QubinoReader, EVCSController

logging.basicConfig(
    level=getattr(logging, CONFIG.log_level),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.Formatter.converter = lambda *args: datetime.now(OSLO_TZ).timetuple()
logger = logging.getLogger(__name__)


class EnergyTrader:
    def __init__(self):
        self.price_fetcher = PriceFetcher()
        self.optimizer = Optimizer()
        self.victron = VictronModbus()
        self.qubino  = QubinoReader()
        self.evcs   = EVCSController()
        self.tracker = ProfitTracker()
        self.running = False
        self.current_action: Optional[Action] = None
        self._action_start_time: float = 0.0  # Tidspunkt da discharge startet (for ramp-up)
        self._last_price_count: int = 0        # Antall pristimer sist hentet (detekterer nye Nordpool-priser)

    def start(self):
        logger.info("Starting Energy Trader...")

        if not self.victron.connect():
            logger.error("Failed to connect to Victron Modbus-TCP. Check VICTRON_HOST and that Modbus-TCP is enabled on Cerbo GX.")
            logger.error("Aktiver Modbus-TCP: Settings -> Services -> Modbus-TCP -> Enabled")
            sys.exit(1)

        logger.info("Connected via Modbus-TCP. Reading SOC...")
        time.sleep(1)

        self.victron.stop_ess_control()
        logger.info("Startup-reset: reg37=0, Hub4Mode=2 (rydder etter evt. krasj)")

        mode = self.victron.get_ess_mode()
        logger.info(f"ESS modus: {mode} (2=Optimized, 4=ExternalControl)")
        if mode != self.victron.HUB4_MODE_OPTIMIZED:
            logger.warning(f"ESS modus er {mode}, forventet 2 (Optimized without BatteryLife)")

        self.victron.set_min_soc(CONFIG.min_soc)
        logger.info(f"ESS min SOC: {CONFIG.min_soc:.0f}%  max SOC: {CONFIG.max_soc:.0f}% (NMC 20-90%)")

        self._action_start_soc: Optional[float] = None
        self._last_price_nok: float = 0.0
        self.running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            self._main_loop()
        except Exception as e:
            logger.exception("Main loop error")
            self.stop()

    def _main_loop(self):
        last_hour = -1
        last_status_min = -1
        last_keepalive = 0.0  # Siste gang vi sendte ESS keepalive
        last_peak_shave = 0.0  # Siste peak-shave sjekk
        last_price_count = 0   # Antall tilgjengelige pristimer sist vi sjekket

        while self.running:
            now = datetime.now(OSLO_TZ)
            current_time = time.time()

            if now.hour != last_hour:
                last_hour = now.hour
                self._execute_trade_cycle()
                last_price_count = self._last_price_count

            # Re-planlegg hvis nye priser er blitt tilgjengelig (Nordpool ~kl 13:00)
            elif self._last_price_count > last_price_count:
                logger.info(f"Nye priser tilgjengelig ({last_price_count} → {self._last_price_count} timer) — re-planlegger")
                last_price_count = self._last_price_count
                self._execute_trade_cycle()

            if current_time - last_peak_shave >= 10:
                # Sjekk om nye Nordpool-priser er tilgjengelig (publiseres ~kl 13:00)
                try:
                    count = len(self.price_fetcher.get_prices(CONFIG.forecast_hours))
                    if count > self._last_price_count:
                        self._last_price_count = count
                except Exception:
                    pass
                self._check_peak_shaving()
                try:
                    grid_w  = self._get_grid_power() or 0
                    solar_w = self.victron.get_solar_power() or 0
                    bat_w   = self.victron.get_battery_power() or 0
                    act     = self.current_action.action if self.current_action else 'idle'
                    self.evcs.adjust_for_trading(
                        battery_action=act,
                        grid_kw=grid_w / 1000,
                        solar_kw=solar_w / 1000,
                        battery_kw=bat_w / 1000)
                except Exception:
                    pass
                last_peak_shave = current_time

            action_hour = self.current_action.timestamp.astimezone(OSLO_TZ).hour if self.current_action else -1
            if self.current_action and self.current_action.action != 'idle' and action_hour == now.hour:
                if current_time - last_keepalive >= 3:
                    if self.current_action.action == 'discharge':
                        # Vent 45s etter oppstart for ramp-up (MultiPlus + Qubino IT-nett trenger tid)
                        if current_time - self._action_start_time < 45:
                            self.victron.send_keepalive()
                            last_keepalive = current_time
                            continue
                        battery_w = self.victron.get_battery_power() or 0
                        discharge_w = abs(self.current_action.power_kw) * 1000
                        # Battery power er negativ ved utlading (Victron konvensjon)
                        # Hvis batteriet ikke leverer minst 40% av planlagt → noe er galt
                        if battery_w > -(discharge_w * 0.4):
                            logger.warning(
                                f"Export-guard: Batteri leverer kun {battery_w:.0f}W "
                                f"(forventer minst -{discharge_w*0.4:.0f}W) — stopper utlading"
                            )
                            self.victron.stop_ess_control()
                            self.current_action = None
                            self._action_start_soc = None
                        else:
                            self.victron.send_keepalive()
                    else:
                        self.victron.send_keepalive()
                    last_keepalive = current_time
            elif self.current_action and self.current_action.action != 'idle' and action_hour != now.hour:
                end_soc = self.victron.get_soc() or 0
                if self._action_start_soc is not None:
                    delta_soc = abs(end_soc - self._action_start_soc)
                    actual_kwh = CONFIG.battery_capacity_kwh * delta_soc / 100
                    if actual_kwh > 0.05:
                        act = self.current_action.action
                        db_action = "sell" if act == "discharge" else "buy"
                        self.tracker.log_trade(db_action, actual_kwh, self._last_price_nok)
                        logger.info(f"Handling ferdig: {act} {actual_kwh:.2f} kWh (SOC {self._action_start_soc:.1f}%->{end_soc:.1f}%)")
                logger.info(f"Action fra time {action_hour:02d} utgatt (na {now.hour:02d}) -- stopper ESS")
                self.victron.stop_ess_control()
                self.current_action = None
                self._action_start_soc = None

            if now.minute % 5 == 0 and now.minute != last_status_min:
                last_status_min = now.minute
                self._log_status()

            time.sleep(3)

    def _execute_trade_cycle(self):
        try:
            logger.info("=" * 50)
            logger.info(f"Trade cycle started at {datetime.now(OSLO_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

            soc = self.victron.get_soc()
            if soc is None:
                logger.warning("SOC unknown, waiting...")
                return
            logger.info(f"Current SOC: {soc:.1f}%")

            prices = self.price_fetcher.get_prices(CONFIG.forecast_hours)
            self._last_price_count = len(prices)
            current = prices[0] if prices else None

            if not current:
                logger.error("Could not fetch prices")
                return

            logger.info(f"Current price: {current.price_nok_kwh:.3f} kr/kWh")
            self._last_price_nok = current.price_nok_kwh

            solar_w = self.victron.get_solar_power() or 0
            solar_kw = solar_w / 1000.0
            if solar_kw > 0:
                logger.info(f"Sol-produksjon: {solar_kw:.2f} kW")

            action = self.optimizer.get_immediate_action(current, prices, soc, solar_kw)
            logger.info(f"Optimal action: {action.action} @ {action.power_kw:.1f}kW | {action.reason}")

            self._execute_action(action, soc, current.price_nok_kwh)
            self.current_action = action

            stats = self.tracker.get_stats()
            logger.info(f"Today's profit so far: {stats['today_profit_nok']:.2f} kr")

        except Exception as e:
            logger.exception("Trade cycle failed")

    def _get_grid_power(self) -> Optional[float]:
        """
        Hent total grid-effekt.
        Primar: Qubino _w_6 (alle 3 faser, IT-nett korrekt).
        Fallback: VM-3P75CT Modbus (L1+L2, mangler L3).
        """
        qpower = self.qubino.get_grid_power()
        if qpower:
            return qpower["total"]
        logger.debug("Qubino utilgjengelig -- fallback til VM-3P75CT (L1+L2)")
        return self.victron.get_grid_power()

    def _check_peak_shaving(self):
        try:
            grid_w = self._get_grid_power()
            soc = self.victron.get_soc()
            if grid_w is None or soc is None:
                return

            grid_kw = grid_w / 1000.0

            if grid_kw <= 0:
                return

            action = self.optimizer.peak_shave(grid_kw, soc)
            if action:
                logger.warning(
                    f"PEAK-SHAVING: Grid {grid_kw:.1f}kW > "
                    f"{self.optimizer.peak_limit_kw}kW. "
                    f"Utlader {abs(action.power_kw):.1f}kW fra batteri."
                )
                self.victron.set_discharge_power(abs(action.power_kw))
                self.current_action = action
        except Exception as e:
            logger.debug(f"Peak-shave sjekk feilet: {e}")

    def _execute_action(self, action: Action, soc: float, price: float):
        if action.action == 'charge':
            if soc >= CONFIG.max_soc:
                logger.info("SOC at max, skipping charge")
                self.victron.stop_ess_control()
                return

            # Peak-limit-koordinering: les live grid og cap ladeeffekt
            # slik at total (grid + lading) ikke overstiger peak_limit_kw
            grid_w = self._get_grid_power() or 0
            grid_kw = grid_w / 1000.0
            headroom_kw = max(0.0, CONFIG.peak_limit_kw - max(0.0, grid_kw))
            charge_kw = min(action.power_kw, headroom_kw)
            if charge_kw < 0.5:
                logger.info(f"Charge blokkert av peak-limit: grid={grid_kw:.1f}kW, headroom={headroom_kw:.1f}kW")
                self.victron.stop_ess_control()
                return

            if charge_kw < action.power_kw:
                logger.info(f"Charge cappet {action.power_kw:.1f}kW -> {charge_kw:.1f}kW (peak-limit {CONFIG.peak_limit_kw}kW)")

            success = self.victron.set_charge_power(charge_kw)
            if success:
                self._action_start_soc = soc
                logger.info(f"Charging {charge_kw:.1f}kW")
                self.current_action = action

        elif action.action == 'discharge':
            if soc <= CONFIG.min_soc:
                logger.info("SOC at min, skipping discharge")
                self.victron.stop_ess_control()
                return

            success = self.victron.set_discharge_power(abs(action.power_kw))
            if success:
                self._action_start_soc = soc
                self._action_start_time = time.time()
                logger.info(f"Discharging {abs(action.power_kw):.1f}kW | {action.reason}")
                self.current_action = action

        else:
            self.victron.stop_ess_control()
            self.current_action = None
            logger.info("Idle - ESS styrer selv")

    def _log_status(self):
        soc  = self.victron.get_soc()
        grid = self._get_grid_power()
        solar = self.victron.get_solar_power()
        logger.info(f"Status: SOC={soc}% Grid={grid}W Sol={solar}W")

    def _signal_handler(self, signum, frame):
        logger.info("Shutdown signal received")
        self.stop()

    def stop(self):
        self.running = False
        logger.info("Stopping...")
        self.evcs.restore_auto()
        if hasattr(self.victron, '_connected') and self.victron._connected:
            self.victron.stop_ess_control()
            time.sleep(1)
            self.victron.disconnect()
        logger.info("Stopped")


def main():
    if not os.getenv("VICTRON_HOST"):
        print("Error: Set VICTRON_HOST in .env file")
        print("cp .env.example .env")
        sys.exit(1)

    trader = EnergyTrader()
    trader.start()


if __name__ == "__main__":
    main()
