"""Modbus-TCP klient for styring av Victron ESS.

Basert på Victron ESS Mode 2 dokumentasjon (Venus OS 3.50+):
https://www.victronenergy.com/live/ess:ess_mode_2_and_3

Viktige registre (Unit-ID 100 = com.victronenergy.system):
- Register 2716/2717: Grid power setpoint 32-bit (Venus >= 3.50)
  Positiv = importer fra grid (lad batteri)
  Negativ = eksporter til grid (utlad batteri)
  VIKTIG: Må skrives minst hvert 60. sekund, ellers nullstilles det
- Register 2705: DVCC max charge current (-1 = ingen grense, Ampere)
- Register 2704: Max inverter/discharge power (-1 = ingen grense, Watt)
- Register 2701: Disable charge (0=lad, 100=deaktivert) [deprecated, bruk 2705]
- Register 2702: Disable inverter/discharge (0=aktiv, 100=deaktivert) [deprecated, bruk 2704]

Read-only system-registre (Unit-ID 100):
- Register 266: Battery SOC (skala /10 → %)
- Register 820: Grid L1 power (W, signed)
- Register 850: PV power (W)

OPPSETT ABELGÅRD:
- 2x MultiPlus-II 48/5000, Cerbo GX v3.72
- ESS: Optimized without BatteryLife, min SOC 50%
- Modbus-TCP aktiveres: Settings → Services → Modbus-TCP → Enabled

SIKKERHET:
- ESS-sikkerhetsfunksjoner (sustain, BMS) overstyrer ALLTID våre setpoints
- Batteriet kan ikke skades via Modbus-kommandoer
- Sett setpoint=0 for å gi kontroll tilbake til ESS
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
    
    # ESS Mode 2 kontroll-registre (Unit-ID 100, Venus OS >= 3.50)
    # Kilde: https://www.victronenergy.com/live/ess:ess_mode_2_and_3
    REG_GRID_SETPOINT_LO  = 2716   # Grid power setpoint 32-bit low word (W)
    REG_GRID_SETPOINT_HI  = 2717   # Grid power setpoint 32-bit high word (W)
    REG_MAX_CHARGE_AMP    = 2705   # DVCC max charge current (A, -1=ingen grense)
    REG_MAX_DISCHARGE_W   = 2704   # Max inverter/discharge power (W, -1=ingen grense)

    # System unit (100) read-only registre
    REG_SOC               = 266    # Battery SOC (scale /10 → %)
    REG_GRID_L1           = 820    # Grid L1 power (W, signed)
    REG_PV_POWER          = 850    # PV / Solar charger power (W) - Fronius Primo

    # Unit-ID for ESS kontroll og systemdata
    UNIT_SYSTEM           = 100    # com.victronenergy.system (alle ESS-registre)
    
    def __init__(self,
                 host: str = CONFIG.victron_host,
                 port: int = 502):
        self.host = host
        self.port = port
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

    def _write_register(self, address: int, value: int, unit: int = None) -> bool:
        """Skriv til enkelt register."""
        if not self._connected or not self.client:
            logger.error("Not connected to Modbus")
            return False
        slave = unit if unit is not None else self.UNIT_SYSTEM
        try:
            result = self.client.write_register(address=address, value=value, slave=slave)
            if result.isError():
                logger.error(f"Modbus write error to register {address}: {result}")
                return False
            return True
        except ModbusException as e:
            logger.exception(f"Modbus write failed for register {address}")
            return False

    def _read_register(self, address: int, count: int = 1, unit: int = None) -> Optional[list]:
        """Les fra register."""
        if not self._connected or not self.client:
            return None
        slave = unit if unit is not None else self.UNIT_SYSTEM
        try:
            result = self.client.read_holding_registers(address=address, count=count, slave=slave)
            if result.isError():
                return None
            return result.registers
        except ModbusException:
            return None

    def set_grid_setpoint(self, power_watts: int) -> bool:
        """
        Sett grid power setpoint via 32-bit register 2716/2717 (Venus OS >= 3.50).

        Args:
            power_watts: Positiv = importer fra grid (lad batteri)
                         Negativ = eksporter til grid (utlad batteri)
                         0 = la ESS styre selv

        VIKTIG: Må kalles minst hvert 60. sekund for å holde setpointet aktivt.
        Kilde: https://www.victronenergy.com/live/ess:ess_mode_2_and_3
        """
        # 32-bit signed → to 16-bit registre (big-endian)
        w = int(power_watts)
        # Konverter til unsigned 32-bit
        if w < 0:
            w_unsigned = w + (1 << 32)
        else:
            w_unsigned = w
        hi = (w_unsigned >> 16) & 0xFFFF
        lo = w_unsigned & 0xFFFF

        try:
            result = self.client.write_registers(
                address=self.REG_GRID_SETPOINT_LO,
                values=[lo, hi],
                slave=self.UNIT_SYSTEM
            )
            if result.isError():
                logger.error(f"Modbus write error reg 2716/2717: {result}")
                return False
            action = "import" if power_watts > 0 else "export" if power_watts < 0 else "idle"
            logger.info(f"Grid setpoint: {power_watts}W ({action})")
            return True
        except Exception as e:
            logger.exception("set_grid_setpoint feilet")
            return False

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

    def set_max_charge_current(self, amps: int) -> bool:
        """DVCC max charge current. -1 = ingen grense. (Register 2705)"""
        val = amps if amps >= 0 else 0xFFFF  # -1 som uint16
        try:
            result = self.client.write_register(
                address=self.REG_MAX_CHARGE_AMP, value=val, slave=self.UNIT_SYSTEM)
            return not result.isError()
        except Exception:
            return False

    def set_max_discharge_power(self, watts: int) -> bool:
        """Max inverter/discharge power. -1 = ingen grense. (Register 2704)"""
        val = watts if watts >= 0 else 0xFFFF
        try:
            result = self.client.write_register(
                address=self.REG_MAX_DISCHARGE_W, value=val, slave=self.UNIT_SYSTEM)
            return not result.isError()
        except Exception:
            return False

    def _read_signed16(self, address: int) -> Optional[float]:
        """Les ett signed 16-bit register fra Unit 100."""
        try:
            result = self.client.read_holding_registers(
                address=address, count=1, slave=self.UNIT_SYSTEM)
            if result and not result.isError() and result.registers:
                val = result.registers[0]
                return float(val - 65536 if val > 32767 else val)
        except Exception as e:
            logger.debug(f"Read reg {address} feilet: {e}")
        return None

    def get_soc(self) -> Optional[float]:
        """Battery SOC. Register 266, scale /10. (Unit 100)"""
        raw = self._read_signed16(self.REG_SOC)
        return raw / 10.0 if raw is not None else None

    def get_grid_power(self) -> Optional[float]:
        """Grid L1 power in Watt. Register 820, signed. (Unit 100)"""
        return self._read_signed16(self.REG_GRID_L1)


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
