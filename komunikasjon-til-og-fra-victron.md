# Kommunikasjon: Victron Trader ↔ Cerbo GX / MultiPlus

**Generert:** 2026-05-18  
**Sist oppdatert:** Se live logger under [Live Overvåkning](#live-overvåkning)

---

## 1. Nettverkstilkobling

| Parameter | Verdi |
|-----------|-------|
| **Protokoll** | Modbus TCP |
| **Vert** | `192.168.1.114` (satt via `VICTRON_HOST` env) |
| **Port** | `502` (standard Modbus TCP) |
| **Modbus Unit IDs** | 227 (VE.Bus), 100 (System), 225 (SmartShunt) |
| **Timeout** | 5 sekunder |

---

## 2. Modbus-registre (les/skriv)

### 2.1 Lese-registre (Input/Holding)

| Register | Adresse | Unit | Skala | Beskrivelse |
|----------|---------|------|-------|-------------|
| **SOC** | reg 843 | 225 | /10 | Batteri State of Charge (f.eks. 850 = 85.0%) |
| **ESS Min SOC** | reg 2901 | 100 | /10 | Minimum SOC grense (f.eks. 200 = 20.0%) |
| **Grid L1** | reg 820 | 227 | 1 | Nettimport/eksport fase 1 (W) |
| **Grid L2** | reg 821 | 227 | 1 | Nettimport/eksport fase 2 (W) |
| **Grid L3** | reg 822 | 227 | 1 | Nettimport/eksport fase 3 (W) |
| **Battery Power** | reg 842 | 225 | 1 | Batterieffekt (negativ = utlading) |
| **Solar Power** | reg 808 | 227 | 1 | Solar AC produksjon (Fronius) |
| **Hub4 Mode** | reg 2902 | 100 | 1 | ESS styremodus (0=Off, 1=External, 2=Optimized, 3=OptimizedWithBatteryLife) |
| **Discharged Energy** | reg 309 | 225 | /10 | SmartShunt utladet energi (kWh) |
| **Charged Energy** | reg 310 | 225 | /10 | SmartShunt ladet energi (kWh) |

### 2.2 Skrive-registre (Coil/Holding)

| Register | Adresse | Unit | Skala | Beskrivelse |
|----------|---------|------|-------|-------------|
| **ESS Setpoint** | reg 37 | 227 | 1 | Setpoint i W (positiv = lade, negativ = utlade) |
| **ESS Min SOC** | reg 2901 | 100 | /10 | Sett minimum SOC |
| **Hub4 Mode** | reg 2902 | 100 | 1 | Bytte styremodus (3 = External Control) |
| **DVCC Max Current** | reg 2705 | 100 | 1 | Maks ladestrøm (0 = stopp lading) |

---

## 3. Kommunikasjonsflyt

### 3.1 Oppstart-sekvens
```
1. CONNECT → TCP 192.168.1.114:502
2. READ reg 2902 (Hub4Mode) → Sjekk nåværende modus
3. WRITE reg 37 = 0 (nullstill setpoint)
4. WRITE reg 2902 = 3 (External Control Mode)
5. WRITE reg 2901 = 200 (Min SOC 20%)
```

### 3.2 Normal drift (hver time)
```
1. READ reg 843 (SOC)
2. READ reg 820-822 (Grid power per fase)
3. READ reg 808 (Solar power)
4. READ reg 842 (Battery power)
5. WRITE reg 37 = ±X W (sett setpoint ved aktiv trading)
```

### 3.3 Keepalive (hver 8. sekund)
```
WRITE reg 37 = forrige verdi (refresher setpoint)
→ Viktig: Ved krasj stopper keepalive → Victron går til Mode 2 automatisk
```

### 3.4 Peak-shaving sjekk (hver 10. sekund)
```
READ reg 820-822 → Grid power
IF grid > 9.5kW:
  WRITE reg 37 = discharge setpoint (reduser nettimport)
```

### 3.5 Handels-logging (ved time-skifte eller action stopp)
```
READ reg 843 (end SOC)
READ reg 309, 310 (SmartShunt energitellere)
→ Beregn delta fra start av action
→ Logg til SQLite database
```

---

## 4. ESS Styremodi

| Mode | Verdi | Beskrivelse |
|------|-------|-------------|
| **Optimized** | 2 | Victron styrer selv (Mode 2) |
| **External Control** | 3 | Trader tar kontroll (Mode 3) |
| **Off** | 0 | Ingen ESS styring |

**Bytte til trading:**
- Trader bytter til Mode 3 ved aktiv trading (charge/discharge)
- Ved idle: Beholder Mode 3 med setpoint=0 (Victron styrer likevel)
- Ved shutdown: Trader bytter tilbake til Mode 2

---

## 5. Live Overvåkning

### Siste logger fra container:

```bash
# Hent live logger:
docker logs victron-trader --tail 100 --follow
```

---

## 6. Kommandoer for feilsøking

### 6.1 Sjekk live Modbus-kommunikasjon
```bash
# Se trader logger i sanntid
docker logs victron-trader --tail 50 --follow

# Sjekk nettverk til Cerbo
ping 192.168.1.114

# Test Modbus TCP port
telnet 192.168.1.114 502
```

### 6.2 Sjekk nåværende verdier fra Cerbo
```bash
# Via trader container
docker exec victron-trader python3 -c "
from victron_modbus import VictronModbus
v = VictronModbus()
v.connect()
print(f'SOC: {v.get_soc()}%')
print(f'Grid: {v.get_grid_power()}W')
print(f'Solar: {v.get_solar_power()}W')
print(f'Battery: {v.get_battery_power()}W')
print(f'ESS Mode: {v.get_ess_mode()}')
"
```

### 6.3 Sjekk database (handler)
```bash
# Se siste handler
docker exec victron-trader sqlite3 /opt/victron-trader/data/profit.db \
  "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 10;"

# Se handler gruppert per time
docker exec victron-web curl -s http://localhost:8080/api/trades/hourly | python3 -m json.tool
```

---

## 7. Vanlige feilmønstre

| Symptom | Mulig årsak | Løsning |
|---------|-------------|---------|
| "Modbus ikke tilkoblet" | Nettverk/Cerbo nede | Sjekk `ping 192.168.1.114` |
| "Kunne ikke sikre ekstern kontroll" | ESS i feil modus | Sjekk Hub4Mode i Victron VRM |
| "Export-guard trigget" | Batteriet lader istedenfor utlader | Normalt ved høy sol |
| "SOC ukjent" | SmartShunt/Modbus feil | Sjekk Modbus TCP tilkobling |
| Keepalive timeout → Mode 2 | Trader krasj/vert restart | Trader restarter automatisk |

---

## 8. Kommunikasjonsoversikt (hver syklus)

```
┌─────────────────┐     ┌──────────────────┐
│  Victron Trader │◄───►│  Cerbo GX        │
│  (Python/Modbus)│     │  (VE.Bus/Modbus) │
└─────────────────┘     └────────┬─────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
               ┌────▼───┐   ┌────▼───┐   ┌────▼───┐
               │MultiPlus│   │SmartShunt│   │Fronius │
               │  (ESS)  │   │ (SOC/kWh)│   │(Solar) │
               └─────────┘   └─────────┘   └─────────┘
```

**Frekvens:**
- Hver 1 sekund: Hovedløkke sjekk
- Hver 8 sekund: Keepalive (setpoint refresh)
- Hver 10 sekund: Peak-shaving sjekk
- Hver time: Trade cycle + planlegging
- Hver 5 minutt: Status logging

---

*Dokument generert automatisk fra kodebase og live logger*
    2026-05-18 17:12:43,660 - INFO - Starting Energy Trader...
    2026-05-18 17:12:43,661 - INFO - Modbus-TCP connected to 192.168.1.60:502
    2026-05-18 17:12:43,661 - INFO - Connected via Modbus-TCP. Reading SOC...
    2026-05-18 17:12:44,665 - INFO - Startup-reset: reg37=0, Hub4Mode=3 (trader tar kontroll)
    2026-05-18 17:12:44,667 - INFO - ESS min SOC satt til 35%
    2026-05-18 17:12:44,667 - INFO - ESS min SOC: 35%  max SOC: 90%
    2026-05-18 17:12:44,667 - INFO - ==================================================
    2026-05-18 17:12:44,667 - INFO - Trade cycle 2026-05-18 17:12:44 CEST
    2026-05-18 17:12:44,670 - INFO - SOC: 78.1%
    2026-05-18 17:12:44,722 - INFO - Spotpris: 1.756 kr/kWh
    2026-05-18 17:12:44,723 - INFO - Sol: 0.48 kW
    2026-05-18 17:12:44,861 - INFO - Open-Meteo sol-prognose i morgen: 24.9 kWh (5.0 eff. timer)
    2026-05-18 17:12:44,861 - INFO - Dynamisk sol-reserve: 40.0% SOC (24.9 kWh prognose i morgen)
    2026-05-18 17:12:44,861 - INFO - Action: idle @ 0.0kW | 
    2026-05-18 17:12:44,863 - INFO - Idle — ESS styrer (Mode 3, setpoint=0)
    2026-05-18 17:12:44,864 - INFO - Dagens profitt: 0.00 kr
    2026-05-18 17:12:44,982 - INFO - EVCS oppstart-sync: faktisk strøm=0A
    2026-05-18 17:12:44,982 - INFO - EVCS: setter ladestrøm 16A (3.7 kW approx)
    2026-05-18 17:12:44,997 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:12:52,999 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:01,083 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:09,176 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:17,269 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:25,346 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:33,349 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:41,422 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:49,521 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:57,603 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:05,688 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:13,690 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:21,792 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:29,868 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:37,946 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:46,046 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:54,049 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:00,122 - INFO - Status: SOC=78.0% Grid=201.1W Sol=422.0W
    2026-05-18 17:15:02,124 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:10,216 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:18,374 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:26,444 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:34,446 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:42,531 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:50,631 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:58,713 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:06,818 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:14,820 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:22,910 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:30,995 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:39,076 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:47,191 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:55,194 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:03,268 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:11,349 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:19,438 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:27,521 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:35,523 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:43,604 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:51,705 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:59,784 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:07,867 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:15,869 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:23,973 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:32,050 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:40,133 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:48,238 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:56,242 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:04,325 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:12,415 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:20,511 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:28,598 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:36,600 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:44,675 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)

    2026-05-18 17:12:43,660 - INFO - Starting Energy Trader...
    2026-05-18 17:12:43,661 - INFO - Modbus-TCP connected to 192.168.1.60:502
    2026-05-18 17:12:43,661 - INFO - Connected via Modbus-TCP. Reading SOC...
    2026-05-18 17:12:44,665 - INFO - Startup-reset: reg37=0, Hub4Mode=3 (trader tar kontroll)
    2026-05-18 17:12:44,667 - INFO - ESS min SOC satt til 35%
    2026-05-18 17:12:44,667 - INFO - ESS min SOC: 35%  max SOC: 90%
    2026-05-18 17:12:44,667 - INFO - ==================================================
    2026-05-18 17:12:44,667 - INFO - Trade cycle 2026-05-18 17:12:44 CEST
    2026-05-18 17:12:44,670 - INFO - SOC: 78.1%
    2026-05-18 17:12:44,722 - INFO - Spotpris: 1.756 kr/kWh
    2026-05-18 17:12:44,723 - INFO - Sol: 0.48 kW
    2026-05-18 17:12:44,861 - INFO - Open-Meteo sol-prognose i morgen: 24.9 kWh (5.0 eff. timer)
    2026-05-18 17:12:44,861 - INFO - Dynamisk sol-reserve: 40.0% SOC (24.9 kWh prognose i morgen)
    2026-05-18 17:12:44,861 - INFO - Action: idle @ 0.0kW | 
    2026-05-18 17:12:44,863 - INFO - Idle — ESS styrer (Mode 3, setpoint=0)
    2026-05-18 17:12:44,864 - INFO - Dagens profitt: 0.00 kr
    2026-05-18 17:12:44,982 - INFO - EVCS oppstart-sync: faktisk strøm=0A
    2026-05-18 17:12:44,982 - INFO - EVCS: setter ladestrøm 16A (3.7 kW approx)
    2026-05-18 17:12:44,997 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:12:52,999 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:01,083 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:09,176 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:17,269 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:25,346 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:33,349 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:41,422 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:49,521 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:13:57,603 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:05,688 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:13,690 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:21,792 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:29,868 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:37,946 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:46,046 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:14:54,049 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:00,122 - INFO - Status: SOC=78.0% Grid=201.1W Sol=422.0W
    2026-05-18 17:15:02,124 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:10,216 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:18,374 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:26,444 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:34,446 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:42,531 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:50,631 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:15:58,713 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:06,818 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:14,820 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:22,910 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:30,995 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:39,076 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:47,191 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:16:55,194 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:03,268 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:11,349 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:19,438 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:27,521 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:35,523 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:43,604 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:51,705 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:17:59,784 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:07,867 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:15,869 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:23,973 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:32,050 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:40,133 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:48,238 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:18:56,242 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:04,325 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:12,415 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:20,511 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:28,598 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:36,600 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:44,675 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)
    2026-05-18 17:19:52,772 - INFO - ESS setpoint (reg37 VE.Bus): 0W (idle)

*Slutt på loggutdrag - kjør kommandoen over for live oppdatering*
