"""Main controller for Victron Energy Trader."""
import os
import sys
import time
import math
import signal
import logging
import logging.handlers
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

# Skriv også til fil — overlever container-restart (montert volum)
_log_dir = os.path.join(os.path.dirname(os.getenv("DB_PATH", "./data/profit.db")), "..", "logs")
_log_dir = os.path.normpath(_log_dir)
os.makedirs(_log_dir, exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(_log_dir, "trader.log"),
    maxBytes=5 * 1024 * 1024,  # 5 MB per fil
    backupCount=7,             # 7 filer = ~35 MB = ~1 uke
    encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
_file_handler.formatter.converter = lambda *args: datetime.now(OSLO_TZ).timetuple()
logging.getLogger().addHandler(_file_handler)

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
        self._action_start_soc: Optional[float] = None
        self._action_start_counters: Optional[tuple] = None
        self._last_price_nok: float = 0.0
        self._solar_cache_kwh: float = 0.0
        self._solar_cache_time: float = 0.0
        self._SOLAR_CACHE_TTL: float = 3600.0  # 1 time
        self._charge_target_soc: float = CONFIG.max_soc  # Oppdateres av optimizer
        self._self_consume_active: bool = False  # Sporer self-consume modus
        self._self_consume_stop_time: float = 0.0  # Cooldown etter stopp (unngår oscillering)
        self._grid_history: list = []              # Rullende snitt grid-avlesninger (W)
        self._cached_grid_w: float = 0.0           # Grid-avlesning cachet per 10s-syklus
        self._cached_solar_w: float = 0.0          # Sol-avlesning cachet per 10s-syklus
        self._cached_bat_w: float = 0.0            # Batteri-avlesning cachet per 10s-syklus
        self._effective_discharge_kw: float = 0.0  # Export-guard: faktisk utladeeffekt (ikke muterer power_kw)

    def _save_state(self):
        """Lagre current_action til disk så den overlever restart."""
        try:
            if self.current_action and self.current_action.action != 'idle':
                state = {
                    "action": self.current_action.action,
                    "power_kw": self.current_action.power_kw,
                    "timestamp": self.current_action.timestamp.isoformat(),
                    "reason": self.current_action.reason,
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
        self.victron.set_max_charge_current(-1)  # Frigjør DVCC ved oppstart (kan være 0A fra forrige kjøring)
        logger.info(f"ESS min SOC: {CONFIG.min_soc:.0f}%  max SOC: {CONFIG.max_soc:.0f}%  DVCC: frigjort")

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
                        logger.info("Modbus reconnect OK — gjenoppretter Mode 3")
                        self.victron.set_min_soc(CONFIG.min_soc)
                        self.victron.enable_external_control()  # Gjenopprett Mode 3 etter Passthru
                        last_keepalive = 0.0  # Tving umiddelbar keepalive
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

            if now.minute % 5 == 0 and now.minute != last_status_min:
                try:
                    count = len(self.price_fetcher.get_prices(CONFIG.forecast_hours))
                    if count > self._last_price_count:
                        self._last_price_count = count
                except Exception:
                    pass

            if current_time - last_peak_shave >= 10:
                # Les alle sanntidsverdier ÉN gang per syklus — deles av alle funksjoner
                try:
                    self._cached_grid_w  = self._get_grid_power() or 0
                    self._cached_solar_w = self.victron.get_solar_power() or 0
                    self._cached_bat_w   = self.victron.get_battery_power() or 0
                except Exception:
                    pass
                # Hierarkisk setpoint-kontroll (prioritet 1-6)
                self._check_peak_shaving()
                self._control_setpoint()
                try:
                    act = self.current_action.action if self.current_action else 'idle'
                    self.evcs.adjust_for_trading(
                        battery_action=act,
                        grid_kw=self._cached_grid_w / 1000,
                        solar_kw=self._cached_solar_w / 1000,
                        battery_kw=self._cached_bat_w / 1000)
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
                    if current_time - self._action_start_time < 15:
                        time.sleep(3)
                        continue
                    battery_w = self.victron.get_battery_power() or 0
                    discharge_w = abs(self.current_action.power_kw) * 1000
                    # Export-guard: batteri lader i stedet for å utlade
                    # Bruker _effective_discharge_kw — muterer IKKE current_action.power_kw
                    # for å unngå kumulativ degradering i _adjust_active_setpoint
                    if battery_w > -(discharge_w * 0.3):  # Batteri leverer < 30% av forventet
                        solar_w = self._cached_solar_w or self.victron.get_solar_power() or 0
                        net_kw = round((discharge_w - solar_w) / 1000, 1)
                        if net_kw >= 0.5 and solar_w > 0:
                            # Sol motvirker delvis — senk setpoint til netto behov
                            logger.warning(
                                f"Export-guard: Sol {solar_w:.0f}W motvirker discharge — "
                                f"senker setpoint {self.current_action.power_kw:.1f}→-{net_kw:.1f}kW"
                            )
                            self.victron.set_discharge_power(net_kw)
                            self._effective_discharge_kw = net_kw  # Separat — ikke mutér power_kw
                        else:
                            logger.warning(
                                f"Export-guard: Batteri {battery_w:.0f}W netto positiv (sol {solar_w:.0f}W) — stopper"
                            )
                            self.victron.stop_ess_control()
                            self.current_action = None
                            self._action_start_soc = None
                            self._effective_discharge_kw = 0.0

            elif self.current_action and self.current_action.action != 'idle' and action_hour != now.hour:
                self._log_completed_action(self.current_action)
                logger.info(f"Action fra time {action_hour:02d} utgatt (nå {now.hour:02d}) — stopper ESS")
                self.victron.stop_ess_control()
                self.current_action = None
                self._action_start_soc = None
                self._action_start_counters = None
                self._original_charge_kw = 0.0

            if now.minute % 5 == 0 and now.minute != last_status_min:
                last_status_min = now.minute
                self._log_status()
                self._adjust_active_setpoint()

            time.sleep(1)  # Maks 1s delay — keepalive worst-case 8+1=9s (Victron timeout ~10s)

    def _log_completed_action(self, completed_action):
        """Logg en fullført action til profit_tracker. Brukes når time skifter eller action stopper."""
        end_soc = self.victron.get_soc() or 0
        act = completed_action.action
        if act == 'idle':
            return  # Ingenting å logge for idle
        actual_kwh = 0.0
        kwh_source = "soc-delta"
        # Foretrekk SmartShunt energitellere (reg 309/310) over SOC-delta
        # compute_counter_delta() håndterer uint16-overflow (wrapper ved 6553.5 kWh)
        end_counters = self.victron.get_energy_counters()
        if end_counters and self._action_start_counters:
            start_dis, start_chg = self._action_start_counters
            end_dis, end_chg = end_counters
            if act == 'discharge':
                actual_kwh = self.victron.compute_counter_delta(start_dis, end_dis)
            else:
                actual_kwh = self.victron.compute_counter_delta(start_chg, end_chg)
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
            action_hour = completed_action.timestamp.astimezone(OSLO_TZ).hour
            price_nok = (sell_price_ore(spot_eks_mva * 100) if db_action == "sell"
                         else buy_price_ore(spot_eks_mva * 100, action_hour)) / 100
            self.tracker.log_trade(db_action, actual_kwh, price_nok)
            logger.info(f"Handling ferdig: {act} {actual_kwh:.2f} kWh [{kwh_source}] (SOC {self._action_start_soc:.1f}%→{end_soc:.1f}%)")

    def _control_setpoint(self):
        """Hierarkisk setpoint-kontroll — kjøres hvert 10s.

        Prioritetsrekkefølge (høyest vinner):
          1. MIN_SOC nødstopp        — stopp all utlading
          2. Peak-shaving            — utlad for å hindre effekttopp
          3. Fullt batteri (≥90%)    — eksporter all sol til nett
          4. Arbitrasje charge       — lad på billig pris
          5. Arbitrasje discharge    — selg på dyr pris
          6. Self-consume            — batteri dekker husforbruk (setpoint=0W)
          7. Idle / natt             — ingenting

        DVCC-styring (ladestrøm-grense) håndteres separat og overstyrer ikke setpoint.
        """
        soc = self.victron.get_soc()
        if soc is None:
            return

        solar_w = self._cached_solar_w
        grid_w  = self._cached_grid_w
        target  = self._charge_target_soc
        now_hour = datetime.now(OSLO_TZ).hour
        is_daytime = 6 <= now_hour < 22

        bat_w   = self._cached_bat_w
        load_w  = solar_w + grid_w - bat_w  # faktisk forbruk (W)

        logger.debug(
            f"[ctrl] SOC={soc:.1f}% sol={solar_w:.0f}W grid={grid_w:+.0f}W "
            f"bat={bat_w:+.0f}W forbruk={load_w:.0f}W target={target:.1f}% "
            f"action={self.current_action.action if self.current_action else 'none'}"
        )

        # --- DVCC: stopp lading når SOC >= lademål ---
        # set_max_charge_current(0) stopper KUN lading (strøm inn i batteri).
        # Utlading påvirkes ikke — den styres av grid setpoint (reg 37).
        if soc >= target and not self._dvcc_charging_stopped:
            logger.info(f"SOC {soc:.1f}% >= lademål {target:.1f}% — DVCC=0A (ingen lading)")
            self.victron.set_max_charge_current(0)
            self._dvcc_charging_stopped = True
        elif soc < target - 1.0 and self._dvcc_charging_stopped:
            logger.info(f"SOC {soc:.1f}% < {target - 1.0:.1f}% — DVCC frigjort, lading tillatt igjen")
            self.victron.set_max_charge_current(-1)
            self._dvcc_charging_stopped = False

        # --- 1. MIN_SOC nødstopp ---
        _, effective_min_soc = self._get_storm_info()
        if soc <= effective_min_soc:
            if self.current_action and self.current_action.action == 'discharge':
                logger.warning(f"[P1] MIN_SOC nødstopp: SOC {soc:.1f}% <= {effective_min_soc}% — stopper utlading")
                self.victron.stop_ess_control()
                self.current_action = None
            else:
                logger.debug(f"[P1] SOC {soc:.1f}% <= min {effective_min_soc}% — idle, ingen self-consume")
            return

        # --- 2. Peak-shaving (håndteres av _check_peak_shaving, ikke her) ---
        if self.current_action and self.current_action.action == 'peak_shave':
            logger.debug(f"[P2] Peak-shave aktiv ({self.current_action.power_kw:.1f}kW) — ingen override")
            return

        # --- 3. Fullt batteri: eksporter all sol til nett ---
        if soc >= CONFIG.max_soc and solar_w > 200:
            if self.current_action and self.current_action.action in ('charge', 'discharge'):
                logger.debug(f"[P3] Fullt batteri men arbitrasje ({self.current_action.action}) har prioritet")
                return
            new_setpoint = max(-int(solar_w), -int(CONFIG.battery_max_discharge_kw * 1000))
            logger.info(
                f"[P3] Fullt batteri ({soc:.1f}%): sol {solar_w:.0f}W forbruk {load_w:.0f}W "
                f"grid {grid_w:+.0f}W bat {bat_w:+.0f}W → setpoint {new_setpoint}W"
            )
            self.victron.set_grid_setpoint(new_setpoint)
            self._self_consume_active = False
            return

        # --- 4+5. Arbitrasje (charge/discharge fra trade-cycle) ---
        if self.current_action and self.current_action.action in ('charge', 'discharge'):
            logger.debug(
                f"[P4/5] Arbitrasje {self.current_action.action} {self.current_action.power_kw:.1f}kW aktiv "
                f"| {self.current_action.reason}"
            )
            self._self_consume_active = False
            return

        # --- 6. Self-consume / natt-tøm ---
        # Natt-tøm: aktiv utlading basert på tid til soloppgang når SOC > lademål + 5%.
        # Tidlig natt → lav effekt.  Nær soloppgang → opp mot 10kW.
        night_drain = not is_daytime and soc > target + 5.0

        if not is_daytime and not night_drain:
            if self._self_consume_active:
                logger.info("[P6] Self-consume: natt — stopper, beholder batteri til sol")
                self._self_consume_active = False
                self._grid_history.clear()
                self.victron.stop_ess_control()
            else:
                logger.debug(f"[P6] Natt ({now_hour}h) — idle")
            return

        if soc <= target + 1.0:
            if self._self_consume_active:
                tag = "natt-tøm ferdig" if night_drain else "SOC ved lademål"
                logger.info(f"[P6] {tag}: SOC {soc:.1f}% ≤ lademål {target:.1f}% + 1% — stopper")
                self._self_consume_active = False
                self._self_consume_stop_time = time.time()
                self._grid_history.clear()
                self.victron.stop_ess_control()
            else:
                logger.debug(f"[P6] SOC {soc:.1f}% <= lademål {target:.1f}% + 1% — idle")
            return

        self._grid_history.append(grid_w)
        if len(self._grid_history) > 3:
            self._grid_history.pop(0)
        avg_grid_kw = (sum(self._grid_history) / len(self._grid_history)) / 1000.0

        if self._self_consume_active:
            if night_drain:
                hours_left = self._hours_to_sunrise()
                setpoint_w = self._calc_night_drain_setpoint(soc, target, hours_left)
                self.victron.set_grid_setpoint(setpoint_w)
                logger.debug(
                    f"[P6] Natt-tøm: SOC {soc:.1f}%→{target:.1f}%, "
                    f"setpoint {setpoint_w/1000:.2f}kW, {hours_left:.1f}t til sol"
                )
            elif avg_grid_kw < 0.10:
                logger.info(f"[P6] Self-consume: snitt-grid {avg_grid_kw:.2f}kW < 0.10kW — sol dekker, stopper")
                self._self_consume_active = False
                self._self_consume_stop_time = time.time()
                self._grid_history.clear()
                self.victron.stop_ess_control()
            else:
                logger.debug(
                    f"[P6] Self-consume aktiv: grid {avg_grid_kw:.2f}kW sol {solar_w:.0f}W "
                    f"bat {bat_w:+.0f}W SOC {soc:.1f}%"
                )
            return

        _SELF_CONSUME_COOLDOWN_S = 300  # 5 min — unngå oscillering ved variabelt sol
        cooldown_ok = night_drain or (time.time() - self._self_consume_stop_time >= _SELF_CONSUME_COOLDOWN_S)

        if (avg_grid_kw >= 0.15 or night_drain) and cooldown_ok:
            if night_drain:
                hours_left = self._hours_to_sunrise()
                setpoint_w = self._calc_night_drain_setpoint(soc, target, hours_left)
                mode_tag = (f"natt-tøm SOC {soc:.1f}%→{target:.1f}%, "
                            f"{hours_left:.1f}t til sol, setpoint {setpoint_w/1000:.2f}kW")
            else:
                setpoint_w = 0
                mode_tag = f"SOC {soc:.1f}% > mål {target:.1f}%"
            logger.info(f"[P6] START: {mode_tag}, grid {avg_grid_kw:.2f}kW sol {solar_w:.0f}W")
            self._self_consume_active = True
            self.victron.set_grid_setpoint(setpoint_w)
        else:
            logger.debug(f"[P6] Grid {avg_grid_kw:.2f}kW < 0.15kW — sol dekker forbruket, idle")

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

            action, charge_target_soc = self.optimizer.get_immediate_action(current, prices, soc, solar_kw)
            self._charge_target_soc = charge_target_soc  # Cache for _enforce_max_soc
            logger.info(f"Action: {action.action} @ {action.power_kw:.1f}kW | {action.reason}")
            logger.info(f"Lademål: {charge_target_soc:.1f}% SOC")

            # Logg plan til DB for etteranalyse (verifiser sol-prognose vs virkelighet)
            try:
                solar_kwh_fc = self._get_solar_kwh_cached()
                storm_now, _ = self._get_storm_info()
                solar_reserve = CONFIG.max_soc - charge_target_soc if not storm_now else 0.0
                self.tracker.log_plan(
                    solar_kwh_forecast=solar_kwh_fc,
                    solar_reserve_pct=round(solar_reserve, 1),
                    charge_target_soc=round(charge_target_soc, 1),
                    storm_mode=storm_now,
                    soc=soc,
                    spot_nok_kwh=current.price_nok_kwh
                )
            except Exception:
                pass

            prev_action = self.current_action
            # Hent fersk SOC rett før execute for å unngå utdatert data
            fresh_soc = self.victron.get_soc()
            if fresh_soc is None:
                fresh_soc = soc
            # Sjekk om time har endret seg mens forrige action var aktiv — logg den først
            if prev_action and prev_action.action != 'idle':
                prev_hour = prev_action.timestamp.astimezone(OSLO_TZ).hour
                curr_hour = datetime.now(OSLO_TZ).hour
                if prev_hour != curr_hour:
                    # Time skiftet — logg forrige action før vi starter ny
                    self._log_completed_action(prev_action)
                    prev_action = None  # Nullstill så ny action starter fresh
            # Les energitellere og SOC ved start av ny aktiv action (ikke ved idle)
            is_new_active = action.action != 'idle' and (prev_action is None or prev_action.action == 'idle')
            if is_new_active:
                self._action_start_soc = fresh_soc
                self._action_start_counters = self.victron.get_energy_counters()
                self._action_start_time = time.time()
            storm_mode, effective_min_soc = self._get_storm_info()
            self._execute_action(action, fresh_soc, current.price_nok_kwh, storm_mode, effective_min_soc)
            self.current_action = action
            self._save_state()

            stats = self.tracker.get_stats()
            logger.info(f"Dagens profitt: {stats['today_profit_nok']:.2f} kr")

        except Exception:
            logger.exception("Trade cycle feilet")

    def _get_storm_info(self) -> tuple:
        """Returner (storm_mode, effective_min_soc) basert på sol-prognose."""
        solar_kwh = self._get_solar_kwh_cached()
        storm = solar_kwh is not None and solar_kwh < CONFIG.storm_mode_threshold_kwh
        return storm, (CONFIG.storm_mode_min_soc if storm else CONFIG.min_soc)

    def _get_solar_kwh_cached(self) -> Optional[float]:
        """Hent sol-prognose for i morgen — cachet i 1 time for å unngå gjentatte API-kall."""
        now = time.time()
        if now - self._solar_cache_time < self._SOLAR_CACHE_TTL:
            return self._solar_cache_kwh
        val = get_solar_kwh_tomorrow(CONFIG.site_lat, CONFIG.site_lon,
                                     CONFIG.solar_max_kw, CONFIG.solar_system_efficiency)
        if val is not None:
            self._solar_cache_kwh = val
            self._solar_cache_time = now
        return val

    def _get_grid_power(self) -> Optional[float]:
        qpower = self.qubino.get_grid_power()
        if qpower:
            return qpower["total"]
        logger.debug("Qubino utilgjengelig — fallback VM-3P75CT")
        return self.victron.get_grid_power()

    def _hours_to_sunrise(self) -> float:
        """Timer til neste soloppgang basert på astronomisk beregning (±5min nøyaktighet)."""
        now = datetime.now(OSLO_TZ)
        lat_rad = math.radians(CONFIG.site_lat)
        for offset_days in range(3):
            d = (now + timedelta(days=offset_days)).date()
            n = d.timetuple().tm_yday
            decl = math.radians(-23.45 * math.cos(math.radians(360 / 365 * (n + 10))))
            cos_h = max(-1.0, min(1.0, -math.tan(lat_rad) * math.tan(decl)))
            H_deg = math.degrees(math.acos(cos_h))
            sunrise_utc_h = 12.0 - H_deg / 15.0 - CONFIG.site_lon / 15.0
            oslo_offset = 2 if 4 <= d.month <= 10 else 1
            midnight = datetime(d.year, d.month, d.day, tzinfo=OSLO_TZ)
            sunrise_dt = midnight + timedelta(hours=sunrise_utc_h + oslo_offset)
            delta_h = (sunrise_dt - now).total_seconds() / 3600.0
            if delta_h > 0.05:
                return delta_h
        return 0.5  # Fallback: 30 min

    def _calc_night_drain_setpoint(self, soc: float, target: float, hours_left: float) -> int:
        """Beregn aktivt utlade-setpoint (W) for å nå lademål akkurat ved soloppgang.

        Negativ = eksporter til nett (utlader batteriet aktivt utover husforbruk).
        Tidlig på natt → lite eksport (lang tid igjen).
        Nær soloppgang → opp mot 10kW (kort tid, mange kWh igjen).
        Justeres hvert 10s i _control_setpoint().
        """
        kwh_to_drain = CONFIG.battery_capacity_kwh * max(0.0, soc - target) / 100.0
        if kwh_to_drain <= 0 or hours_left <= 0:
            return 0
        needed_kw = min(kwh_to_drain / max(0.1, hours_left), CONFIG.battery_max_discharge_kw)
        # Husforbruk = hva huset allerede trekker fra batteri + grid + sol
        bat_discharge_kw = max(0.0, -self._cached_bat_w / 1000.0)
        house_kw = bat_discharge_kw + max(0.0, self._cached_grid_w / 1000.0) + self._cached_solar_w / 1000.0
        # Eksport = ønsket utlading minus det huset allerede absorberer
        export_kw = max(0.0, needed_kw - house_kw)
        logger.debug(
            f"Natt-tøm setpoint: {kwh_to_drain:.2f}kWh gjenstår, {hours_left:.1f}t til sol → "
            f"behov {needed_kw:.2f}kW, hus {house_kw:.2f}kW, eksport {export_kw:.2f}kW"
        )
        return -int(export_kw * 1000)

    def _check_peak_shaving(self):
        try:
            grid_w = self._cached_grid_w or self._get_grid_power()
            soc = self.victron.get_soc()
            if grid_w is None or soc is None:
                return

            grid_kw = grid_w / 1000.0
            peak_kw = self.optimizer.peak_limit_kw

            # KRITISK: Kontinuerlig MIN_SOC beskyttelse med storm mode
            # Sjekk storm mode status (samme logikk som optimizer)
            solar_kwh_tomorrow = self._get_solar_kwh_cached()
            storm_mode = solar_kwh_tomorrow is not None and solar_kwh_tomorrow < CONFIG.storm_mode_threshold_kwh
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
                # Grid OK — nullstill peak_shave action så _control_setpoint kan ta over igjen
                if self.current_action and self.current_action.action == 'peak_shave':
                    logger.info(f"Peak-shave ferdig: grid {grid_kw:.1f}kW ≤ {peak_kw + 0.3:.1f}kW — stopper")
                    self.victron.stop_ess_control()
                    self.current_action = None
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


    def _execute_action(self, action: Action, soc: float, price: float,
                        storm_mode: bool = False, effective_min_soc: float = None):
        if effective_min_soc is None:
            effective_min_soc = CONFIG.min_soc
        mode_str = "STORM" if storm_mode else "NORMAL"

        if action.action == 'charge':
            logger.info(f"CHARGE CHECK: SOC {soc:.1f}% vs {mode_str} MIN_SOC {effective_min_soc:.1f}% vs MAX_SOC {CONFIG.max_soc:.1f}%")
            if soc >= CONFIG.max_soc:
                logger.info("SOC ved maks, hopper over lading")
                self.victron.stop_ess_control()
                return
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
                self._original_charge_kw = charge_kw
                logger.info(f"Lader {charge_kw:.1f}kW")
                self.current_action = action

        elif action.action == 'discharge':
            if soc <= effective_min_soc:
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
            self.victron.stop_ess_control()  # Behold Mode 3 med setpoint=0
            self.current_action = None
            self._original_charge_kw = 0.0
            self._self_consume_active = False  # La _control_setpoint ta over
            logger.info("Idle — ESS styrer (Mode 3, setpoint=0)")

    def _log_status(self):
        soc   = self.victron.get_soc()
        # Bruk cachet verdi hvis den er populert (satt i main-loop hvert 10s).
        # Fallback til live avlesning. Merk: != 0 er feil guard siden 0W er gyldig —
        # vi bruker _last_peak_shave > 0 som proxy for om cachen er initialisert.
        cache_ready = self._cached_grid_w != 0 or self._cached_solar_w != 0
        grid  = self._cached_grid_w  if cache_ready else (self._get_grid_power() or 0)
        solar = self._cached_solar_w if cache_ready else (self.victron.get_solar_power() or 0)
        bat   = self._cached_bat_w   if cache_ready else (self.victron.get_battery_power() or 0)

        # Beregn forbruk: Sol + Grid (inn) - Bat (lading) = Forbruk
        # bat positiv=lading, negativ=utlading
        forbruk = solar + grid - bat  # W

        # Modus-streng
        if self.current_action and self.current_action.action != 'idle':
            modus = f"{self.current_action.action}({self.current_action.power_kw:.1f}kW)"
        elif self._self_consume_active:
            sp = getattr(self.victron, '_last_setpoint', 0)
            modus = f"natt-tøm({sp/1000:.1f}kW)" if sp < -50 else "self-consume(0W)"
        elif self._dvcc_charging_stopped:
            modus = "DVCC-stopp(sol-eksport)"
        else:
            modus = "idle"

        target = self._charge_target_soc
        logger.info(
            f"Status: SOC={soc:.1f}%/{target:.0f}% "
            f"Grid={grid/1000:.2f}kW Sol={solar/1000:.2f}kW "
            f"Bat={bat/1000:+.2f}kW Forbruk={forbruk/1000:.2f}kW "
            f"Modus={modus}"
        )

    def _adjust_active_setpoint(self):
        """Justerer discharge/charge-effekt midt i timen basert på gjenværende SOC og tid.

        Mål: strekke batteriet nøyaktig til slutten av timen — hverken for mye eller for lite.
        Kjøres hvert 5. minutt ved aktiv charge/discharge-handling.
        """
        if not self.current_action or self.current_action.action not in ('charge', 'discharge'):
            return

        soc = self.victron.get_soc()
        if soc is None:
            return

        now = datetime.now(OSLO_TZ)
        remaining_min = 60 - now.minute
        if remaining_min <= 2:
            return  # Trade cycle kjøres om <2 min — ikke juster
        remaining_hours = remaining_min / 60.0

        _, effective_min_soc = self._get_storm_info()
        opt = self.optimizer

        if self.current_action.action == 'discharge':
            # Sol-reserve discharge: fast 2kW, ikke eskalér
            is_solar_reserve = 'Sol-reserve' in (self.current_action.reason or '')
            if is_solar_reserve:
                return
            floor_soc = effective_min_soc
            avail_kwh = max(0.0, opt.capacity * (soc - floor_soc) / 100 - opt.peak_reserve)
            if avail_kwh <= 0:
                logger.info(
                    f"Setpoint-justering: SOC {soc:.1f}% ved mål ({floor_soc:.0f}%) — stopper utlading"
                )
                self.victron.stop_ess_control()
                self.current_action = None
                return
            optimal_kw = round(min(opt.max_discharge, avail_kwh / remaining_hours), 1)
            current_kw = abs(self.current_action.power_kw)
            if abs(optimal_kw - current_kw) >= 0.5:
                logger.info(
                    f"Setpoint-justering: utlading {current_kw:.1f}→{optimal_kw:.1f}kW "
                    f"(SOC {soc:.1f}%, {remaining_min}min igjen, {avail_kwh:.1f}kWh tilgj.)"
                )
                self.victron.set_discharge_power(optimal_kw)
                self.current_action.power_kw = -optimal_kw

        elif self.current_action.action == 'charge':
            space_kwh = max(0.0, opt.capacity * (CONFIG.max_soc - soc) / 100)
            if space_kwh <= 0:
                logger.info(
                    f"Setpoint-justering: SOC {soc:.1f}% ved maks ({CONFIG.max_soc:.0f}%) — stopper lading"
                )
                self.victron.stop_ess_control()
                self.current_action = None
                return
            optimal_kw = round(min(opt.max_charge, space_kwh / remaining_hours), 1)
            # Respekter peak-limit basert på annen last (ekskl. vår egen lading)
            grid_w = self._get_grid_power() or 0
            other_load_kw = max(0.0, grid_w / 1000.0 - (self._original_charge_kw or self.current_action.power_kw))
            headroom_kw = max(0.0, opt.peak_limit_kw - other_load_kw)
            optimal_kw = min(optimal_kw, headroom_kw)
            current_kw = self.current_action.power_kw
            if optimal_kw < 0.5:
                logger.info(f"Setpoint-justering: lading blokkert av peak-limit (headroom {headroom_kw:.1f}kW)")
                return
            if abs(optimal_kw - current_kw) >= 0.5:
                logger.info(
                    f"Setpoint-justering: lading {current_kw:.1f}→{optimal_kw:.1f}kW "
                    f"(SOC {soc:.1f}%, {remaining_min}min igjen, {space_kwh:.1f}kWh plass)"
                )
                self.victron.set_charge_power(optimal_kw)
                self.current_action.power_kw = optimal_kw

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


