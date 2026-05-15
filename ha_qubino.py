"""
Qubino 3-fase smartmåler via Home Assistant REST API.

Qubino ZMNHXD sitter på inntaket og måler grid-import/-eksport for alle 3 faser.
Brukes som primærkilde for grid-effekt — VM-3P75CT (Modbus) er fallback.

Fordel vs VM-3P75CT: Måler L3 korrekt i 3-fase IT-nett (230V L-N).
VM-3P75CT viser L3=0W pga IT-nett-topologi.

Entity _w_6 = total grid alle 3 faser (import positiv, eksport via production_w_*).
Vi bruker consumption_w_6 som total grid-import (positiv = importerer fra nett).
"""
import os
import logging
import requests
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Leses ved import — kan overstyres av instans
_HA_URL_DEFAULT   = "https://homeassistant.abelgaard.no"
_HA_TOKEN_DEFAULT = ""

# Entity-IDs verifisert mot HA 2026-05-11 (Qubino ZMNHXD, Z-Wave)
# _w_6 = total alle 3 faser (consumption), verifisert = VM-3P75CT L1+L2 + manglende L3
HA_ENTITIES = {
    "power_total": "sensor.qubino_3_phase_smart_meter_electric_consumption_w_6",    # Total L1+L2+L3
    "power_l1":   "sensor.qubino_3_phase_smart_meter_electric_consumption_w_2_2",  # L1
    "power_l2":   "sensor.qubino_3_phase_smart_meter_electric_consumption_w_3_2",  # L2
    "power_l3":   "sensor.qubino_3_phase_smart_meter_electric_consumption_w_4_2",  # L3 ✅
    "voltage_l1": "sensor.qubino_3_phase_smart_meter_electric_consumption_v_2",    # ~143V
    "voltage_l2": "sensor.qubino_3_phase_smart_meter_electric_consumption_v_3_2",  # ~142V
    "voltage_l3": "sensor.qubino_3_phase_smart_meter_electric_consumption_v_4_2",  # ~141V
    "status":     "sensor.qubino_3_phase_smart_meter_node_status",                 # alive/dead
}

# Timeout for HA-kall — Z-Wave kan være treg
HA_TIMEOUT = float(os.getenv("HA_TIMEOUT", "3.0"))
# Minimum sekunder mellom kall mot HA (unngå rate-limit/ban)
# 30s matcher Qubino P42=30s (active power rapport-intervall)
HA_MIN_INTERVAL = float(os.getenv("HA_MIN_INTERVAL", "30.0"))


class QubinoReader:
    """Les Qubino 3-fase smartmåler fra Home Assistant.
    
    Bruker ET enkelt batch-kall til /api/states for alle entiteter
    for å unngå rate-limiting/IP-ban fra HA.
    """

    def __init__(self):
        self.ha_url   = os.getenv("HA_URL",   _HA_URL_DEFAULT)
        self.ha_token = os.getenv("HA_TOKEN", _HA_TOKEN_DEFAULT)
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        })
        self._cache: dict = {}          # entity_id → state-verdi
        self._last_fetch: float = 0.0   # tidspunkt for siste batch-henting
        if not self.ha_token:
            logger.warning("HA_TOKEN ikke satt — Qubino vil ikke fungere")

    def _fetch_all(self) -> bool:
        """
        Hent alle Qubino-entiteter i ETT HTTP-kall via /api/states.
        Filtrer lokalt på entity_id — unngår mange separate kall og IP-ban.
        Respekterer HA_MIN_INTERVAL for å unngå rate-limiting.
        """
        import time, json
        now = time.monotonic()
        if now - self._last_fetch < HA_MIN_INTERVAL:
            return True  # Bruk eksisterende cache

        wanted = set(HA_ENTITIES.values())
        try:
            r = self._session.get(
                f"{self.ha_url}/api/states",
                timeout=HA_TIMEOUT
            )
            if r.status_code == 200:
                all_states = r.json()
                self._cache = {
                    s["entity_id"]: s["state"]
                    for s in all_states
                    if s["entity_id"] in wanted
                }
                self._last_fetch = now
                logger.debug(f"Qubino batch-fetch OK: {len(self._cache)}/{len(wanted)} entiteter")
                return True
            else:
                logger.warning(f"HA /api/states {r.status_code}: {r.text[:80]}")
        except requests.Timeout:
            logger.warning("HA batch-fetch timeout")
        except Exception as e:
            logger.warning(f"HA batch-fetch feil: {e}")
        return False

    def _get_state(self, entity_id: str, as_str: bool = False):
        """Hent entity-verdi fra cache (populert av _fetch_all)."""
        if not entity_id:
            return None
        state = self._cache.get(entity_id)
        if state is None or state in ("unavailable", "unknown", ""):
            return None
        if as_str:
            return state
        try:
            return float(state)
        except (ValueError, TypeError):
            return None

    def get_grid_power(self) -> Optional[Dict]:
        """
        Hent grid-effekt fra alle 3 faser.

        Returnerer dict med l1/l2/l3/total, eller None hvis Qubino er nede.
        ETT HTTP-kall per HA_MIN_INTERVAL sekunder (standard 15s).
        """
        if not self._fetch_all():
            return None

        # Sjekk Z-Wave node-status (alive/dead) — ikke blokker på dead, bare logg
        if "status" in HA_ENTITIES:
            status = self._get_state(HA_ENTITIES["status"], as_str=True)
            if status == "dead":
                logger.warning("Qubino Z-Wave node: dead — data kan være utdatert")
            elif status is None:
                logger.warning("Qubino Z-Wave node: unavailable — forsøker likevel")

        total = self._get_state(HA_ENTITIES["power_total"])
        l1    = self._get_state(HA_ENTITIES["power_l1"])
        l2    = self._get_state(HA_ENTITIES["power_l2"])
        l3    = self._get_state(HA_ENTITIES["power_l3"])

        if total is None:
            logger.warning("Qubino: total utilgjengelig (Z-Wave nede?)")
            return None

        l1 = l1 or 0.0
        l2 = l2 or 0.0
        l3 = l3 or 0.0

        logger.debug(f"Qubino: total={total}W  L1={l1}W L2={l2}W L3={l3}W")
        return {"l1": l1, "l2": l2, "l3": l3, "total": total, "source": "qubino"}

    def get_voltages(self) -> Optional[Dict]:
        """Hent spenning på alle 3 faser (bruker eksisterende cache)."""
        self._fetch_all()  # Bruker cache hvis nylig hentet
        v1 = self._get_state(HA_ENTITIES["voltage_l1"])
        v2 = self._get_state(HA_ENTITIES["voltage_l2"])
        v3 = self._get_state(HA_ENTITIES["voltage_l3"])
        if v1 is None:
            return None
        return {"v1": v1, "v2": v2, "v3": v3}

    def is_available(self) -> bool:
        """Sjekk om Qubino er tilgjengelig (Z-Wave ikke nede)."""
        return self.get_grid_power() is not None


class EVCSController:
    """
    Styrer EVCS elbil-lader via Home Assistant for å koordinere med batteri-trading.

    Prioriteter:
    1. Aldri overskrid peak_limit_kw totalt (batteri + EVCS + annet forbruk)
    2. Under batteri-eksport (salg): stopp EVCS helt
    3. Om dagen med sol-overskudd: lad bil med overskuddsstrøm
    4. Om natten: del tilgjengelig kapasitet mellom batteri og EVCS

    EVCS sitter på AC-input (grid-siden) og teller mot kapasitetsleddet.
    """

    def __init__(self):
        from config import CONFIG
        self.ha_url   = os.getenv("HA_URL", "https://homeassistant.abelgaard.no")
        self.ha_token = os.getenv("HA_TOKEN", "")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        })
        self._prefix  = CONFIG.evcs_entity_prefix
        self._min_a   = CONFIG.evcs_min_current_a   # 6A
        self._max_a   = CONFIG.evcs_max_current_a   # 16A
        self._phases  = CONFIG.evcs_phases           # 1 (1-fase EVCS, default satt i config.py)
        self._peak_kw = CONFIG.peak_limit_kw         # 9.5 kW
        self._cache: dict = {}
        self._last_fetch = 0.0
        self._last_current_a: int = 0  # Siste satte strøm

    # ------------------------------------------------------------------ #
    # HA-kall                                                              #
    # ------------------------------------------------------------------ #

    def _fetch(self) -> bool:
        """Hent EVCS-states fra HA (maks hvert 10s)."""
        import time
        if time.monotonic() - self._last_fetch < 10:
            return True
        wanted = {
            f"binary_sensor.{self._prefix}_connected",
            f"sensor.{self._prefix}_power",
            f"sensor.{self._prefix}_current",
            f"sensor.{self._prefix}_status",
            f"select.{self._prefix}_mode",
            f"switch.{self._prefix}_ev_charging",
            f"number.{self._prefix}_charge_current_setpoint",
        }
        try:
            r = self._session.get(f"{self.ha_url}/api/states", timeout=3)
            if r.status_code == 200:
                self._cache = {s["entity_id"]: s["state"]
                               for s in r.json() if s["entity_id"] in wanted}
                self._last_fetch = __import__("time").monotonic()
                return True
        except Exception as e:
            logger.debug(f"EVCS fetch feil: {e}")
        return False

    def _call(self, domain: str, service: str, data: dict) -> bool:
        try:
            r = self._session.post(
                f"{self.ha_url}/api/services/{domain}/{service}",
                json=data, timeout=5)
            return r.status_code in (200, 201)
        except Exception as e:
            logger.warning(f"EVCS HA-kall feil {domain}/{service}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Status                                                               #
    # ------------------------------------------------------------------ #

    def is_connected(self) -> bool:
        self._fetch()
        return self._cache.get(f"binary_sensor.{self._prefix}_connected") == "on"

    def get_power_kw(self) -> float:
        self._fetch()
        try:
            return float(self._cache.get(f"sensor.{self._prefix}_power", 0)) / 1000
        except (ValueError, TypeError):
            return 0.0

    # ------------------------------------------------------------------ #
    # Styring                                                              #
    # ------------------------------------------------------------------ #

    def stop_charging(self, reason: str = "batteri selger") -> bool:
        """Stopp lading helt. Kaller HA kun ved statusendring."""
        if not self.is_connected():
            return True
        if self._last_current_a == 0:
            return True  # Allerede stoppet — ikke kall HA eller logg
        logger.info(f"EVCS: stopper elbillading ({reason})")
        ok = self._call("switch", "turn_off",
                        {"entity_id": f"switch.{self._prefix}_ev_charging"})
        self._last_current_a = 0
        return ok

    def set_charge_current(self, amps: int) -> bool:
        """Sett ladestrøm i Ampere (6-max_a). 0 = stopp."""
        if not self.is_connected():
            return True
        if amps <= 0:
            return self.stop_charging()
        amps = max(self._min_a, min(self._max_a, amps))
        if amps == self._last_current_a:
            return True  # Ingen endring
        logger.info(f"EVCS: setter ladestrøm {amps}A "
                    f"({amps * self._phases * 0.23:.1f} kW approx)")
        ok1 = self._call("number", "set_value", {
            "entity_id": f"number.{self._prefix}_charge_current_setpoint",
            "value": amps})
        ok2 = self._call("switch", "turn_on",
                         {"entity_id": f"switch.{self._prefix}_ev_charging"})
        if ok1 and ok2:
            self._last_current_a = amps
        return ok1 and ok2

    def restore_auto(self) -> bool:
        """Sett EVCS tilbake til auto-modus."""
        if not self.is_connected():
            return True
        logger.info("EVCS: tilbake til auto-modus")
        ok = self._call("select", "select_option", {
            "entity_id": f"select.{self._prefix}_mode",
            "option": "auto"})
        self._last_current_a = 0
        return ok

    # ------------------------------------------------------------------ #
    # Koordinering med batteri og peak-limit                              #
    # ------------------------------------------------------------------ #

    def adjust_for_trading(self, battery_action: str, grid_kw: float,
                           solar_kw: float, battery_kw: float) -> None:
        """
        Juster EVCS-ladestrøm basert på nåværende situasjon.

        battery_action: 'discharge', 'charge', 'idle'
        grid_kw:        Nåværende grid-import (positiv=import, negativ=eksport)
        solar_kw:       Sol-produksjon (kW)
        battery_kw:     Batterieffekt (positiv=lading, negativ=utlading)
        """
        if not self.is_connected():
            return

        # --- Scenario 1: Trader selger aktivt (Mode 3) → stopp EVCS helt ---
        # Merk: battery_kw < 0 i Mode 2 (sol-overskudd) skal IKKE stoppe EVCS —
        # det er naturlig ESS-eksport, ikke aktiv trader-discharge.
        if battery_action == 'discharge':
            self.stop_charging()
            return

        # --- Beregn tilgjengelig kapasitet for EVCS ---
        # grid_kw er total fra nett (inkl batteri-lading + EVCS + husforbruk).
        # Tilgjengelig = peak_limit - (grid uten EVCS)
        evcs_kw = self.get_power_kw()
        grid_without_evcs = grid_kw - evcs_kw  # Hva grid ville vært uten EVCS
        available_kw = self._peak_kw - grid_without_evcs

        # --- Scenario 2: Sol-overskudd om dagen → lad med overskudd ---
        hour = __import__("datetime").datetime.now(
            __import__("zoneinfo").ZoneInfo("Europe/Oslo")).hour
        is_day = 6 <= hour < 22
        if is_day and solar_kw > 0.5:
            # Overskudd = sol - grid uten EVCS (husforbruk + evt. batteri-lading)
            surplus_kw = solar_kw - grid_without_evcs
            charge_kw = max(0, min(surplus_kw, available_kw,
                                   self._max_a * self._phases * 0.23))
            amps = int(charge_kw * 1000 / (self._phases * 230))
            if amps >= self._min_a:
                self.set_charge_current(amps)
            else:
                self.stop_charging(reason="ikke nok sol")
            return

        # --- Scenario 3: Natt eller idle → gi EVCS tilgjengelig kapasitet ---
        charge_kw = max(0, min(available_kw, self._max_a * self._phases * 0.23))
        amps = int(charge_kw * 1000 / (self._phases * 230))
        if amps >= self._min_a:
            self.set_charge_current(amps)
        else:
            logger.debug(f"EVCS: ikke nok kapasitet ({available_kw:.1f}kW ledig)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    q = QubinoReader()
    print(f"HA URL: {q.ha_url}")
    print(f"Token satt: {'ja' if q.ha_token else 'NEI — sett HA_TOKEN'}")
    print()

    power = q.get_grid_power()
    if power:
        print(f"✅ Qubino tilkoblet!")
        print(f"  L1: {power['l1']:.0f} W")
        print(f"  L2: {power['l2']:.0f} W")
        print(f"  L3: {power['l3']:.0f} W")
        print(f"  Total: {power['total']:.0f} W")
        v = q.get_voltages()
        if v:
            print(f"  V1: {v['v1']} V  V2: {v['v2']} V  V3: {v['v3']} V")
    else:
        print("❌ Qubino ikke tilgjengelig")
        print("   Sjekk HA_URL, HA_TOKEN og entity-navn i ha_qubino.py")

