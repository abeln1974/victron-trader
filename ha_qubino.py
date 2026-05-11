"""
Henter Qubino 3-fase smartmåler data fra Home Assistant REST API.

Qubino ZMNHXD via Z-Wave — måler alle 3 faser korrekt i IT-nett.
Brukes som primærkilde for grid-effekt, med VM-3P75CT (Modbus) som fallback.

Entity-navnene er basert på HA device "Qubino 3-Phase Smart Meter Node".
Juster HA_ENTITY_* hvis navnene avviker i din installasjon.
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
# Forbruk (positiv = inn fra nett, negativ = eksport til nett)
HA_ENTITIES = {
    "power_l1":   "sensor.qubino_3_phase_smart_meter_electric_consumption_w_6",    # L1: ~888W
    "power_l2":   "sensor.qubino_3_phase_smart_meter_electric_consumption_w_2_2",  # L2: ~-1W
    "power_l3":   "sensor.qubino_3_phase_smart_meter_electric_consumption_w_3_2",  # L3: ~909W
    "voltage_l1": "sensor.qubino_3_phase_smart_meter_electric_consumption_v_2",    # ~143V
    "voltage_l2": "sensor.qubino_3_phase_smart_meter_electric_consumption_v_3_2",  # ~142V
    "voltage_l3": "sensor.qubino_3_phase_smart_meter_electric_consumption_v_4_2",  # ~141V
    "status":     "sensor.qubino_3_phase_smart_meter_node_status",                 # alive/dead
}

# Timeout for HA-kall — Z-Wave kan være treg
HA_TIMEOUT = float(os.getenv("HA_TIMEOUT", "3.0"))
# Minimum sekunder mellom kall mot HA (unngå rate-limit/ban)
HA_MIN_INTERVAL = float(os.getenv("HA_MIN_INTERVAL", "15.0"))


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

        l1 = self._get_state(HA_ENTITIES["power_l1"])
        l2 = self._get_state(HA_ENTITIES["power_l2"])
        l3 = self._get_state(HA_ENTITIES["power_l3"])

        if l1 is None and l2 is None and l3 is None:
            logger.warning("Qubino: alle faser utilgjengelig (Z-Wave nede?)")
            return None

        # Erstatt None med 0 for delvis data
        l1 = l1 or 0.0
        l2 = l2 or 0.0
        l3 = l3 or 0.0
        total = l1 + l2 + l3

        logger.debug(f"Qubino: L1={l1}W L2={l2}W L3={l3}W tot={total}W")
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    q = QubinoReader()
    print(f"HA URL: {HA_URL}")
    print(f"Token satt: {'ja' if HA_TOKEN else 'NEI — sett HA_TOKEN'}")
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
