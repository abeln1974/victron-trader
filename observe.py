"""
READ-ONLY observasjon av Cerbo GX via Modbus-TCP.

Ingen skriving — kun lesing av registre for å verifisere
at vi leser riktige verdier FØR vi sender setpoints.

Kjør:
  docker compose run --rm victron-trader python observe.py
"""
import os
import time
from datetime import datetime
from pymodbus.client import ModbusTcpClient

HOST = os.getenv("VICTRON_HOST", "192.168.1.60")
PORT = int(os.getenv("VICTRON_MODBUS_PORT", "502"))
UNIT = 100  # com.victronenergy.system

# Registre vi vil lese (alle read-only)
REGISTERS = [
    (266,  1, 10.0,   "Batteri SOC",          "%"),
    (820,  1, 1.0,    "Grid L1 effekt",        "W"),
    (817,  1, 1.0,    "Grid L2 effekt",        "W"),
    (818,  1, 1.0,    "Grid L3 effekt",        "W"),
    (850,  1, 1.0,    "PV/Sol effekt",         "W"),
    (842,  1, 1.0,    "Batteri effekt",        "W"),
    (843,  1, 1.0,    "Batteri spenning",      "V (x10)"),
    (844,  1, 1.0,    "Batteri strøm",         "A (x10)"),
    (2700, 1, 1.0,    "ESS setpoint (gammel)", "W"),
    (2716, 2, 1.0,    "ESS setpoint (ny)",     "W (32-bit)"),
]


def read_signed16(client, address):
    result = client.read_holding_registers(address=address, count=1, slave=UNIT)
    if result.isError():
        return None
    val = result.registers[0]
    return val - 65536 if val > 32767 else val


def read_32bit(client, address):
    result = client.read_holding_registers(address=address, count=2, slave=UNIT)
    if result.isError():
        return None
    lo, hi = result.registers[0], result.registers[1]
    val = (hi << 16) | lo
    return val - (1 << 32) if val >= (1 << 31) else val


def main():
    print(f"Kobler til Cerbo GX på {HOST}:{PORT} ...")
    client = ModbusTcpClient(host=HOST, port=PORT, timeout=10)

    if not client.connect():
        print(f"❌ Kunne ikke koble til {HOST}:{PORT}")
        print("Sjekk: Settings → Services → Modbus-TCP → Enabled på Cerbo GX")
        return

    print(f"✅ Tilkoblet! Leser registre (ingen skriving) — Ctrl+C for å avslutte\n")
    print(f"{'Tid':<10} {'Register':<8} {'Beskrivelse':<25} {'Verdi':<12} Enhet")
    print("─" * 72)

    try:
        while True:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n{'─'*72}")
            print(f"  Observasjon kl {now}  (Cerbo GX v3.72 / {HOST})")
            print(f"{'─'*72}")

            for addr, count, scale, label, unit in REGISTERS:
                try:
                    if count == 2:
                        raw = read_32bit(client, addr)
                    else:
                        raw = read_signed16(client, addr)

                    if raw is None:
                        print(f"  reg {addr:<5}  {label:<25} {'ERROR':<12} {unit}")
                        continue

                    if addr == 266:  # SOC
                        display = f"{raw / scale:.1f}"
                    elif addr in (843, 844):  # Spenning/strøm
                        display = f"{raw / 10.0:.1f}"
                    else:
                        display = f"{int(raw)}"

                    print(f"  reg {addr:<5}  {label:<25} {display:<12} {unit}")
                except Exception as e:
                    print(f"  reg {addr:<5}  {label:<25} {'EXC: ' + str(e)[:20]}")

            print(f"\n  ⚠️  INGEN SKRIVING — kun observasjon")
            time.sleep(15)

    except KeyboardInterrupt:
        print("\n\nAvslutter observasjon.")
    finally:
        client.close()
        print("Frakoblet.")


if __name__ == "__main__":
    main()
