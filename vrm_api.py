"""VRM API klient som alternativ til MQTT (for DESS-aktive systemer).

Abelgard har DESS aktiv - dette kolliderer med ESS-styring via MQTT.
VRM API kan brukes for å:
1. Lese SOC, effekt, priser
2. Overstyre DESS midlertidig (krever experimental access)

Site ID: 411797
Token: Må genereres i VRM → Settings → Integrations
"""
import requests
import json
from typing import Optional, Dict, Any
from config import CONFIG


class VRMAPI:
    """Victron Remote Management API client."""
    
    BASE_URL = "https://vrmapi.victronenergy.com/v2"
    
    def __init__(self, token: str, site_id: str = "411797"):
        self.token = token
        self.site_id = site_id
        self.headers = {
            "X-Authorization": f"Token {token}",
            "Content-Type": "application/json"
        }
    
    def get_site_info(self) -> Optional[Dict]:
        """Get site information."""
        url = f"{self.BASE_URL}/installations/{self.site_id}"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"VRM API error: {e}")
            return None
    
    def get_battery_state(self) -> Optional[Dict[str, Any]]:
        """Get current battery state (SOC, voltage, current, power)."""
        # Attributes: 26=SOC, 29=voltage, 30=current
        url = f"{self.BASE_URL}/installations/{self.site_id}/diagnostics"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Parse diagnostics
            result = {}
            for item in data.get("records", []):
                code = item.get("idDataAttribute")
                if code == 26:  # SOC
                    result["soc"] = item.get("formattedValue")
                elif code == 29:  # Voltage
                    result["voltage"] = item.get("formattedValue")
                elif code == 30:  # Current
                    result["current"] = item.get("formattedValue")
            return result
        except requests.RequestException as e:
            print(f"VRM API error: {e}")
            return None
    
    def get_grid_stats(self) -> Optional[Dict[str, Any]]:
        """Get grid power and stats."""
        url = f"{self.BASE_URL}/installations/{self.site_id}/stats?type=custom&attributeCodes[]=cGridOvervoltage&attributeCodes[]=cGridRelay"
        # For enkel strøm-data, bruk stats endepunkt
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"VRM API error: {e}")
            return None


if __name__ == "__main__":
    import os
    token = os.getenv("VRM_TOKEN")
    if not token:
        print("Sett VRM_TOKEN miljøvariabel")
        exit(1)
    
    vrm = VRMAPI(token)
    
    print("Henter site info...")
    info = vrm.get_site_info()
    if info:
        print(f"Site: {info.get('records', {}).get('name')}")
    
    print("\nHenter batteri-status...")
    battery = vrm.get_battery_state()
    if battery:
        print(f"SOC: {battery.get('soc', 'Ukjent')}")
