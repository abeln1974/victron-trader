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
        self._last_warn_time: float = 0.0  # Throttle warnings til maks 1/min
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

        # Oppdater alltid _last_fetch — også ved feil — for å unngå spam
        self._last_fetch = now
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
                logger.debug(f"Qubino batch-fetch OK: {len(self._cache)}/{len(wanted)} entiteter")
                return True
            else:
                if now - self._last_warn_time >= 60:
                    logger.warning(f"HA /api/states {r.status_code} — Qubino utilgjengelig")
                    self._last_warn_time = now
        except requests.Timeout:
            if now - self._last_warn_time >= 60:
                logger.warning("HA batch-fetch timeout — Qubino utilgjengelig")
                self._last_warn_time = now
        except Exception as e:
            if now - self._last_warn_time >= 60:
                logger.warning(f"HA batch-fetch feil: {e}")
                self._last_warn_time = now
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
                logger.debug("Qubino Z-Wave node: dead — data kan være utdatert")
            elif status is None:
                logger.debug("Qubino Z-Wave node: unavailable — forsøker likevel")

        total = self._get_state(HA_ENTITIES["power_total"])
        l1    = self._get_state(HA_ENTITIES["power_l1"])
        l2    = self._get_state(HA_ENTITIES["power_l2"])
        l3    = self._get_state(HA_ENTITIES["power_l3"])

        if total is None:
            logger.debug("Qubino: total utilgjengelig (Z-Wave nede?) — fallback til Modbus")
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
    Styrer Victron EV Charging Station via direkte Modbus TCP (192.168.1.45:502, unit=1).

    Ingen Home Assistant nødvendig — kommuniserer direkte med EVCS via Victron Modbus.

    Registre (unit=1, fra victronenergy/dbus-modbus-client/ev_charger.py):
      5009 = Mode (0=manual, 1=auto, 2=scheduled)  — writeable
      5010 = StartStop (0=stop, 1=start)            — writeable
      5014 = Total AC Power (W)
      5015 = Status (0=disconnected, 1=connected, 2=charging, 3=charged, 4=wait_sun, 7=low_soc)
      5016 = SetCurrent (A)                         — writeable
      5018 = Actual current (/10 → A)

    Prioriteter:
      1. Batteri selger aktivt → stopp EVCS
      2. Batteri lader aktivt → EVCS kun sol-eksport-overskudd
      3. Idle/sol → EVCS får alt overskudd som ellers eksporteres
    """

    # EVCS status-koder
    STATUS_DISCONNECTED = 0
    STATUS_CONNECTED    = 1
    STATUS_CHARGING     = 2
    STATUS_CHARGED      = 3
    STATUS_WAIT_SUN     = 4
    STATUS_LOW_SOC      = 7

    # EVCS Modbus-registre
    REG_MODE        = 5009
    REG_STARTSTOP   = 5010
    REG_POWER_TOTAL = 5014
    REG_STATUS      = 5015
    REG_SET_CURRENT = 5016
    REG_MAX_CURRENT = 5017
    REG_CURRENT     = 5018  # /10 = A

    def __init__(self):
        from config import CONFIG
        from pymodbus.client import ModbusTcpClient
        self._host    = CONFIG.evcs_host
        self._port    = CONFIG.evcs_modbus_port
        self._unit    = CONFIG.evcs_unit_id
        self._min_a   = CONFIG.evcs_min_current_a
        self._max_a   = CONFIG.evcs_max_current_a
        self._phases  = CONFIG.evcs_phases
        self._peak_kw = CONFIG.peak_limit_kw
        self._client  = ModbusTcpClient(self._host, port=self._port, timeout=3)
        self._connected_modbus = False
        self._last_current_a: int = -1
        self._last_fetch: float = 0.0
        self._last_warn_time: float = 0.0
        self._cache: dict = {}

    def _ensure_connected(self) -> bool:
        """Koble til EVCS Modbus hvis ikke allerede tilkoblet."""
        if self._connected_modbus and self._client.connected:
            return True
        try:
            self._connected_modbus = self._client.connect()
            if self._connected_modbus and self._last_current_a == -1:
                r = self._client.read_holding_registers(
                    address=self.REG_SET_CURRENT, count=1, device_id=self._unit)
                if r and not r.isError():
                    self._last_current_a = r.registers[0]
                    logger.info(f"EVCS oppstart-sync: SetCurrent={self._last_current_a}A")
                else:
                    self._last_current_a = 0
        except Exception as e:
            logger.debug(f"EVCS Modbus tilkobling feilet: {e}")
            self._connected_modbus = False
        return self._connected_modbus

    def _read(self, register: int) -> Optional[int]:
        """Les ett register fra EVCS."""
        import time as _t
        if not self._ensure_connected():
            return None
        try:
            r = self._client.read_holding_registers(
                address=register, count=1, device_id=self._unit)
            if r and not r.isError():
                return r.registers[0]
        except Exception as e:
            now = _t.monotonic()
            if now - self._last_warn_time >= 60:
                logger.warning(f"EVCS Modbus read reg {register} feil: {e}")
                self._last_warn_time = now
            self._connected_modbus = False
        return None

    def _write(self, register: int, value: int) -> bool:
        """Skriv ett register til EVCS."""
        import time as _t
        if not self._ensure_connected():
            return False
        try:
            r = self._client.write_register(
                address=register, value=value, device_id=self._unit)
            if r and not r.isError():
                return True
        except Exception as e:
            now = _t.monotonic()
            if now - self._last_warn_time >= 60:
                logger.warning(f"EVCS Modbus write reg {register}={value} feil: {e}")
                self._last_warn_time = now
            self._connected_modbus = False
        return False

    # ------------------------------------------------------------------ #
    # Status                                                               #
    # ------------------------------------------------------------------ #

    def is_connected(self) -> bool:
        """Sjekk om elbil er koblet til laderen."""
        status = self._read(self.REG_STATUS)
        if status is None:
            return False
        return status != self.STATUS_DISCONNECTED

    def get_power_kw(self) -> float:
        """Nåværende ladeeffekt i kW."""
        val = self._read(self.REG_POWER_TOTAL)
        if val is None:
            return 0.0
        return float(val) / 1000.0

    def get_status(self) -> Optional[int]:
        """Les EVCS status-kode."""
        return self._read(self.REG_STATUS)

    # ------------------------------------------------------------------ #
    # Styring                                                              #
    # ------------------------------------------------------------------ #

    def stop_charging(self, reason: str = "batteri selger") -> bool:
        """Stopp lading. Skriver kun ved statusendring."""
        if not self.is_connected():
            return True
        if self._last_current_a == 0:
            return True
        logger.info(f"EVCS: stopper elbillading ({reason})")
        ok = self._write(self.REG_STARTSTOP, 0)
        if ok:
            self._last_current_a = 0
        return ok

    def set_charge_current(self, amps: int) -> bool:
        """Sett ladestrøm i Ampere (min_a–max_a). 0 = stopp.

        Setter Mode=manual slik at EVCS ikke overstyrer med auto/wait_sun.
        """
        if not self.is_connected():
            return True
        if amps <= 0:
            return self.stop_charging()
        amps = max(self._min_a, min(self._max_a, amps))
        if amps == self._last_current_a:
            return True
        logger.info(f"EVCS: setter ladestrøm {amps}A "
                    f"({amps * self._phases * 0.23:.1f} kW approx)")
        self._write(self.REG_MODE, 0)        # Mode = manual
        ok1 = self._write(self.REG_SET_CURRENT, amps)
        ok2 = self._write(self.REG_STARTSTOP, 1)
        if ok1 and ok2:
            self._last_current_a = amps
        return ok1 and ok2

    def restore_auto(self) -> bool:
        """Sett EVCS tilbake til auto-modus."""
        if not self.is_connected():
            return True
        logger.info("EVCS: tilbake til auto-modus")
        ok = self._write(self.REG_MODE, 1)
        if ok:
            self._last_current_a = 0
        return ok

    # ------------------------------------------------------------------ #
    # Koordinering med batteri og peak-limit                              #
    # ------------------------------------------------------------------ #

    def adjust_for_trading(self, battery_action: str, grid_kw: float,
                           solar_kw: float, battery_kw: float) -> None:
        """
        Juster EVCS-ladestrøm basert på nåværende situasjon.

        Prioritetsrekkefølge:
          1. Hus (alltid)
          2. Batteri (til charge_target_soc)
          3. Elbil (overskudd som ellers eksporteres)
          4. Eksport til nett (kun det som gjenstår)

        battery_action: 'discharge', 'charge', 'idle'
        grid_kw:        Nåværende grid (positiv=import, negativ=eksport)
        solar_kw:       Sol-produksjon (kW)
        battery_kw:     Batterieffekt (positiv=lading, negativ=utlading)
        """
        if not self.is_connected():
            return

        # --- P1: Trader selger aktivt → stopp EVCS ---
        if battery_action == 'discharge':
            self.stop_charging(reason="batteri selger")
            return

        # --- Beregn overskudd tilgjengelig for elbil ---
        evcs_kw = self.get_power_kw()
        grid_without_evcs = grid_kw - evcs_kw
        export_kw = max(0, -grid_without_evcs)
        available_kw = self._peak_kw - max(0, grid_without_evcs)

        if battery_action == 'charge':
            # Batteri lader aktivt — EVCS får kun ekte sol-eksport-overskudd
            charge_kw = min(export_kw, available_kw,
                            self._max_a * self._phases * 0.23)
        else:
            # Idle/sol-reserve — gi elbilen alt som ellers ville gått til nett
            charge_kw = min(
                max(export_kw, 0) + max(0, available_kw),
                self._max_a * self._phases * 0.23,
                available_kw
            )

        charge_kw = max(0, charge_kw)
        amps = int(charge_kw * 1000 / (self._phases * 230))

        if amps >= self._min_a:
            self.set_charge_current(amps)
            logger.debug(f"EVCS: lader {amps}A ({charge_kw:.1f}kW) — "
                         f"eksport-overskudd {export_kw:.1f}kW brukes")
        else:
            if self._last_current_a > 0:
                self.stop_charging(reason="ikke nok overskudd")
            else:
                logger.debug(f"EVCS: venter — overskudd {export_kw:.2f}kW "
                             f"< min {self._min_a * self._phases * 0.23:.1f}kW")


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

    print()
    evcs = EVCSController()
    status = evcs.get_status()
    status_names = {0:'disconnected',1:'connected',2:'charging',3:'charged',4:'wait_sun',7:'low_soc'}
    print(f"EVCS status: {status} ({status_names.get(status, 'ukjent')})")
    print(f"EVCS power: {evcs.get_power_kw():.2f} kW")
    print(f"EVCS koblet til: {evcs.is_connected()}")

