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
from solar_forecast import get_solar_kwh_tomorrow

logging.basicConfig(
    level=getattr(logging, CONFIG.log_level),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.Formatter.converter = lambda *args: datetime.now(OSLO_TZ).timetuple()
logger = logging.getLogger(__name__)


class EnergyTrader:
    _STATE_FILE = os.path.join(os.path.dirname(os.getenv("DB_PATH", "./data/profit.db")), "trader_state.json")

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
        self._original_charge_kw: float = 0.0

    def _save_state(self):
        """Lagre current_action til disk så den overlever restart."""
        try:
            if self.current_action and self.current_action.action != 'idle':
                state = {
                    "action": self.current_action.action,
                    "power_kw": self.current_action.power_kw,
                    "timestamp": self.current_action.timestamp.isoformat(),
                    "action_start_soc": getattr(self, '_action_start_soc', None),
                    "last_price_nok": getattr(self, '_last_price_nok', 0.0),
                }
                os.makedirs(os.path.dirname(self._STATE_FILE) or ".", exist_ok=True)
                with open(self._STATE_FILE, "w") as f:
                    import json
                    json.dump(state, f)
            else:
                if os.path.exists(self._STATE_FILE):
                    os.remove(self._STATE_FILE)
        except Exception as e:
            logger.debug(f"_save_state feilet: {e}")

    def _restore_state(self):
        """Gjenopprett action fra forrige kjøring og logg eventuell handel."""
        try:
            if not os.path.exists(self._STATE_FILE):
                return
            import json
            with open(self._STATE_FILE) as f:
                state = json.load(f)
            prev_action = state.get("action")
            prev_hour = datetime.fromisoformat(state["timestamp"]).astimezone(OSLO_TZ).hour
            now_hour = datetime.now(OSLO_TZ).hour
            prev_start_soc = state.get("action_start_soc")
            prev_price_nok = state.get("last_price_nok", 0.0)

            logger.info(f"Gjenopprettet state: {prev_action} fra time {prev_hour:02d} (nå {now_hour:02d})")

            # Hvis handlingen er fra en annen time — logg den som ferdig
            if prev_action in ('charge', 'discharge') and prev_hour != now_hour:
                end_soc = self.victron.get_soc() or 0
                if prev_start_soc is not None:
                    delta_soc = abs(end_soc - prev_start_soc)
                    actual_kwh = CONFIG.battery_capacity_kwh * delta_soc / 100
                    if actual_kwh > 0.05:
                        db_action = "sell" if prev_action == "discharge" else "buy"
                        spot_eks_mva = prev_price_nok / CONFIG.vat
                        price_nok = (sell_price_ore(spot_eks_mva * 100) if db_action == "sell"
                                     else buy_price_ore(spot_eks_mva * 100, prev_hour)) / 100
                        self.tracker.log_trade(db_action, actual_kwh, price_nok)
                        logger.info(f"Gjenopprettet handel logget: {prev_action} {actual_kwh:.2f} kWh "
                                    f"(SOC {prev_start_soc:.1f}%→{end_soc:.1f}%)")

            os.remove(self._STATE_FILE)
        except Exception as e:
            logger.warning(f"_restore_state feilet: {e}")

    def start(self):
        logger.info("Starting Energy Trader...")

        if not self.victron.connect():
            logger.error("Failed to connect to Victron Modbus-TCP.")
            sys.exit(1)

        logger.info("Connected via Modbus-TCP. Reading SOC...")
        time.sleep(1)

        self.victron.stop_ess_control()
        logger.info("Startup-reset: reg37=0, Hub4Mode=3 (trader tar kontroll)")

        mode = self.victron.get_ess_mode()
        if mode != self.victron.HUB4_MODE_DISABLED:
            logger.warning(f"ESS modus er {mode}, forventet 3 — forsøker å sette Mode 3")
            self.victron.enable_external_control()

        self.victron.set_min_soc(CONFIG.min_soc)
        logger.info(f"ESS min SOC: {CONFIG.min_soc:.0f}%  max SOC: {CONFIG.max_soc:.0f}%")

        self._action_start_soc: Optional[float] = None
        self._action_start_counters: Optional[tuple] = None
        self._last_price_nok: float = 0.0
        self.running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._restore_state()

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
        last_reconnect_attempt = 0.0
        self._dvcc_charging_stopped = False  # Sporer om vi har satt DVCC 0A

        while self.running:
            now = datetime.now(OSLO_TZ)
            current_time = time.time()

            # Reconnect ved Modbus-feil (maks hvert 30s)
            if not self.victron._connected:
                if current_time - last_reconnect_attempt >= 30:
                    last_reconnect_attempt = current_time
                    logger.warning("Modbus ikke tilkoblet — forsøker reconnect...")
                    if self.victron.connect():
                        logger.info("Modbus reconnect OK")
                        self.victron.set_min_soc(CONFIG.min_soc)
                    else:
                        logger.error("Modbus reconnect feilet — venter 30s")
                        time.sleep(3)
                        continue
                else:
                    time.sleep(3)
                    continue

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

            # Keepalive: send setpoint hvert 8s uansett — holder Mode 3 aktiv
            # Ved krasj stopper keepalive → Passthru → Victron tar Mode 2 automatisk
            if current_time - last_keepalive >= 8:
                self.victron.send_keepalive()
                last_keepalive = current_time

            action_hour = self.current_action.timestamp.astimezone(OSLO_TZ).hour if self.current_action else -1
            if self.current_action and self.current_action.action != 'idle' and action_hour == now.hour:
                if self.current_action.action == 'discharge':
                    if current_time - self._action_start_time < 45:
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
                    # Fallback til SOC-delta hvis SmartShunt gir 0 (reg 310 teller ikke alltid)
                    if actual_kwh < 0.05 and self._action_start_soc is not None:
                        delta_soc = abs(end_soc - self._action_start_soc)
                        actual_kwh = CONFIG.battery_capacity_kwh * delta_soc / 100
                        kwh_source = "soc-delta-fallback"
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
            logger.info(f"SOC {soc:.1f}% >= {CONFIG.max_soc}% — float: setpoint=0, Mode 3 beholdes (NMC-vern)")
            self.victron.stop_ess_control()  # setpoint → 0, Mode 3 beholdes
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
            # Hent fersk SOC rett før execute for å unngå utdatert data
            fresh_soc = self.victron.get_soc()
            if fresh_soc is None:
                fresh_soc = soc
            self._execute_action(action, fresh_soc, current.price_nok_kwh)
            # Les energitellere ved start av ny aktiv action (ikke ved idle)
            if action.action != 'idle' and (prev_action is None or prev_action.action == 'idle'):
                self._action_start_counters = self.victron.get_energy_counters()
            self.current_action = action
            self._save_state()

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

            # KRITISK: Kontinuerlig MIN_SOC beskyttelse med storm mode
            # Sjekk storm mode status (samme logikk som optimizer)
<<<<<<< HEAD
=======
            from solar_forecast import get_solar_kwh_tomorrow
>>>>>>> 57224e1 (docs: Complete system analysis with DESS comparison)
            solar_kwh_tomorrow = get_solar_kwh_tomorrow(CONFIG.site_lat, CONFIG.site_lon, CONFIG.solar_max_kw, CONFIG.solar_system_efficiency)
            storm_mode = solar_kwh_tomorrow < CONFIG.storm_mode_threshold_kwh
            effective_min_soc = CONFIG.storm_mode_min_soc if storm_mode else CONFIG.min_soc
            
            if soc < effective_min_soc:
                mode_str = "STORM MODE" if storm_mode else "NORMAL"
                logger.warning(f"{mode_str} MIN_SOC BESKYTTELSE: SOC {soc:.1f}% < {effective_min_soc}% - STOPPER DISCHARGE")
                if self.current_action and self.current_action.action == 'discharge':
                    self.victron.stop_ess_control()
                    self.current_action = None
                    logger.info(f"Emergency stop: SOC under {mode_str.lower()} MIN_SOC")
                # IKKE returner her - tillat peak shaving selv under MIN_SOC hvis batteriet har kapasitet

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
            # Sjekk storm mode status for MIN_SOC
            from solar_forecast import get_solar_kwh_tomorrow
            solar_kwh_tomorrow = get_solar_kwh_tomorrow(CONFIG.site_lat, CONFIG.site_lon, CONFIG.solar_max_kw, CONFIG.solar_system_efficiency)
            storm_mode = solar_kwh_tomorrow < CONFIG.storm_mode_threshold_kwh
            effective_min_soc = CONFIG.storm_mode_min_soc if storm_mode else CONFIG.min_soc
            mode_str = "STORM" if storm_mode else "NORMAL"
            
            logger.info(f"CHARGE CHECK: SOC {soc:.1f}% vs {mode_str} MIN_SOC {effective_min_soc:.1f}% vs MAX_SOC {CONFIG.max_soc:.1f}%")
            if soc >= CONFIG.max_soc:
                logger.info("SOC ved maks, hopper over lading")
                self.victron.stop_ess_control()
                return
            # Tillat lading når SOC < effective_min_soc - batteriet trenger å lade opp!
            if soc < effective_min_soc:
                logger.info(f"SOC {soc:.1f}% < {effective_min_soc}% — LADING NØDVENDIG ({mode_str.lower()}vindu)")

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
            # Sjekk storm mode status for MIN_SOC
<<<<<<< HEAD
=======
            from solar_forecast import get_solar_kwh_tomorrow
>>>>>>> 57224e1 (docs: Complete system analysis with DESS comparison)
            solar_kwh_tomorrow = get_solar_kwh_tomorrow(CONFIG.site_lat, CONFIG.site_lon, CONFIG.solar_max_kw, CONFIG.solar_system_efficiency)
            storm_mode = solar_kwh_tomorrow < CONFIG.storm_mode_threshold_kwh
            effective_min_soc = CONFIG.storm_mode_min_soc if storm_mode else CONFIG.min_soc
            
            if soc <= effective_min_soc:
                mode_str = "STORM" if storm_mode else "NORMAL"
                logger.info(f"SOC ved {mode_str.lower()} min ({effective_min_soc}%), hopper over utlading")
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
<<<<<<< HEAD
            self.victron.stop_ess_control()  # setpoint=0, Mode 3 beholdes
=======
            self.victron.stop_ess_control()  # Behold Mode 3 med setpoint=0
>>>>>>> 57224e1 (docs: Complete system analysis with DESS comparison)
            self.current_action = None
            self._original_charge_kw = 0.0
            logger.info("Idle — ESS styrer (Mode 3, setpoint=0)")

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
        logger.info("Stopper — gir kontroll tilbake til Victron ESS...")
        self.evcs.restore_auto()
        if hasattr(self.victron, '_connected') and self.victron._connected:
            self.victron.release_control()  # Hub4Mode → 2, setpoint → 0
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

