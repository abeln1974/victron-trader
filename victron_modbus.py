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
- 2x MultiPlus-II 48/5000/70-50 parallell, Cerbo GX v3.72 (192.168.1.60)
- SmartShunt 500A: device_id=226, SOC på reg 266
- VE.Bus: device_id=227, ESS reg 37
- System (Cerbo): device_id=100, grid reg 820, PV reg 850, ESS setpoint reg 2716
- ESS: Optimized without BatteryLife, min SOC 50%
- Modbus-TCP aktiveres: Settings → Services → Modbus-TCP → Enabled

SIKKERHET:
- ESS-sikkerhetsfunksjoner (sustain, BMS) overstyrer ALLTID våre setpoints
- Batteriet kan ikke skades via Modbus-kommandoer
- Sett setpoint=0 for å gi kontroll tilbake til ESS
"""
import os
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
    
    # ESS kontroll-registre — verifisert via test mot Cerbo GX v3.72
    # Mode 3 (External Control) krever Hub4Mode=3 + direkte VE.Bus setpoint reg 37
    REG_GRID_SETPOINT_LO  = 2716   # Hub4 AC grid setpoint (Mode 2, blir overstyrt av grid-meter!)
    REG_GRID_SETPOINT_HI  = 2717
    REG_HUB4_MODE         = 2902   # /Settings/Cgwacs/Hub4Mode: 2=Opt, 3=ESS Control Disabled
    REG_ESS_MIN_SOC       = 2901   # ESS Minimum SoC (scale x10, 200 = 20%)
    REG_MAX_CHARGE_AMP    = 2705   # DVCC max charge current (A, -1=ingen grense)
    REG_MAX_DISCHARGE_W   = 2704   # Max inverter/discharge power (W, -1=ingen grense)

    # Mode 3 — direkte VE.Bus styring (bypasser grid-meter loop)
    REG_VEBUS_ESS_SETPOINT_L1 = 37   # ESS power setpoint phase 1 (signed16, W, unit 227)
    REG_VEBUS_ESS_SETPOINT_L2 = 40   # ESS power setpoint phase 2
    REG_VEBUS_ESS_SETPOINT_L3 = 41   # ESS power setpoint phase 3

    HUB4_MODE_OPTIMIZED   = 2      # GX styrer selv (med BatteryLife av for NMC)
    HUB4_MODE_DISABLED    = 3      # ESS control disabled — vi tar over via VE.Bus reg 37

    # System unit (100) read-only registre
    REG_SOC               = 266    # Battery SOC (scale /10 → %)
    REG_GRID_L1           = 820    # Grid L1 power (W, signed)
    REG_GRID_L2           = 821    # Grid L2 power (W, signed)
    REG_GRID_L3           = 822    # Grid L3 power (W, signed) — 0W på IT-nett!
    REG_PV_POWER          = 808    # AC-coupled PV on AC output L1 (W) - Fronius Primo 5kW
    REG_BATTERY_POWER     = 842    # Battery Power System (W, signed16, positiv=lading)

    # Unit-ID — verifisert via Modbus device scan mot 192.168.1.60
    UNIT_SYSTEM           = 100    # com.victronenergy.system: grid(820), pv(850), ESS(2716/2902)
    UNIT_BATTERY          = 226    # com.victronenergy.battery: SmartShunt 500A — SOC(266)
    UNIT_VEBUS            = 227    # com.victronenergy.vebus: MultiPlus-II parallell
    
    def __init__(self,
                 host: str = CONFIG.victron_host,
                 port: int = 502):
        self.host = host
        self.port = port
        self.client: Optional[ModbusTcpClient] = None
        self._connected = False
        # READONLY_MODE=true → les alt, skriv ingenting
        self.readonly = os.getenv("READONLY_MODE", "false").lower() == "true"
        if self.readonly:
            logger.info("🔒 READONLY_MODE aktiv — ingen skriving til Cerbo GX")

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
        if self.readonly:
            logger.warning(f"🔒 READONLY_MODE: blokkerte skriving til reg {address}={value}")
            return False
        if not self._connected or not self.client:
            logger.error("Not connected to Modbus")
            return False
        uid = unit if unit is not None else self.UNIT_SYSTEM
        try:
            result = self.client.write_register(address=address, value=value, device_id=uid)
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
        uid = unit if unit is not None else self.UNIT_SYSTEM
        try:
            result = self.client.read_holding_registers(address=address, count=count, device_id=uid)
            if result.isError():
                return None
            return result.registers
        except ModbusException:
            return None

    def set_grid_setpoint(self, power_watts: int) -> bool:
        """
        Sett ESS setpoint via VE.Bus reg 37 (Mode 3, direkte til MultiPlus).
        Bypasser GX grid-meter loop som ellers overstyrer setpointet.

        Args:
            power_watts: Positiv = importer fra grid (lad batteri)
                         Negativ = eksporter til grid (utlad batteri)
                         0 = ingen setpoint

        VIKTIG: Må kalles minst hvert 10. sekund. Multi resetter setpoint etter ~10s.
        Hub4Mode må være 3 (ESS Control Disabled) for full effekt.
        """
        if self.readonly:
            logger.warning(f"🔒 READONLY_MODE: blokkerte ESS setpoint {power_watts}W")
            return False
        w = int(power_watts)
        # signed16 → uint16 (range -32768 til 32767 W per fase)
        if w > 32767: w = 32767
        if w < -32768: w = -32768
        val = w + 65536 if w < 0 else w
        try:
            result = self.client.write_register(
                address=self.REG_VEBUS_ESS_SETPOINT_L1,
                value=val,
                device_id=self.UNIT_VEBUS
            )
            if result.isError():
                logger.error(f"Modbus write error reg 37 (VE.Bus): {result}")
                return False
            self._last_setpoint = w
            action = "import" if w > 0 else "export" if w < 0 else "idle"
            logger.info(f"ESS setpoint (reg37 VE.Bus): {w}W ({action})")
            return True
        except Exception as e:
            logger.exception("set_grid_setpoint feilet")
            return False

    def _ensure_external_control(self) -> bool:
        """Sjekk at Hub4Mode=3 (ESS Control Disabled) før setpoint.
        
        Mode 3 krever at GX ikke overstyrer setpoint via grid-meter loop.
        Returnerer True hvis Hub4Mode=3, False ved feil.
        """
        try:
            r = self.client.read_holding_registers(
                address=self.REG_HUB4_MODE, count=1, device_id=self.UNIT_SYSTEM)
            if r and not r.isError():
                if r.registers[0] != self.HUB4_MODE_DISABLED:
                    write_result = self.client.write_register(
                        address=self.REG_HUB4_MODE, value=self.HUB4_MODE_DISABLED,
                        device_id=self.UNIT_SYSTEM)
                    if write_result.isError():
                        logger.error("Kunne ikke sette Hub4Mode=3")
                        return False
                    logger.info("Bytter Hub4Mode → 3 (ESS Control Disabled)")
                return True
            else:
                logger.error("Kunne ikke lese Hub4Mode")
                return False
        except Exception:
            logger.exception("_ensure_external_control feilet")
            return False

    def set_charge_power(self, charge_kw: float) -> bool:
        """Sett ladefart i kW. Bytter til Mode 4 (External Control) automatisk."""
        if not self._ensure_external_control():
            logger.warning("Kunne ikke sikre ekstern kontroll for charge")
            return False
        watts = int(charge_kw * 1000)
        return self.set_grid_setpoint(watts)

    def set_discharge_power(self, discharge_kw: float) -> bool:
        """Sett utladefart i kW. Bytter til Mode 4 (External Control) automatisk."""
        # Sikre ekstern kontroll og verifiser
        if not self._ensure_external_control():
            logger.warning("Kunne ikke sikre ekstern kontroll for discharge")
            return False
        
        # Verifiser Hub4Mode=3 før setpoint
        try:
            r = self.client.read_holding_registers(
                address=self.REG_HUB4_MODE, count=1, device_id=self.UNIT_SYSTEM)
            if r.isError() or r.registers[0] != self.HUB4_MODE_DISABLED:
                logger.warning("Hub4Mode ikke 3 før discharge setpoint")
                return False
        except Exception:
            logger.warning("Kunne ikke verifisere Hub4Mode før discharge")
            return False
        
        watts = -int(discharge_kw * 1000)
        return self.set_grid_setpoint(watts)

    def enable_external_control(self) -> bool:
        """Sett Hub4Mode=3 (ESS Control Disabled) for full kontroll via reg 37."""
        if self.readonly:
            return False
        try:
            r = self.client.write_register(
                address=self.REG_HUB4_MODE, value=self.HUB4_MODE_DISABLED,
                device_id=self.UNIT_SYSTEM)
            if not r.isError():
                logger.info("Hub4Mode = 3 (ESS Control Disabled)")
                return True
        except Exception:
            logger.exception("enable_external_control feilet")
        return False

    def disable_external_control(self) -> bool:
        """Tilbake til Hub4Mode=2 (Optimized without BatteryLife) for NMC."""
        if self.readonly:
            return False
        try:
            # Reset VE.Bus setpoint først
            self.client.write_register(
                address=self.REG_VEBUS_ESS_SETPOINT_L1, value=0,
                device_id=self.UNIT_VEBUS)
            r = self.client.write_register(
                address=self.REG_HUB4_MODE, value=self.HUB4_MODE_OPTIMIZED,
                device_id=self.UNIT_SYSTEM)
            if not r.isError():
                logger.info("Hub4Mode = 2 (Optimized) — GX overtar styring")
                return True
        except Exception:
            logger.exception("disable_external_control feilet")
        return False

    def get_ess_mode(self) -> Optional[int]:
        """Les nåværende Hub4Mode (reg 2902)."""
        try:
            r = self.client.read_holding_registers(
                address=self.REG_HUB4_MODE, count=1, device_id=self.UNIT_SYSTEM)
            if r and not r.isError():
                return r.registers[0]
        except Exception:
            pass
        return None

    def set_min_soc(self, soc_pct: float) -> bool:
        """Sett ESS minimum SOC (reg 2901, scale x10). F.eks. 20.0 → skriver 200."""
        if self.readonly:
            return False
        try:
            val = int(soc_pct * 10)
            r = self.client.write_register(
                address=self.REG_ESS_MIN_SOC, value=val, device_id=self.UNIT_SYSTEM)
            if not r.isError():
                logger.info(f"ESS min SOC satt til {soc_pct:.0f}%")
                return True
        except Exception:
            logger.exception("set_min_soc feilet")
        return False

    def stop_ess_control(self) -> bool:
        """Returner kontroll til GX/ESS Optimized — NMC-vennlig idle.
        Bytter Hub4Mode tilbake til 2 (Optimized without BatteryLife)."""
        self._last_setpoint = 0
        if not self.readonly:
            try:
                # Nullstill VE.Bus setpoint
                self.client.write_register(
                    address=self.REG_VEBUS_ESS_SETPOINT_L1, value=0,
                    device_id=self.UNIT_VEBUS)
                # Bytt Hub4Mode tilbake til 2
                r = self.client.read_holding_registers(
                    address=self.REG_HUB4_MODE, count=1, device_id=self.UNIT_SYSTEM)
                if r and not r.isError() and r.registers[0] != self.HUB4_MODE_OPTIMIZED:
                    self.client.write_register(
                        address=self.REG_HUB4_MODE, value=self.HUB4_MODE_OPTIMIZED,
                        device_id=self.UNIT_SYSTEM)
                    logger.info("Hub4Mode → 2 (idle, GX overtar)")
            except Exception:
                pass
        return True

    def send_keepalive(self) -> bool:
        """
        Gjenta siste setpoint på reg 37 (VE.Bus). Mode 3 krever skriving
        minst hvert 10. sekund — ellers nullstilles setpointet av Multi.
        """
        last = getattr(self, '_last_setpoint', 0)
        if last == 0:
            return True  # ingen aktiv setpoint, ikke nodvendig
        return self.set_grid_setpoint(last)

    def set_max_charge_current(self, amps: int) -> bool:
        """DVCC max charge current. -1 = ingen grense. (Register 2705)"""
        val = amps if amps >= 0 else 0xFFFF  # -1 som uint16
        try:
            result = self.client.write_register(
                address=self.REG_MAX_CHARGE_AMP, value=val, device_id=self.UNIT_SYSTEM)
            return not result.isError()
        except Exception:
            return False

    def set_max_discharge_power(self, watts: int) -> bool:
        """Max inverter/discharge power. -1 = ingen grense. (Register 2704)"""
        val = watts if watts >= 0 else 0xFFFF
        try:
            result = self.client.write_register(
                address=self.REG_MAX_DISCHARGE_W, value=val, device_id=self.UNIT_SYSTEM)
            return not result.isError()
        except Exception:
            return False

    def _read_signed16(self, address: int) -> Optional[float]:
        """Les ett signed 16-bit register fra Unit 100."""
        try:
            result = self.client.read_holding_registers(
                address=address, count=1, device_id=self.UNIT_SYSTEM)
            if result and not result.isError() and result.registers:
                val = result.registers[0]
                return float(val - 65536 if val > 32767 else val)
        except Exception as e:
            logger.debug(f"Read reg {address} feilet: {e}")
        return None

    def get_soc(self) -> Optional[float]:
        """Battery SOC. Register 266, scale /10. (SmartShunt unit 226)"""
        try:
            result = self.client.read_holding_registers(
                address=self.REG_SOC, count=1, device_id=self.UNIT_BATTERY)
            if result and not result.isError() and result.registers:
                return result.registers[0] / 10.0
        except Exception as e:
            logger.debug(f"get_soc feilet: {e}")
        return None

    def get_battery_power(self) -> int:
        """Returnerer batterieffekt i Watt (positiv=lading, negativ=utlading)."""
        try:
            r = self.client.read_holding_registers(
                address=self.REG_BATTERY_POWER, count=1, device_id=self.UNIT_SYSTEM)
            if r.isError():
                return 0
            val = r.registers[0]
            # Håndter signed16 verdier
            return val - 65536 if val > 32767 else val
        except Exception:
            logger.exception("get_battery_power feilet")
            return 0

    def get_grid_power(self) -> Optional[float]:
        """
        Total grid-effekt for Abelgård 3-fase IT-nett.

        VM-3P75CT måler L1 og L2 korrekt, men L3 viser 0W selv om det går strøm der.
        Dette er en kjent begrensning med VM-3P75CT i 3-fase IT-nett (230V L-N).
        Reell grid = L1 + L2 + L3(målt=0 pga IT-nett) = L1 + L2.
        For nøyaktig total anbefales energibalanse via get_power_balance().
        """
        l1 = self._read_signed16(self.REG_GRID_L1)
        l2 = self._read_signed16(self.REG_GRID_L2)
        if l1 is not None and l2 is not None:
            return l1 + l2
        return l1

    def get_grid_phases(self) -> dict:
        """Les alle tre faser (L3 = 0W pga IT-nett målerbegrensning)."""
        return {
            "l1": self._read_signed16(self.REG_GRID_L1),
            "l2": self._read_signed16(self.REG_GRID_L2),
            "l3": self._read_signed16(self.REG_GRID_L3),
        }

    def get_power_balance(self) -> dict:
        """
        Energibalanse for å kompensere for manglende L3-måling.

        Kirchhoffs lov for AC-nett:
          Sol (Fronius) + Grid (inn) = Forbruk + Batteri (inn)
          => Grid_reell = Batteri_effekt + Forbruk - Sol

        Alternativt: vi kan beregne antatt L3 fra batteri+sol-balansen.
        Batteri-effekt fra SmartShunt er alltid korrekt (måler DC-siden).
        """
        l1  = self._read_signed16(self.REG_GRID_L1) or 0
        l2  = self._read_signed16(self.REG_GRID_L2) or 0
        sol = self._read_signed16(self.REG_PV_POWER) or 0
        bat = self._read_signed16(842) or 0   # reg 842 = batteri DC-effekt

        grid_measured = l1 + l2           # Hva måleren ser (mangler L3)
        # Balanse: alt målt fra DC-siden er korrekt
        # Positiv bat = lader (strøm INN i batteri fra grid/sol)
        # Negativ bat = utlader
        return {
            "l1": l1, "l2": l2, "l3_measured": 0,
            "grid_measured_w": grid_measured,
            "solar_w": sol,
            "battery_w": bat,
            "note": "L3=0W: VM-3P75CT måler ikke L3 i 3-fase IT-nett"
        }


    def get_solar_power(self) -> Optional[float]:
        """
        Hent sol-produksjon fra Fronius Primo 5kW (AC-coupled på AC output).
        Register 808: AC Consumption L1 = PV-produksjon når Fronius er på output-siden.
        Verifisert 2026-05-11: reg808@device100 = 750W med sol ute.
        """
        raw = self._read_signed16(self.REG_PV_POWER)
        if raw is not None:
            return float(max(0, raw))
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
