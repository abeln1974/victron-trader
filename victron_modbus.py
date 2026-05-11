"""Modbus-TCP klient for styring av Victron ESS.

Basert på Victron ESS Mode 2 dokumentasjon:
- Register 37: Grid power setpoint (L1) - Positive = import, Negative = export
- Register 38: Disable charge (0=enabled, 1=disabled)
- Register 39: Disable feed-in (0=enabled, 1=disabled)
- Unit-ID 246: VE.Bus port (MultiPlus/Quattro)

Oppsett Abelgard:
- 2x MultiPlus-II 48/5000 (parallell, men styres som én enhet via VE.Bus)
- Cerbo GX v3.72
- Modbus-TCP må aktiveres: Settings → Services → Modbus-TCP → Enabled
"""
import logging
from typing import Optional
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from config import CONFIG

logger = logging.getLogger(__name__)


class VictronModbus:
    """
    Modbus-TCP klient for Victron ESS styring.
    
    ESS Mode 2: Ekstern kontroll med Grid Setpoint
    """
    
    # Modbus registers for ESS Mode 2 (VE.Bus unit)
    REG_GRID_SETPOINT  = 37     # L1 power setpoint (Watts, signed)
    REG_DISABLE_CHARGE = 38     # 0=charge enabled, 1=disabled
    REG_DISABLE_FEEDIN = 39     # 0=feed-in enabled, 1=disabled

    # System unit (100) registers
    REG_SOC            = 266    # Battery SOC (scale /10 → %)
    REG_GRID_L1        = 820    # Grid L1 power (W, signed)
    REG_PV_POWER       = 850    # PV / Solar charger power (W) - Fronius Primo
    
    def __init__(self, 
                 host: str = CONFIG.victron_host,
                 port: int = 502,
                 unit_id: int = 246):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.client: Optional[ModbusTcpClient] = None
        self._connected = False

    def connect(self) -> bool:
        """Koble til Cerbo GX Modbus-TCP."""
        try:
            self.client = ModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=10
            )
            self._connected = self.client.connect()
            if self._connected:
                logger.info(f"Modbus-TCP connected to {self.host}:{self.port}")
            else:
                logger.error(f"Failed to connect to {self.host}:{self.port}")
            return self._connected
        except Exception as e:
            logger.exception("Modbus connection failed")
            return False

    def disconnect(self):
        """Koble fra."""
        if self.client:
            self.client.close()
            self._connected = False
            logger.info("Modbus-TCP disconnected")

    def _write_register(self, address: int, value: int) -> bool:
        """Skriv til enkelt register."""
        if not self._connected or not self.client:
            logger.error("Not connected to Modbus")
            return False
        
        try:
            result = self.client.write_register(
                address=address,
                value=value,
                slave=self.unit_id
            )
            if result.isError():
                logger.error(f"Modbus write error to register {address}: {result}")
                return False
            return True
        except ModbusException as e:
            logger.exception(f"Modbus write failed for register {address}")
            return False

    def _read_register(self, address: int, count: int = 1) -> Optional[list]:
        """Les fra register."""
        if not self._connected or not self.client:
            return None
        
        try:
            result = self.client.read_holding_registers(
                address=address,
                count=count,
                slave=self.unit_id
            )
            if result.isError():
                return None
            return result.registers
        except ModbusException:
            return None

    def set_grid_setpoint(self, power_watts: int) -> bool:
        """
        Sett grid power setpoint.
        
        Args:
            power_watts: Positive = importere fra grid (lade)
                        Negative = eksportere til grid (utlade)
                        0 = la ESS styre selv (passthru/idle)
        
        Range: -32768 til 32767 Watt
        
        For Abelgard 2x MultiPlus 5000 = 10kW max:
        - Ladning: 0 til 10000W
        - Utlading: -10000W til 0W
        """
        # Clamp til gyldig range
        power_watts = max(-32768, min(32767, int(power_watts)))
        
        # Konverter til unsigned 16-bit (Modbus bruker uint16)
        if power_watts < 0:
            value = 65536 + power_watts  # To's complement til uint16
        else:
            value = power_watts
        
        success = self._write_register(self.REG_GRID_SETPOINT, value)
        if success:
            action = "import" if power_watts > 0 else "export" if power_watts < 0 else "idle"
            logger.info(f"Grid setpoint: {power_watts}W ({action})")
        return success

    def set_charge_power(self, charge_kw: float) -> bool:
        """Sett ladefart i kW."""
        watts = int(charge_kw * 1000)
        return self.set_grid_setpoint(watts)

    def set_discharge_power(self, discharge_kw: float) -> bool:
        """Sett utladefart i kW."""
        watts = -int(discharge_kw * 1000)
        return self.set_grid_setpoint(watts)

    def stop_ess_control(self) -> bool:
        """Returner kontroll til intern ESS (setpoint = 0)."""
        return self.set_grid_setpoint(0)

    def disable_charge(self, disabled: bool = True) -> bool:
        """Skru av/på lading."""
        value = 1 if disabled else 0
        return self._write_register(self.REG_DISABLE_CHARGE, value)

    def disable_feedin(self, disabled: bool = True) -> bool:
        """Skru av/på tilbakeføring til grid."""
        value = 1 if disabled else 0
        return self._write_register(self.REG_DISABLE_FEEDIN, value)

    def get_soc(self) -> Optional[float]:
        """
        Hent batteri-SOC fra system-registre.
        
        Register 266 (com.victronenergy.system) = State of Charge
        Scale factor 10 (f.eks. 850 = 85.0%)
        """
        try:
            # Bruk system unit (100) for SOC
            result = self.client.read_holding_registers(
                address=266,
                count=1,
                slave=100  # System unit
            )
            if result and not result.isError() and result.registers:
                soc_raw = result.registers[0]
                return soc_raw / 10.0
        except Exception as e:
            logger.debug(f"Could not read SOC: {e}")
        return None

    def get_grid_power(self) -> Optional[float]:
        """
        Hent grid power (L1) fra system-registre.
        
        Register 820: AC Consumption L1 (W)
        """
        try:
            result = self.client.read_holding_registers(
                address=820,
                count=1,
                slave=100
            )
            if result and not result.isError() and result.registers:
                # Signed 16-bit
                val = result.registers[0]
                if val > 32767:
                    val -= 65536
                return float(val)
        except Exception as e:
            logger.debug(f"Could not read grid power: {e}")
        return None


    def get_solar_power(self) -> Optional[float]:
        """
        Hent sol-produksjon fra Fronius Primo (AC-coupled via PV inverter).
        Register 850: PV power (W) fra system unit.
        """
        try:
            result = self.client.read_holding_registers(
                address=self.REG_PV_POWER,
                count=1,
                slave=100
            )
            if result and not result.isError() and result.registers:
                val = result.registers[0]
                if val > 32767:
                    val -= 65536
                return float(max(0, val))  # Alltid positiv (produksjon)
        except Exception as e:
            logger.debug(f"Could not read solar power: {e}")
        return None


if __name__ == "__main__":
    import os
    import time
    
    host = os.getenv("VICTRON_HOST", "192.168.1.100")
    
    vic = VictronModbus(host=host)
    
    if vic.connect():
        print(f"Connected. SOC: {vic.get_soc()}%")
        print(f"Grid power: {vic.get_grid_power()}W")
        
        # Test: Charge 2kW for 5 seconds
        print("\nTesting charge 2kW...")
        vic.set_charge_power(2.0)
        time.sleep(5)
        
        print("Testing discharge 1kW...")
        vic.set_discharge_power(1.0)
        time.sleep(5)
        
        print("Idle (ESS control)...")
        vic.stop_ess_control()
        
        vic.disconnect()
    else:
        print(f"Failed to connect to {host}:502")
        print("Sjekk at Modbus-TCP er aktivert på Cerbo GX")
