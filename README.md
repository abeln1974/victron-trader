# Victron Energy Trader — Abelgård

Automatisk strømhandel og peak-shaving med Victron ESS. Kjøper strøm billig (natt), selger/bruker fra batteri når strøm er dyr (dag), og hindrer at grid-effekt overstiger 10 kW (Elvia kapasitetstrinn).

## Anlegget

| Komponent | Detalj |
|-----------|--------|
| Cerbo GX | v3.72, IP 192.168.1.60, VRM site 411797 |
| Invertere | 2× MultiPlus-II 48/5000/70-50 parallell |
| Batteri | 4× 12kWh NMC = 48kWh, ekstern BMS |
| Sol | Fronius Primo 5kW (AC-koblet, AC output) |
| Grid-måler | Qubino ZMNHXD 3-fase (primær, via HA) |
| Grid-måler fallback | VM-3P75CT via Victron Modbus (mangler L3 på IT-nett) |
| Home Assistant | RPi5, 192.168.1.34, https://homeassistant.abelgaard.no |
| Strømleverandør | Kraftriket (kraftriket.no) |
| Nettleie | Elvia 2026, prisområde NO1 |

## Arkitektur

```
price_fetcher.py   — Spotpriser fra hvakosterstrommen.no (NO1)
optimizer.py       — Optimal lade/utlade-plan + peak-shaving logikk
main.py            — Hovedloop: trading hvert 60min, peak-shaving hvert 10s
victron_modbus.py  — Modbus-TCP klient mot Cerbo GX (port 502)
ha_qubino.py       — Qubino 3-fase grid-måler via Home Assistant REST API
tariff.py          — Elvia 2026 kapasitetstrinn + Kraftriket priser
profit_tracker.py  — SQLite-logging av handler og inntjening
web.py             — Flask dashboard (port 8080)
observe.py         — Diagnostikkverktøy: les alle Modbus-registre
```

## Viktige Modbus-registre (CCGX register-list 3.71)

### Unit ID mapping
| Unit | Service | Beskrivelse |
|------|---------|-------------|
| 100 | com.victronenergy.system | Alle system/hub4/settings-registre |
| 226 | com.victronenergy.battery | SmartShunt 500A (SOC, strøm, spenning) |
| 227 | com.victronenergy.vebus | MultiPlus-II parallell |

### ESS-styring (unit 100)
| Register | Beskrivelse | Skrivbar |
|----------|-------------|---------|
| 2716/2717 | AC grid setpoint 32-bit (W, hub4, volatile) | ✅ |
| 2901 | ESS Minimum SoC (scale ×10, 200=20%) | ✅ |
| 2902 | ESS Mode: 1=Opt+BL, 2=Optimized, 3=KeepCharged, 4=ExternalControl | ✅ |

### Batterimåling (unit 226)
| Register | Beskrivelse | Scale |
|----------|-------------|-------|
| 266 | SOC % | ÷10 |
| 258 | Battery power (W) | ×1 |
| 259 | Voltage (V) | ÷100 |
| 261 | Current (A) | ÷10 |

### Grid/PV (unit 100)
| Register | Beskrivelse |
|----------|-------------|
| 820/821/822 | Grid L1/L2/L3 power (W) — L3=0 på IT-nett med VM-3P75CT |
| 808 | AC-coupled PV L1 (Fronius Primo, W) |

> 📄 Full register-liste: `/home/lars/Nedlastinger/CCGX-Modbus-TCP-register-list-3.71.xlsx`

## NMC-batteri konfig

NMC-kjemi degraderer ved langvarig høy SOC. Konfigurert for lang levetid:
- **ESS modus**: `Optimized without BatteryLife` (modus 2) — ESS styrer aktivt ned fra 100%
- **Min SOC**: 20% (satt via reg 2901 ved oppstart)
- **Max SOC**: 90% (optimizer lader ikke over 90%)
- **Dynamic ESS**: **Deaktivert** (VRM → Settings → Dynamic ESS → Off)

## Peak-shaving

Elvia 2026 kapasitetstrinn basert på gjennomsnitt av 3 høyeste timer på ulike dager:
- Under 5 kW: 195 kr/mnd
- 5–9.99 kW: 419 kr/mnd  ← vi holder oss her (buffer 9.5 kW)
- 10–14.99 kW: 643 kr/mnd
- 15–19.99 kW: 867 kr/mnd

Grid-effekt overvåkes hvert 10. sekund via Qubino (primær) eller VM-3P75CT (fallback). Ved >9.5 kW utlades batteriet automatisk.

## ESS keepalive

Victron nullstiller setpoint etter 60s uten skriving. Koden sender keepalive hvert 30s når aktiv handling pågår (lading/utlading).

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

# Lag .env med tokens
cat > .env << EOF
HA_TOKEN=<ditt HA long-lived token>
HA_URL=https://homeassistant.abelgaard.no
EOF

docker compose up -d
docker compose logs -f
```

### Dashboard

Åpne http://localhost:8080 — viser live SOC, grid (alle 3 faser), sol, priser og handelsplan.

### Diagnostikk

```bash
# Sjekk Qubino grid-måler
docker compose run --rm victron-trader python ha_qubino.py

# Les alle Modbus-registre fra Cerbo GX
docker compose run --rm victron-trader python observe.py

# Kjør optimizer manuelt
docker compose run --rm victron-trader python optimizer.py
```

## Miljøvariabler

| Variabel | Standard | Beskrivelse |
|----------|---------|-------------|
| VICTRON_HOST | 192.168.1.60 | Cerbo GX IP |
| BATTERY_CAPACITY_KWH | 48 | Batterikapasitet |
| MIN_SOC | 20 | Minimum SOC % (NMC) |
| MAX_SOC | 90 | Maksimum SOC % (NMC) |
| BATTERY_MAX_CHARGE_KW | 10 | Maks ladefart kW |
| BATTERY_MAX_DISCHARGE_KW | 10 | Maks utladefart kW |
| PRICE_AREA | NO1 | Prisområde |
| HA_URL | https://homeassistant.abelgaard.no | Home Assistant URL |
| HA_TOKEN | — | HA long-lived access token |
| HA_MIN_INTERVAL | 30.0 | Min sekunder mellom HA-kall |
| READONLY_MODE | false | true = ingen skriving til Cerbo |

## Gitea

Repo: https://gitea.abelgaard.no/lars/victron-trader

```bash
git add .
git commit -m "beskrivelse"
git push origin master
```
