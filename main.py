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
from victron_mqtt import VictronMQTT
from profit_tracker import ProfitTracker

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
        self.victron = VictronMQTT()
        self.tracker = ProfitTracker()
        self.running = False
        self.current_action: Optional[Action] = None

    def start(self):
        """Start the trading loop."""
        logger.info("Starting Energy Trader...")
        
        # Connect to Victron
        if not self.victron.connect():
            logger.error("Failed to connect to Victron MQTT. Check VICTRON_HOST.")
            sys.exit(1)
        
        logger.info(f"Connected. Waiting for SOC data...")
        time.sleep(2)  # Wait for initial data

        self.running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            self._main_loop()
        except Exception as e:
            logger.exception("Main loop error")
            self.stop()

    def _main_loop(self):
        """Run trading logic every hour."""
        last_hour = -1
        
        while self.running:
            now = datetime.now()
            
            # Run at start of each hour
            if now.hour != last_hour:
                last_hour = now.hour
                self._execute_trade_cycle()
            
            # Log status every 5 minutes
            if now.minute % 5 == 0:
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

            # Get optimal action
            action = self.optimizer.get_immediate_action(current, prices, soc)
            logger.info(f"Optimal action: {action.action} @ {action.power_kw:.1f}kW")

            # Execute
            self._execute_action(action, soc, current.price_nok_kwh)
            self.current_action = action

            # Log stats
            stats = self.tracker.get_stats()
            logger.info(f"Today's profit so far: {stats['today_profit_nok']:.2f} kr")

        except Exception as e:
            logger.exception("Trade cycle failed")

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
                logger.info(f"Discharging {abs(action.power_kw):.1f}kW")
        
        else:
            self.victron.stop_ess_control()
            logger.info("Idle - letting ESS manage itself")

    def _log_status(self):
        """Log current system status."""
        soc = self.victron.get_soc()
        grid = self.victron.grid_power
        battery = self.victron.battery_power
        logger.debug(f"Status SOC={soc}% Grid={grid}W Battery={battery}W")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info("Shutdown signal received")
        self.stop()

    def stop(self):
        """Stop trading and cleanup."""
        self.running = False
        logger.info("Stopping...")
        
        # Return control to ESS
        if self.victron._connected:
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
