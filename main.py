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
from tariff import sell_price_ore, buy_price_ore

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
        self._action_start_time: float = 0.0
        self._last_price_count: int = 0
        self._original_charge_kw: float = 0.0  # Ladeeffekt ved time-start — brukes av peak-shave som referanse

    def start(self):
        logger.info("Starting Energy Trader...")

        if not self.victron.connect():
            logger.error("Failed to connect to Victron Modbus-TCP.")
            sys.exit(1)

        logger.info("Connected via Modbus-TCP. Reading SOC...")
        time.sleep(1)

        self.victron.stop_ess_control()
        logger.info("Startup-reset: reg37=0, Hub4Mode=2")

        mode = self.victron.get_ess_mode()
        logger.info(f"ESS modus: {mode} (2=Optimized, 4=ExternalControl)")
        if mode != self.victron.HUB4_MODE_OPTIMIZED:
            logger.warning(f"ESS modus er {mode}, forventet 2")

        self.victron.set_min_soc(CONFIG.min_soc)
        logger.info(f"ESS min SOC: {CONFIG.min_soc:.0f}%  max SOC: {CONFIG.max_soc:.0f}%")

        self._action_start_soc: Optional[float] = None
        self._action_start_counters: Optional[tuple] = None  # (discharged_kwh, charged_kwh) ved action-start
        self._last_price_nok: float = 0.0
        self.running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            self._main_loop()
        except Exception:
            logger.exception("Main loop error")
            self.stop()

    def _main_loop(self):
        last_hour = -1
        last_status_min = -1
        last_keepalive = 0.0
        last_peak_shave = 0.0
        last_price_count = 0
        self._dvcc_charging_stopped = False  # Sporer om vi har satt DVCC 0A

        while self.running:
            now = datetime.now(OSLO_TZ)
            current_time = time.time()

            if now.hour != last_hour:
                last_hour = now.hour
                self._execute_trade_cycle()
                last_price_count = self._last_price_count

            elif self._last_price_count > last_price_count:
                logger.info(f"Nye priser ({last_price_count}→{self._last_price_count} timer) — re-planlegger")
                last_price_count = self._last_price_count
                self._execute_trade_cycle()

            if current_time - last_peak_shave >= 10:
                try:
                    count = len(self.price_fetcher.get_prices(CONFIG.forecast_hours))
                    if count > self._last_price_count:
                        self._last_price_count = count
                except Exception:
                    pass
                self._check_peak_shaving()
                self._enforce_max_soc()
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
                        if current_time - self._action_start_time < 45:
                            self.victron.send_keepalive()
                            last_keepalive = current_time
                            time.sleep(3)
                            continue
                        battery_w = self.victron.get_battery_power() or 0
                        discharge_w = abs(self.current_action.power_kw) * 1000
                        if battery_w > -(discharge_w * 0.4):
                            logger.warning(
                                f"Export-guard: Batteri {battery_w:.0f}W (forventer <-{discharge_w*0.4:.0f}W) — stopper"
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
                act = self.current_action.action
                actual_kwh = 0.0
                kwh_source = "soc-delta"
                # Foretrekk SmartShunt energitellere (reg 309/310) over SOC-delta
                end_counters = self.victron.get_energy_counters()
                if end_counters and self._action_start_counters:
                    start_dis, start_chg = self._action_start_counters
                    end_dis, end_chg = end_counters
                    if act == 'discharge':
                        actual_kwh = max(0.0, end_dis - start_dis)
                    else:
                        actual_kwh = max(0.0, end_chg - start_chg)
                    kwh_source = "smartshunt"
                elif self._action_start_soc is not None:
                    delta_soc = abs(end_soc - self._action_start_soc)
                    actual_kwh = CONFIG.battery_capacity_kwh * delta_soc / 100
                if actual_kwh > 0.05:
                    db_action = "sell" if act == "discharge" else "buy"
                    spot_eks_mva = self._last_price_nok / CONFIG.vat
                    price_nok = (sell_price_ore(spot_eks_mva * 100) if db_action == "sell"
                                 else buy_price_ore(spot_eks_mva * 100, datetime.now(OSLO_TZ).hour)) / 100
                    self.tracker.log_trade(db_action, actual_kwh, price_nok)
                    logger.info(f"Handling ferdig: {act} {actual_kwh:.2f} kWh [{kwh_source}] (SOC {self._action_start_soc:.1f}%→{end_soc:.1f}%)")
                logger.info(f"Action fra time {action_hour:02d} utgatt (nå {now.hour:02d}) — stopper ESS")
                self.victron.stop_ess_control()
                self.current_action = None
                self._action_start_soc = None
                self._action_start_counters = None
                self._original_charge_kw = 0.0

            if now.minute % 5 == 0 and now.minute != last_status_min:
                last_status_min = now.minute
                self._log_status()

            time.sleep(3)

    def _enforce_max_soc(self):
        """Håndhev max SOC — hold batteriet i float ved >= max_soc.

        AC-koblet Fronius påvirkes ikke av DVCC. Riktig løsning er å sikre at
        trader ikke aktivt lader når SOC >= max_soc, og ellers la Mode 2 flyte:
        - Fronius dekker husforbruk direkte
        - Overskudd eksporteres naturlig via Victron ESS
        - Batteriet synker sakte av husforbruk til under 89%
        Kjøres hvert 10s fra peak-shave-sløyfen.
        """
        soc = self.victron.get_soc()
        if soc is None:
            return

        if soc >= CONFIG.max_soc and not self._dvcc_charging_stopped:
            logger.info(f"SOC {soc:.1f}% >= {CONFIG.max_soc}% — float: ESS Mode 2, Fronius styrer (NMC-vern)")
            self.victron.stop_ess_control()  # Sikrer Mode 2 — ingen aktiv lading fra trader
            self._dvcc_charging_stopped = True
        elif soc < CONFIG.max_soc - 1.0 and self._dvcc_charging_stopped:
            logger.info(f"SOC {soc:.1f}% < {CONFIG.max_soc - 1.0}% — lading tillatt igjen")
            self._dvcc_charging_stopped = False

    def _execute_trade_cycle(self):
        try:
            logger.info("=" * 50)
            logger.info(f"Trade cycle {datetime.now(OSLO_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

            soc = self.victron.get_soc()
            if soc is None:
                logger.warning("SOC ukjent, venter...")
                return
            logger.info(f"SOC: {soc:.1f}%")

            prices = self.price_fetcher.get_prices(CONFIG.forecast_hours)
            self._last_price_count = len(prices)
            current = prices[0] if prices else None
            if not current:
                logger.error("Ingen priser tilgjengelig")
                return

            logger.info(f"Spotpris: {current.price_nok_kwh:.3f} kr/kWh")
            self._last_price_nok = current.price_nok_kwh

            solar_w = self.victron.get_solar_power() or 0
            solar_kw = solar_w / 1000.0
            if solar_kw > 0:
                logger.info(f"Sol: {solar_kw:.2f} kW")

            action = self.optimizer.get_immediate_action(current, prices, soc, solar_kw)
            logger.info(f"Action: {action.action} @ {action.power_kw:.1f}kW | {action.reason}")

            prev_action = self.current_action
            self._execute_action(action, soc, current.price_nok_kwh)
            # Les energitellere ved start av ny aktiv action (ikke ved idle)
            if action.action != 'idle' and (prev_action is None or prev_action.action == 'idle'):
                self._action_start_counters = self.victron.get_energy_counters()
            self.current_action = action

            stats = self.tracker.get_stats()
            logger.info(f"Dagens profitt: {stats['today_profit_nok']:.2f} kr")

        except Exception:
            logger.exception("Trade cycle feilet")

    def _get_grid_power(self) -> Optional[float]:
        qpower = self.qubino.get_grid_power()
        if qpower:
            return qpower["total"]
        logger.debug("Qubino utilgjengelig — fallback VM-3P75CT")
        return self.victron.get_grid_power()

    def _check_peak_shaving(self):
        try:
            grid_w = self._get_grid_power()
            soc = self.victron.get_soc()
            if grid_w is None or soc is None:
                return

            grid_kw = grid_w / 1000.0
            peak_kw = self.optimizer.peak_limit_kw

            if grid_kw <= peak_kw + 0.3:
                return

            if self.current_action and self.current_action.action == 'charge':
                # Bruk _original_charge_kw som referanse — ikke current_action.power_kw
                # som kan ha blitt redusert av forrige peak-shave-kall (kumulativ jaging)
                ref_kw = self._original_charge_kw or self.current_action.power_kw
                other_load_kw = grid_kw - ref_kw
                new_charge_kw = max(0.0, round(peak_kw - other_load_kw, 1))
                if new_charge_kw < 0.5:
                    logger.warning(f"PEAK-SHAVING: Grid {grid_kw:.1f}kW > {peak_kw}kW — stopper lading")
                    self.victron.stop_ess_control()
                    self.current_action = None
                    self._original_charge_kw = 0.0
                else:
                    logger.warning(
                        f"PEAK-SHAVING: Grid {grid_kw:.1f}kW > {peak_kw}kW — "
                        f"lading {ref_kw:.1f}kW → {new_charge_kw:.1f}kW (last={other_load_kw:.1f}kW)"
                    )
                    self.victron.set_charge_power(new_charge_kw)
                    # IKKE oppdater current_action.power_kw — behold original som referanse
            else:
                # Beregn nødvendig utlading for å komme under peak_limit
                # Unngå å utlade mer enn nødvendig (forhindrer eksport til nett)
                needed_discharge_kw = max(0.0, round(grid_kw - peak_kw + 0.3, 1))
                
                # Ikke utlad mer enn nødvendig, og maks 10kW
                discharge_kw = min(needed_discharge_kw, 10.0)
                
                if discharge_kw < 0.5:
                    logger.debug(f"PEAK-SHAVING: Grid {grid_kw:.1f}kW > {peak_kw}kW, men for lite til utlading ({discharge_kw:.1f}kW)")
                    return
                
                logger.warning(
                    f"PEAK-SHAVING: Grid {grid_kw:.1f}kW > {peak_kw}kW — "
                    f"utlader {discharge_kw:.1f}kW (minimum nødvendig)"
                )
                self.victron.set_discharge_power(discharge_kw)
                
                # Lagre action for tracking
                self.current_action = Action(
                    timestamp=datetime.now(OSLO_TZ),
                    action='peak_shave',
                    power_kw=-discharge_kw,
                    expected_profit_nok=0.0,
                    reason=f'Grid {grid_kw:.1f}kW > {peak_kw}kW minimum shave'
                )
        except Exception as e:
            logger.debug(f"Peak-shave feilet: {e}")

    def _execute_action(self, action: Action, soc: float, price: float):
        if action.action == 'charge':
            if soc >= CONFIG.max_soc:
                logger.info("SOC ved maks, hopper over lading")
                self.victron.stop_ess_control()
                return

            grid_w = self._get_grid_power() or 0
            grid_kw = grid_w / 1000.0
            headroom_kw = max(0.0, CONFIG.peak_limit_kw - max(0.0, grid_kw))
            charge_kw = min(action.power_kw, headroom_kw)
            if charge_kw < 0.5:
                logger.info(f"Lading blokkert av peak-limit: grid={grid_kw:.1f}kW, headroom={headroom_kw:.1f}kW")
                self.victron.stop_ess_control()
                return

            if charge_kw < action.power_kw:
                logger.info(f"Lading cappet {action.power_kw:.1f}kW → {charge_kw:.1f}kW (peak {CONFIG.peak_limit_kw}kW)")

            success = self.victron.set_charge_power(charge_kw)
            if success:
                self._action_start_soc = soc
                self._original_charge_kw = charge_kw  # Lagre som fast referanse for peak-shave
                logger.info(f"Lader {charge_kw:.1f}kW")
                self.current_action = action

        elif action.action == 'discharge':
            if soc <= CONFIG.min_soc:
                logger.info("SOC ved min, hopper over utlading")
                self.victron.stop_ess_control()
                return

            success = self.victron.set_discharge_power(abs(action.power_kw))
            if success:
                self._action_start_soc = soc
                self._action_start_time = time.time()
                self._original_charge_kw = 0.0
                logger.info(f"Utlader {abs(action.power_kw):.1f}kW | {action.reason}")
                self.current_action = action

        else:
            self.victron.stop_ess_control()
            self.current_action = None
            self._original_charge_kw = 0.0
            logger.info("Idle — ESS styrer selv")

    def _log_status(self):
        soc   = self.victron.get_soc()
        grid  = self._get_grid_power()
        solar = self.victron.get_solar_power()
        logger.info(f"Status: SOC={soc}% Grid={grid}W Sol={solar}W")

    def _signal_handler(self, signum, frame):
        logger.info("Shutdown signal")
        self.stop()

    def stop(self):
        self.running = False
        logger.info("Stopper...")
        self.evcs.restore_auto()
        if hasattr(self.victron, '_connected') and self.victron._connected:
            self.victron.stop_ess_control()
            time.sleep(1)
            self.victron.disconnect()
        logger.info("Stoppet")


def main():
    if not os.getenv("VICTRON_HOST"):
        print("Error: Sett VICTRON_HOST i .env")
        sys.exit(1)
    EnergyTrader().start()


if __name__ == "__main__":
    main()
