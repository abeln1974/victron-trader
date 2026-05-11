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

# Setup logging med norsk tid
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
        self.qubino  = QubinoReader()   # Primærkilde grid total (inkl L3, via _w_6)
        self.evcs   = EVCSController()     # Elbil-lader styring via HA
        self.tracker = ProfitTracker()
        self.running = False
        self.current_action: Optional[Action] = None

    def start(self):
        """Start the trading loop."""
        logger.info("Starting Energy Trader...")
        
        # Connect to Victron via Modbus-TCP
        if not self.victron.connect():
            logger.error("Failed to connect to Victron Modbus-TCP. Check VICTRON_HOST and that Modbus-TCP is enabled on Cerbo GX.")
            logger.error("Aktiver Modbus-TCP: Settings → Services → Modbus-TCP → Enabled")
            sys.exit(1)
        
        logger.info(f"Connected via Modbus-TCP. Reading SOC...")
        time.sleep(1)

        # Startup-reset: nullstill alltid reg37 og Hub4Mode=2 ved oppstart.
        # Rydder opp etter eventuell krasj der Hub4Mode=3 ble stående.
        self.victron.stop_ess_control()
        logger.info("Startup-reset: reg37=0, Hub4Mode=2 (rydder etter evt. krasj)")

        mode = self.victron.get_ess_mode()
        logger.info(f"ESS modus: {mode} (2=Optimized, 4=ExternalControl)")
        if mode != self.victron.HUB4_MODE_OPTIMIZED:
            logger.warning(f"ESS modus er {mode}, forventet 2 (Optimized without BatteryLife)")

        # Sett min SOC — NMC: ikke utlad under 20%
        self.victron.set_min_soc(CONFIG.min_soc)
        logger.info(f"ESS min SOC: {CONFIG.min_soc:.0f}%  max SOC: {CONFIG.max_soc:.0f}% (NMC 20-90%)")

        self._action_start_soc: Optional[float] = None  # SOC ved start av aktiv handling
        self._last_price_nok: float = 0.0               # Siste spotpris (kr/kWh)
        self.running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            self._main_loop()
        except Exception as e:
            logger.exception("Main loop error")
            self.stop()

    def _main_loop(self):
        """Run trading logic every hour + peak-shaving every 10 seconds."""
        last_hour = -1
        last_status_min = -1
        last_keepalive = 0.0  # Siste gang vi sendte ESS keepalive
        last_peak_shave = 0.0  # Siste peak-shave sjekk

        while self.running:
            now = datetime.now(OSLO_TZ)
            current_time = time.time()

            # Kjør handelslogikk ved start av hver time
            if now.hour != last_hour:
                last_hour = now.hour
                self._execute_trade_cycle()

            # Peak-shaving + EVCS-koordinering hvert 10. sekund
            if current_time - last_peak_shave >= 10:
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

            # ESS keepalive: Mode 3 via VE.Bus reg 37 krever skriving hvert ~10s.
            # Vi sender hvert 3s for sikkerhet — men kun hvis action gjelder nåværende time
            action_hour = self.current_action.timestamp.astimezone(OSLO_TZ).hour if self.current_action else -1
            if self.current_action and self.current_action.action != 'idle' and action_hour == now.hour:
                if current_time - last_keepalive >= 3:
                    # Export-guard: sjekk at vi faktisk eksporterer til nett.
                    # Hvis lokalt forbruk (elbil etc) spiser opp batteriet, stopp utlading.
                    if self.current_action.action == 'discharge':
                        grid_w = self._get_grid_power() or 0
                        discharge_w = abs(self.current_action.power_kw) * 1000
                        # Hvis grid ikke er negativ nok → forbruket spiser batteriet lokalt
                        if grid_w > -(discharge_w * 0.6):  # Toleranse: 60% kan gå til lokalt forbruk
                            logger.warning(
                                f"Export-guard: Grid {grid_w:.0f}W — lokalt forbruk for høyt, "
                                f"stopper utlading (elbil/last?) — grid={grid_w:.0f}W, grense={-(discharge_w*0.6):.0f}W"
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
                # Action tilhører en annen time — logg faktisk kWh basert på SOC-endring og stopp
                end_soc = self.victron.get_soc() or 0
                if self._action_start_soc is not None:
                    delta_soc = abs(end_soc - self._action_start_soc)
                    actual_kwh = CONFIG.battery_capacity_kwh * delta_soc / 100
                    if actual_kwh > 0.05:
                        act = self.current_action.action
                        db_action = "sell" if act == "discharge" else "buy"
                        self.tracker.log_trade(db_action, actual_kwh, self._last_price_nok)
                        logger.info(f"Handling ferdig: {act} {actual_kwh:.2f} kWh (SOC {self._action_start_soc:.1f}%→{end_soc:.1f}%)")
                logger.info(f"Action fra time {action_hour:02d} utgått (nå {now.hour:02d}) — stopper ESS")
                self.victron.stop_ess_control()
                self.current_action = None
                self._action_start_soc = None

            # Log status hvert 5. minutt
            if now.minute % 5 == 0 and now.minute != last_status_min:
                last_status_min = now.minute
                self._log_status()

            time.sleep(3)

    def _execute_trade_cycle(self):
        """Execute one trading cycle."""
        try:
            logger.info("=" * 50)
            logger.info(f"Trade cycle started at {datetime.now(OSLO_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
            
            # Get current state
            soc = self.victron.get_soc()
            if soc is None:
                logger.warning("SOC unknown, waiting...")
                return
            logger.info(f"Current SOC: {soc:.1f}%")

            # Get prices
            prices = self.price_fetcher.get_prices(CONFIG.forecast_hours)
            current = prices[0] if prices else None
            
            if not current:
                logger.error("Could not fetch prices")
                return
            
            logger.info(f"Current price: {current.price_nok_kwh:.3f} kr/kWh")
            self._last_price_nok = current.price_nok_kwh

            # Les sol-produksjon
            solar_w = self.victron.get_solar_power() or 0
            solar_kw = solar_w / 1000.0
            if solar_kw > 0:
                logger.info(f"Sol-produksjon: {solar_kw:.2f} kW")

            # Get optimal action
            action = self.optimizer.get_immediate_action(current, prices, soc, solar_kw)
            logger.info(f"Optimal action: {action.action} @ {action.power_kw:.1f}kW | {action.reason}")

            # Execute
            self._execute_action(action, soc, current.price_nok_kwh)
            self.current_action = action

            # Log stats
            stats = self.tracker.get_stats()
            logger.info(f"Today's profit so far: {stats['today_profit_nok']:.2f} kr")

        except Exception as e:
            logger.exception("Trade cycle failed")

    def _get_grid_power(self) -> Optional[float]:
        """
        Hent total grid-effekt.
        Primær: Qubino _w_6 (alle 3 faser, IT-nett korrekt).
        Fallback: VM-3P75CT Modbus (L1+L2, mangler L3).
        """
        qpower = self.qubino.get_grid_power()
        if qpower:
            return qpower["total"]
        logger.debug("Qubino utilgjengelig — fallback til VM-3P75CT (L1+L2)")
        return self.victron.get_grid_power()

    def _check_peak_shaving(self):
        """Sjekk om vi må peak-shave basert på nåværende grid-effekt."""
        try:
            grid_w = self._get_grid_power()
            soc = self.victron.get_soc()
            if grid_w is None or soc is None:
                return

            grid_kw = grid_w / 1000.0

            # Bare peak-shave ved import (positiv = trekker fra nett)
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
                # Sett current_action for keepalive
                self.current_action = action
        except Exception as e:
            logger.debug(f"Peak-shave sjekk feilet: {e}")

    def _execute_action(self, action: Action, soc: float, price: float):
        """Execute the trading action on Victron."""
        energy_kwh = abs(action.power_kw)  # Approximate for 1 hour
        
        if action.action == 'charge':
            # Check SOC limits
            if soc >= CONFIG.max_soc:
                logger.info("SOC at max, skipping charge")
                self.victron.stop_ess_control()
                return
            
            success = self.victron.set_charge_power(action.power_kw)
            if success:
                self._action_start_soc = soc  # Merk start-SOC
                logger.info(f"Charging {action.power_kw:.1f}kW")
                self.current_action = action  # Sett for keepalive
            
        elif action.action == 'discharge':
            if soc <= CONFIG.min_soc:
                logger.info("SOC at min, skipping discharge")
                self.victron.stop_ess_control()
                return

            success = self.victron.set_discharge_power(abs(action.power_kw))
            if success:
                self._action_start_soc = soc  # Merk start-SOC
                logger.info(f"Discharging {abs(action.power_kw):.1f}kW | {action.reason}")
                self.current_action = action  # Sett for keepalive

        else:
            self.victron.stop_ess_control()
            self.current_action = None  # Nullstill for keepalive
            logger.info("Idle - ESS styrer selv")

    def _log_status(self):
        """Log current system status."""
        soc  = self.victron.get_soc()
        grid = self._get_grid_power()
        solar = self.victron.get_solar_power()
        logger.info(f"Status: SOC={soc}% Grid={grid}W Sol={solar}W")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info("Shutdown signal received")
        self.stop()

    def stop(self):
        """Stop trading and cleanup."""
        self.running = False
        logger.info("Stopping...")
        
        # Nullstill setpoint og tilbakestill ESS til Mode 2
        self.evcs.restore_auto()  # Tilbakestill EVCS til auto ved shutdown
        if hasattr(self.victron, '_connected') and self.victron._connected:
            self.victron.stop_ess_control()  # Tilbake til Hub4Mode=2 og nullstill reg 37
            time.sleep(1)
            self.victron.disconnect()
        
        logger.info("Stopped")


def main():
    # Check required config
    if not os.getenv("VICTRON_HOST"):
        print("Error: Set VICTRON_HOST in .env file")
        print("cp .env.example .env")
        sys.exit(1)

    trader = EnergyTrader()
    trader.start()


if __name__ == "__main__":
    main()
