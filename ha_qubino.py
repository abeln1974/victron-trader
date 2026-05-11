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

HA_URL   = os.getenv("HA_URL",   "http://192.168.1.34:8123")
HA_TOKEN = os.getenv("HA_TOKEN", "")

# Entity-IDs fra HA — basert på Qubino 3-Phase Smart Meter Node (ZMNHXD)
# Tilpass disse til faktiske entity_id i din HA-installasjon
HA_ENTITIES = {
    "power_l1":   "sensor.qubino_3_phase_smart_meter_node_electric_consumption_w",
    "power_l2":   "sensor.qubino_3_phase_smart_meter_node_electric_consumption_w_2",
    "power_l3":   "sensor.qubino_3_phase_smart_meter_node_electric_consumption_w_3",
    "power_l4":   "sensor.qubino_3_phase_smart_meter_node_electric_consumption_w_4",  # Totalt?
    "voltage_l1": "sensor.qubino_3_phase_smart_meter_node_electric_consumption_v",
    "voltage_l2": "sensor.qubino_3_phase_smart_meter_node_electric_consumption_v_3",
    "voltage_l3": "sensor.qubino_3_phase_smart_meter_node_electric_consumption_v_4",
}

# Timeout for HA-kall — Z-Wave kan være treg
HA_TIMEOUT = float(os.getenv("HA_TIMEOUT", "3.0"))
# Max alder på data før vi anser Qubino som nede (sekunder)
HA_MAX_AGE = int(os.getenv("HA_MAX_AGE", "60"))


class QubinoReader:
    """Les Qubino 3-fase smartmåler fra Home Assistant."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        })

    def _get_state(self, entity_id: str) -> Optional[float]:
        """Hent en enkelt entity-verdi fra HA."""
        try:
            r = self._session.get(
                f"{HA_URL}/api/states/{entity_id}",
                timeout=HA_TIMEOUT
            )
            if r.status_code == 200:
                data = r.json()
                state = data.get("state", "unavailable")
                if state in ("unavailable", "unknown", ""):
                    return None
                return float(state)
            elif r.status_code == 404:
                logger.debug(f"HA entity ikke funnet: {entity_id}")
            else:
                logger.warning(f"HA API {r.status_code} for {entity_id}")
        except requests.Timeout:
            logger.debug(f"HA timeout for {entity_id}")
        except (ValueError, KeyError):
            pass
        except Exception as e:
            logger.debug(f"HA feil for {entity_id}: {e}")
        return None

    def get_grid_power(self) -> Optional[Dict]:
        """
        Hent grid-effekt fra alle 3 faser.

        Returnerer dict med l1/l2/l3/total, eller None hvis Qubino er nede.
        """
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
        """Hent spenning på alle 3 faser."""
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
