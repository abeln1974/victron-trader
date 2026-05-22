# Victron Energy Trader — Abelgård

Automatisk styring av Victron ESS med tre parallelle strategier: **self-consumption** (batteri dekker husets forbruk på dagtid), **arbitrasje** (kjøp billig natt, selg/bruk dyrt dag), og **peak-shaving** (hindre grid > 9.5 kW, Føie AS kapasitetstrinn).

## Anlegget

| Komponent | Detalj |
|-----------|--------|
| Cerbo GX | v3.72, IP 192.168.1.60, VRM site 411797 (Ethernet, kablet) |
| Invertere | 2× MultiPlus-II 48/5000/70-50 parallell |
| Batteri | 4× Receel 12kWh NMC = 45.6 kWh brutto (42.8 kWh netto), ekstern REC-BMS |
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
optimizer.py       — Optimal lade/utlade-plan + sol-reserve utlading
main.py            — Hovedloop: trading hvert 60min, peak-shave/self-consume hvert 10s, keepalive 8s
victron_modbus.py  — Modbus-TCP klient mot Cerbo GX (port 502)
ha_qubino.py       — Qubino 3-fase grid-måler + EVCS-koordinering via HA REST API
tariff.py          — Føie AS 2026 kapasitetstrinn, Norgespris-tak + Kraftriket priser
solar_forecast.py  — Sol-prognoser fra Open-Meteo (MET Norway MEPS 2.5 km), cachet 1t
profit_tracker.py  — SQLite-logging av handler og inntjening (netto arbitrasje-profitt)
web.py             — Flask dashboard (port 8080)
observe.py         — Diagnostikkverktøy: les alle Modbus-registre
```

## Styringsprinsipp

Trader eier Victron **alltid** via Mode 3 (ekstern kontroll). Prioritetsrekkefølge hvert 10s:

```
Peak-shaving  >  enforce_max_soc  >  self-consume
```

| Tilstand | Setpoint | Trigger |
|----------|----------|---------|
| **Oppstart** | 0W, DVCC frigjort (−1A) | `start()` kaller `set_max_charge_current(-1)` |
| **Natt-lading** | +kW | Billig natttariff, SOC < charge_target_soc |
| **Sol-reserve utlading** | −2 kW | SOC > charge_target_soc + 2%, dagtid, sol prognose tilsier det |
| **Arbitrasje utlading** | −kW | Spot høy nok (terskel MIN_PRICE_DIFF_NOK) |
| **Self-consumption** | −grid_kW | Dagtid, SOC > charge_target_soc + 1%, grid > 0.15 kW |
| **Peak-shaving** | −kW | Grid > 9.5 kW, hvert 10s |
| **Idle** | 0W | Ingen av over — keepalive holder Mode 3 |
| **Planlagt stopp** | Hub4Mode=2, DVCC=−1 | `release_control()` nullstiller setpoint og DVCC ved shutdown |
| **Krasj** | Passthru ~60s | Keepalive stopper → Victron tar Optimized etter timeout |

## Self-consumption

Ny funksjonalitet (mai 2026): batteriet dekker husets forbruk på dagtid i stedet for at strøm kjøpes fra nett.

**Logikk (`_check_self_consume()`, kjøres hvert 10s):**
- Aktiveres: dagtid (06–22), SOC > `charge_target_soc` + 1%, snitt-grid > 0.15 kW
- Setpoint = −(snitt_grid_kW) → batteriet leverer nøyaktig det huset trenger
- **Aldri eksport** — setpointet begrenses alltid til faktisk grid-forbruk
- Stopper: SOC ≤ `charge_target_soc` + 1%, natt, aktiv arbitrasje/peak-shave, eller snitt-grid < 0.10 kW
- Hysterese: starter ved >0.15 kW, stopper ikke før <0.10 kW (unngår av/på-jaging)
- Grid-snitt: rullende snitt av siste 3 avlesninger (~30s) for stabilitet

**Verdi:** Sparer kjøpspris dag (81 øre/kWh inkl. mva) for kWh levert fra batteri. Batteriet lades opp igjen gratis av sol.

## Sol-reserve logikk

Open-Meteo MEPS gir sol-prognose for i morgen. Systemet beregner:
```
charge_target_soc = max_soc - solar_reserve_pct
```
Eksempel: prognose 17 kWh → reserve 37% → `charge_target_soc = 53%`

`charge_target_soc` er dynamisk og brukes på fire steder:
1. **Natt-lading**: lader kun til `charge_target_soc` (ikke alltid til 90%)
2. **DVCC-grense**: DVCC=0A aktiveres ved SOC ≥ `charge_target_soc` — stopper Fronius fra å lade batteriet videre. Overstyrer **ikke** ESS-setpointet (self-consume/discharge kan fortsette).
3. **Sol-reserve utlading**: dagtid (06–22) utlades 2 kW sakte ned mot `charge_target_soc` hvis SOC er over målet
4. **Self-consume stopp**: self-consume deaktiveres når SOC ≤ `charge_target_soc` + 1%

**Storm mode:** Hvis prognose < 10 kWh → MIN_SOC heves fra 35% til 45% (30t nødstrøm), lader til 90%, ingen sol-reserve utlading.

## Peak-shaving

Føie AS 2026 kapasitetstrinn basert på gjennomsnitt av 3 høyeste timer på ulike dager:
- 5–9.99 kW: 418.8 kr/mnd ← **mål** (buffer 9.5 kW)
- 10–14.99 kW: 662.5 kr/mnd ← faktisk trinn april 2026 (avregnet 12.09 kW)

Besparelse trinn 4→3: **243.7 kr/mnd**

Grid-effekt overvåkes hvert 10s via Qubino (primær) eller VM-3P75CT (fallback). Ved >9.5 kW utlades batteriet med nøyaktig nødvendig effekt. Grid- og sol-verdier leses **én gang per 10s-syklus** og deles mellom peak-shave, enforce_max_soc og self-consume.

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
| 842 | Battery power (W) — **positiv=lading, negativ=utlading** | ❌ |

### Batterimåling (unit 226)
| Register | Beskrivelse | Scale |
|----------|-------------|-------|
| 266 | SOC % | ÷10 |
| 259 | Voltage (V) | ÷100 |
| 261 | Current (A) | ÷10 |
| 309 | Discharged energy (kWh) | ÷10 |
| 310 | Charged energy (kWh) | ÷10 |

> ✅ Reg 842: positiv=lading, negativ=utlading (verifisert mot VRM 2026-05-22).

> 📄 Full register-liste: `/home/lars/Nedlastinger/CCGX-Modbus-TCP-register-list-3.71.xlsx`

## NMC-batteri konfig

NMC-kjemi degraderer ved langvarig høy SOC. Konfigurert for lang levetid:
- **Min SOC**: 35% normal / 45% storm mode
- **Max SOC**: 90% (DVCC=0A stopper aktiv lading over `charge_target_soc`)
- **Brukbar kapasitet** (35–90% SOC): ~24.9 kWh av 45.6 kWh brutto
- **Batterislitasje**: ~1.00 kr/kWh (60 000 kr ÷ 2000 sykler × 30 kWh)
- **Absorption**: 57.20V (3.575V/celle)
- **Float**: 56.80V (3.55V/celle)
- **Repeated absorption**: hvert 30. døgn

> ✅ DVCC reg 2705 = 0A fungerer for AC-koblet Fronius Primo på dette systemet (2× MultiPlus-II parallell, verifisert 2026-05-22). MultiPlus reduserer absorption-kapasiteten slik at Fronius-overskudd eksporteres til nett i stedet for å gå inn i batteriet.

## Data og lagring

- **Database**: `/opt/victron-trader/data/profit.db` (SQLite, permanent)
  - Tabell `trades`: alle handler med pris, kWh og netto profitt
  - Tabell `daily_plan`: optimizer-plan per syklus (sol-prognose, reserve%, lademål, SOC) — for etteranalyse
- **State**: `/opt/victron-trader/data/trader_state.json` (overlever restart)
- **Logs**: `/opt/victron-trader/logs/trader.log` (RotatingFileHandler, 5MB × 7 filer ≈ 1 uke)
- Docker-compose mounter `/opt/victron-trader/data` og `/opt/victron-trader/logs` — overlever alle rebuilds

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

Åpne http://localhost:8080 — moderne Tailwind-dashboard med:
- **Energiflyt live**: Grid ↔ Hus ↔ Sol med animerte piler (retning og styrke)
- **Batteri**: visuell SOC-indikator med fargeskift (grønn/gul/rød), lademål
- **Stat-kort**: spot nå, sol nå/prognose i morgen, profitt i dag, arbitrasje-margin
- **Spotpris 24t**: søylediagram med nåværende time uthevet
- **Sol-profil i dag**: time-for-time prognose fra Open-Meteo
- **Optimizer-plan**: neste 24t med action-type, grunn og forventet profitt
- **Sol-analyse**: siste daily_plan-sykluser (sol kWh, reserve%, lademål, SOC)
- **Systemkonfig**: SOC-grenser, spread, kapasitetsavgift

### Diagnostikk

```bash
# Sjekk container-status
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}'

# Siste logg
docker logs victron-trader --tail 50

# Sjekk trading-aktivitet (arbitrasje, sol-reserve, self-consume)
docker logs victron-trader 2>&1 | grep -E "Trade cycle|Action:|Self-consume|Peak-shave|Export-guard|SOC="

# Følg live logg
docker logs victron-trader -f

# Les alle Modbus-registre fra Cerbo GX
docker compose run --rm victron-trader python observe.py

# Sjekk database
docker exec victron-trader python3 -c "
import sqlite3; conn = sqlite3.connect('/app/data/profit.db')
rows = conn.execute('SELECT * FROM trades ORDER BY timestamp DESC LIMIT 10').fetchall()
for r in rows: print(r)
"
```

## Grid-måler analyse

`grid_analysis.py` er et diagnostikkverktøy for å sammenligne VM-3P75CT (Victron Modbus, rask ~1s, mangler L3) og Qubino ZMNHXD (HA Z-Wave, alle 3 faser, ~10s oppdatering):

```bash
# 30 raske målinger — statistisk sammenligning
docker exec victron-trader python3 /app/grid_analysis.py sample

# 5 minutters live-logging → /tmp/grid_compare.csv
docker exec victron-trader python3 /app/grid_analysis.py live 300
```

**Konklusjon (verifisert 2026-05-22):**
- Qubino oppdaterer hvert ~10s etter P40=1%, P42=10s ble satt
- L3-bidrag er stabilt +32W — Victron L1+L2 (live) + Qubino L3 (offset) gir best nøyaktighet
- `HA_MIN_INTERVAL` satt til 10s for å matche Qubino poll-frekvens

### Qubino Z-Wave parametere (anbefalt)
| Param | Navn | Verdi |
|-------|------|-------|
| P40 | Reporting on Power Change | 1% |
| P42 | Reporting on Time Interval | 10s |
| P43 | Other Values Time Interval | 60s |

## VRM / ESS-konfig

Konfig som er verifisert og anbefalt for dette systemet:
- **ESS Mode**: `Optimized without BatteryLife` — Victron tar tilbake kontroll etter 60s keepalive-timeout ved crash
- **Dynamic ESS**: **deaktivert** — vil krige med trader om setpoint
- **Grid feed-in**: AC-coupled PV feed in excess = ON, ingen limit
- **Multiphase regulation**: Individual phase (IT-nett)
- **Grid setpoint**: 0W (nøytral når trader ikke kjører)

## Miljøvariabler

| Variabel | Standard | Beskrivelse |
|----------|---------|-------------|
| VICTRON_HOST | 192.168.1.60 | Cerbo GX IP |
| BATTERY_CAPACITY_KWH | 45.6 | Batterikapasitet kWh |
| MIN_SOC | 35 | Minimum SOC % (normal) |
| MAX_SOC | 90 | Maksimum SOC % |
| STORM_MODE_MIN_SOC | 45 | MIN_SOC ved storm mode |
| STORM_MODE_THRESHOLD_KWH | 10.0 | Sol-prognose under denne → storm mode aktiveres |
| BATTERY_MAX_CHARGE_KW | 10 | Maks ladefart kW |
| BATTERY_MAX_DISCHARGE_KW | 10 | Maks utladefart kW |
| PEAK_LIMIT_KW | 9.5 | Peak-shaving grense kW |
| MIN_PRICE_DIFF_NOK | 1.10 | Min spread for arbitrasje inkl. batterislitasje. Med Norgespris-tak kreves spot > ~2.26 kr/kWh |
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
| HA_MIN_INTERVAL | 10.0 | Minimum sekunder mellom HA-kall (matcher Qubino P42) |
| DB_PATH | /app/data/profit.db | Database-sti |
| READONLY_MODE | false | true = ingen skriving til Cerbo |

## Lisens

AGPL-3.0 — se LICENSE

## Gitea / GitHub

- Primær: https://gitea.abelgaard.no/lars/victron-trader
- Mirror: https://github.com/abeln1874/victron-trader
