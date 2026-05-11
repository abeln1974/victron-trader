"""MQTT-klient for styring av Victron ESS."""
import json
import time
from typing import Optional, Callable
import paho.mqtt.client as mqtt
from config import CONFIG


class VictronMQTT:
    """
    Styrer Victron ESS via MQTT mot Cerbo GX.
    
    Viktig: MQTT må være aktivert på Cerbo GX:
    Settings → Services → MQTT → Enabled (port 1883)
    """
    
    # Victron MQTT topics
    TOPIC_AC_SETPOINT = "W/{}/settings/0/Settings/CGwacs/AcPowerSetPoint"
    TOPIC_SOC = "N/{}/battery/0/Soc"
    TOPIC_GRID_POWER = "N/{}/grid/0/Power"
    TOPIC_BATTERY_POWER = "N/{}/battery/0/Power"
    
    def __init__(self, host: str = CONFIG.victron_host,
                 port: int = 1883,
                 username: str = "",
                 password: str = ""):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client: Optional[mqtt.Client] = None
        self.system_id: Optional[str] = None
        self._callbacks: dict = {}
        self._connected = False
        
        # Last known values
        self.current_soc: Optional[float] = None
        self.grid_power: Optional[float] = None
        self.battery_power: Optional[float] = None

    def connect(self) -> bool:
        """Connect to Cerbo GX MQTT broker."""
        try:
            self.client = mqtt.Client()
            if self.username:
                self.client.username_pw_set(self.username, self.password)
            
            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.on_disconnect = self._on_disconnect
            
            self.client.connect(self.host, self.port, keepalive=60)
            self.client.loop_start()
            
            # Wait for connection
            timeout = 5
            while not self._connected and timeout > 0:
                time.sleep(0.1)
                timeout -= 0.1
            
            return self._connected
        except Exception as e:
            print(f"MQTT connection failed: {e}")
            return False

    def disconnect(self):
        """Disconnect from broker."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self._connected = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            print(f"MQTT connected to {self.host}")
            # Subscribe to status topics
            self.client.subscribe("N/+/+/+/+/#")
        else:
            print(f"MQTT connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        print(f"MQTT disconnected (code {rc})")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages."""
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            
            # Extract system ID from first message
            if self.system_id is None and topic.startswith("N/"):
                parts = topic.split("/")
                if len(parts) >= 2:
                    self.system_id = parts[1]
            
            # Parse value
            if '{"value":' in payload:
                data = json.loads(payload)
                value = data.get("value")
            else:
                return

            # Update state
            if "battery/0/Soc" in topic:
                self.current_soc = float(value) if value is not None else None
            elif "grid/0/Power" in topic:
                self.grid_power = float(value) if value is not None else None
            elif "battery/0/Power" in topic:
                self.battery_power = float(value) if value is not None else None

            # Call registered callbacks
            for pattern, callback in self._callbacks.items():
                if pattern in topic:
                    callback(topic, value)

        except Exception as e:
            pass  # Ignore parse errors

    def set_grid_setpoint(self, power_watts: float) -> bool:
        """
        Set AC power setpoint to control grid import/export.
        
        Positive = import from grid (charge battery)
        Negative = export to grid (discharge battery)
        0 = keep battery at current SOC
        
        Typical ESS control: setpoint = -battery_power_target
        """
        if not self._connected or not self.system_id:
            return False
        
        topic = self.TOPIC_AC_SETPOINT.format(self.system_id)
        payload = json.dumps({"value": power_watts})
        
        result = self.client.publish(topic, payload)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def set_charge_power(self, charge_kw: float) -> bool:
        """Set battery charge power (positive) or discharge (negative)."""
        # Convert to grid setpoint
        # If we want to charge 3kW, we need to import 3kW from grid
        return self.set_grid_setpoint(charge_kw * 1000)

    def set_discharge_power(self, discharge_kw: float) -> bool:
        """Set battery discharge power."""
        # Export to grid = negative setpoint
        return self.set_grid_setpoint(-discharge_kw * 1000)

    def stop_ess_control(self) -> bool:
        """Return control to ESS internal logic (setpoint = 0)."""
        return self.set_grid_setpoint(0)

    def get_soc(self) -> Optional[float]:
        """Get current battery SOC."""
        return self.current_soc

    def register_callback(self, topic_pattern: str, callback: Callable):
        """Register callback for topic pattern."""
        self._callbacks[topic_pattern] = callback


if __name__ == "__main__":
    import os
    if not os.getenv("VICTRON_HOST"):
        print("Set VICTRON_HOST environment variable first")
        exit(1)
    
    vic = VictronMQTT()
    if vic.connect():
        print(f"Connected. SOC: {vic.get_soc()}%")
        
        # Test: charge 2kW for 5 seconds
        print("Charging 2kW...")
        vic.set_charge_power(2.0)
        time.sleep(5)
        
        print("Idle...")
        vic.stop_ess_control()
        time.sleep(2)
        
        vic.disconnect()
    else:
        print("Failed to connect")
