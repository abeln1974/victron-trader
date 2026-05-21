# Victron Energy Trader — Abelgård

Automatisk strømhandel og peak-shaving med Victron ESS. Kjøper strøm billig (natt), selger/bruker fra batteri når strøm er dyr (dag), og hindrer at grid-effekt overstiger 10 kW (Føie AS kapasitetstrinn).

## Anlegget

| Komponent | Detalj |
|-----------|--------|
| Cerbo GX | v3.72, IP 192.168.1.60, VRM site 411797 (Ethernet, kablet) |
| Invertere | 2× MultiPlus-II 48/5000/70-50 parallell |
| Batteri | 4× Receel 12kWh NMC = 45.6 kWh netto (42.8 kWh brutto), ekstern REC-BMS |
| Sol | Fronius Primo 5kW (AC-koblet på AC-out — **ikke** DC-bussen) |
| Grid-måler | Qubino ZMNHXD 3-fase (primær, via HA) — sensor `_kwh_3` og `_w_6` |
| Grid-måler fallback | VM-3P75CT via Victron Modbus (mangler L3 på IT-nett) |
| Home Assistant | RPi5, 192.168.1.34, https://homeassistant.abelgaard.no |
| EVCS | HQ2309VTVNF, 1-fase, 6–16A via HA |
| Strømleverandør | Kraftriket Solstrøm (kraftriket.no) |
| Nettleie | Føie AS 2026, prisområde NO1 |

## Arkitektur

```
price_fetcher.py   — Spotpriser fra hvakosterstrommen.no (NO1)
optimizer.py       — Optimal lade/utlade-plan + kveldsutlading for sol-reserve
main.py            — Hovedloop: trading hvert 60min, peak-shaving hvert 10s, keepalive 8s
victron_modbus.py  — Modbus-TCP klient mot Cerbo GX (port 502)
ha_qubino.py       — Qubino 3-fase grid-måler + EVCS-koordinering via HA REST API
tariff.py          — Føie AS 2026 kapasitetstrinn, Norgespris-tak + Kraftriket priser
solar_forecast.py  — Sol-prognoser fra Open-Meteo (MET Norway MEPS 2.5 km), cachet 1t
profit_tracker.py  — SQLite-logging av handler og inntjening (netto arbitrasje-profitt)
web.py             — Flask dashboard (port 8080)
observe.py         — Diagnostikkverktøy: les alle Modbus-registre
```

## Styringsprinsipp

Trader eier Victron **alltid** via Mode 3 (ekstern kontroll):

- **Oppstart** → Hub4Mode=3, setpoint=0, DVCC frigjort
- **Idle** → setpoint=0, keepalive 8s (Mode 3 holdes)
- **Natt-lading** → setpoint=+kW, lader til `90% - sol_reserve_pct`
- **Kveldsutlading** → utlader ned til lademål kl 20-22 for å gjøre plass til sol
- **Dag-utlading** → setpoint=-kW ved lønnsom spot (terskkel 1.10 kr)
- **Peak-shaving** → utlader ved grid > 9.5 kW, hvert 10s
- **SOC ≥ 90%** → DVCC=0A (stopper Fronius-overskudd fra å lade batteriet), eksport til nett
- **SOC < 89%** → DVCC frigjøres (-1A = ingen grense)
- **Planlagt stopp** → `release_control()` → Hub4Mode=2, Victron overtar
- **Krasj** → keepalive stopper → Passthru ~10s → Victron tar Mode 2 automatisk

## Sol-reserve logikk

Open-Meteo MEPS gir sol-prognose for i morgen. Systemet beregner:
```
charge_target_soc = max_soc - solar_reserve_pct
```
Eksempel: prognose 11 kWh → reserve 24.6% → lademål 65.4% SOC

**Kveldsutlading (kl 20-22):** Hvis SOC > lademål + 2% → utlad ned til lademål. Sol fyller opp neste dag.

**Storm mode:** Hvis prognose < 10 kWh → MIN_SOC heves fra 35% til 45% (30t nødstrøm), lades til 90%.

## Peak-shaving

Føie AS 2026 kapasitetstrinn basert på gjennomsnitt av 3 høyeste timer på ulike dager:
- 5–9.99 kW: 418.8 kr/mnd ← **mål** (buffer 9.5 kW)
- 10–14.99 kW: 662.5 kr/mnd ← faktisk trinn april 2026 (avregnet 12.09 kW)

Besparelse trinn 4→3: **243.7 kr/mnd**

Grid-effekt overvåkes hvert 10s. Ved >9.5 kW utlades batteriet automatisk.

## Viktige Modbus-registre (CCGX register-list 3.71)

### Unit ID mapping
| Unit | Service | Beskrivelse |
|------|---------|-------------|
| 100 | com.victronenergy.system | System/hub4/settings-registre |
| 226 | com.victronenergy.battery | SmartShunt 500A (SOC, strøm, spenning) |
| 227 | com.victronenergy.vebus | MultiPlus-II parallell |

### ESS-styring (unit 100)
| Register | Beskrivelse | Skrivbar |
|----------|-------------|---------|
| 37 | VE.Bus ESS setpoint (W, volatile, timeout ~10s) | ✅ |
| 2705 | DVCC max charge current (A, -1=ingen grense, 0=stopp) | ✅ |
| 2901 | ESS Minimum SoC (scale ×10, 200=20%) | ✅ |
| 2902 | Hub4Mode: 2=Optimized, 3=ExternalControl | ✅ |
| 842 | Battery power (W) — **negativ=lading på dette systemet** | ❌ |

### Batterimåling (unit 226)
| Register | Beskrivelse | Scale |
|----------|-------------|-------|
| 266 | SOC % | ÷10 |
| 259 | Voltage (V) | ÷100 |
| 261 | Current (A) | ÷10 |
| 309 | Discharged energy (kWh) | ÷10 |
| 310 | Charged energy (kWh) | ÷10 |

> ⚠️ Reg 842 returnerer negativ verdi ved lading på dette systemet (2× MultiPlus parallell) — inverteres i koden.

> 📄 Full register-liste: `/home/lars/Nedlastinger/CCGX-Modbus-TCP-register-list-3.71.xlsx`

## NMC-batteri konfig

NMC-kjemi degraderer ved langvarig høy SOC. Konfigurert for lang levetid:
- **Min SOC**: 35% normal / 45% storm mode
- **Max SOC**: 90% (DVCC=0A stopper lading over 90%)
- **Absorption**: 57.20V (3.575V/celle)
- **Float**: 56.80V (3.55V/celle)
- **Repeated absorption**: hvert 30. døgn

## Data og lagring

- **Database**: `/opt/victron-trader/data/profit.db` (SQLite, permanent)
- **State**: `/opt/victron-trader/data/trader_state.json` (overlever restart)
- **Logs**: `/opt/victron-trader/logs/`
- Docker-compose mounter `/opt/victron-trader/data` — overlever alle rebuilds

## Oppsett

### Krav
- Docker + Docker Compose
- Cerbo GX med Modbus-TCP aktivert: `Settings → Services → Modbus-TCP → Enabled`
- Dynamic ESS **deaktivert** i VRM
- Home Assistant med Qubino Z-Wave entiteter og gyldig long-lived token

### Start

```bash
git clone https://gitea.abelgaard.no/lars/victron-trader.git
cd victron-trader

cp .env.example .env
# Rediger .env med HA_TOKEN

docker compose up -d
docker compose logs -f
```

### Dashboard

Åpne http://localhost:8080 — viser live SOC, grid (alle 3 faser), sol, priser, handelsplan og handelshistorikk.

### Diagnostikk

```bash
# Sjekk container-status
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}'

# Siste logg
docker logs victron-trader --tail 50

# Sjekk trading-aktivitet
docker logs victron-trader 2>&1 | grep -E "Trade cycle|Action:|charge|discharge|SOC="

# Les alle Modbus-registre fra Cerbo GX
docker compose run --rm victron-trader python observe.py

# Sjekk database
docker exec victron-trader python3 -c "
import sqlite3; conn = sqlite3.connect('/app/data/profit.db')
rows = conn.execute('SELECT * FROM trades ORDER BY timestamp DESC LIMIT 10').fetchall()
for r in rows: print(r)
"
```

## Miljøvariabler

| Variabel | Standard | Beskrivelse |
|----------|---------|-------------|
| VICTRON_HOST | 192.168.1.60 | Cerbo GX IP |
| BATTERY_CAPACITY_KWH | 45.6 | Batterikapasitet kWh |
| MIN_SOC | 35 | Minimum SOC % (normal) |
| MAX_SOC | 90 | Maksimum SOC % |
| STORM_MODE_MIN_SOC | 45 | MIN_SOC ved storm mode |
| STORM_MODE_THRESHOLD_KWH | 10.0 | Sol-grense for storm mode (kWh) |
| BATTERY_MAX_CHARGE_KW | 10 | Maks ladefart kW |
| BATTERY_MAX_DISCHARGE_KW | 10 | Maks utladefart kW |
| PEAK_LIMIT_KW | 9.5 | Peak-shaving grense kW |
| MIN_PRICE_DIFF_NOK | 1.10 | Min spread for arbitrasje (inkl. 1.00 kr batterislitasje) |
| PRICE_AREA | NO1 | Prisområde |
| SITE_LAT | 60.14 | Breddegrad (Ringerike) |
| SITE_LON | 10.25 | Lengdegrad (Ringerike) |
| SOLAR_MAX_KW | 5.0 | Fronius Primo maks effekt |
| SOLAR_SYSTEM_EFFICIENCY | 0.85 | Sol-system virkningsgrad |
| SOLAR_EFFECTIVE_HOURS | 4.0 | Fallback sol-timer/dag |
| EVCS_ENTITY_PREFIX | evcs_hq2309vtvnf | HA entity prefix for EVCS |
| EVCS_PHASES | 1 | Antall faser elbil-lader |
| EVCS_MIN_CURRENT_A | 6 | Min ladestrøm EVCS (A) |
| EVCS_MAX_CURRENT_A | 16 | Max ladestrøm EVCS (A) |
| HA_URL | https://homeassistant.abelgaard.no | Home Assistant URL |
| HA_TOKEN | — | HA long-lived access token |
| DB_PATH | /app/data/profit.db | Database-sti |
| READONLY_MODE | false | true = ingen skriving til Cerbo |

## Lisens

AGPL-3.0 — se LICENSE

## Gitea / GitHub

- Primær: https://gitea.abelgaard.no/lars/victron-trader
- Mirror: https://github.com/abeln1874/victron-trader
