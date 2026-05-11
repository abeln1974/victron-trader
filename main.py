"""Main controller for Victron Energy Trader."""
import os
import sys
import time
import signal
import logging
from datetime import datetime
from typing import Optional

from config import CONFIG
from price_fetcher import PriceFetcher
from optimizer import Optimizer, Action
from victron_modbus import VictronModbus
from profit_tracker import ProfitTracker
from ha_qubino import QubinoReader

# Setup logging
logging.basicConfig(
    level=getattr(logging, CONFIG.log_level),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class EnergyTrader:
    def __init__(self):
        self.price_fetcher = PriceFetcher()
        self.optimizer = Optimizer()
        self.victron = VictronModbus()
        self.qubino  = QubinoReader()   # Primærkilde grid total (inkl L3, via _w_6)
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

        while self.running:
            now = datetime.now()

            # Kjør handelslogikk ved start av hver time
            if now.hour != last_hour:
                last_hour = now.hour
                self._execute_trade_cycle()

            # Peak-shaving: sjekk grid-effekt hvert 10. sekund
            self._check_peak_shaving()

            # Log status hvert 5. minutt
            if now.minute % 5 == 0 and now.minute != last_status_min:
                last_status_min = now.minute
                self._log_status()

            time.sleep(10)

    def _execute_trade_cycle(self):
        """Execute one trading cycle."""
        try:
            logger.info("=" * 50)
            logger.info(f"Trade cycle started at {datetime.now()}")
            
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
                self.tracker.log_trade("peak_shave", abs(action.power_kw), 0)
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
                self.tracker.log_trade("buy", energy_kwh, price)
                logger.info(f"Charging {action.power_kw:.1f}kW")
            
        elif action.action == 'discharge':
            if soc <= CONFIG.min_soc:
                logger.info("SOC at min, skipping discharge")
                self.victron.stop_ess_control()
                return

            success = self.victron.set_discharge_power(abs(action.power_kw))
            if success:
                self.tracker.log_trade("sell", energy_kwh, price)
                logger.info(f"Discharging {abs(action.power_kw):.1f}kW | {action.reason}")

        else:
            self.victron.stop_ess_control()
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
        
        # Return control to ESS
        if hasattr(self.victron, '_connected') and self.victron._connected:
            self.victron.stop_ess_control()
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
