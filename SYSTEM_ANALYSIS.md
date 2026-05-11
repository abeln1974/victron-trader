# Victron Energy Trader — Systemanalyse

> Sist oppdatert: 2026-05-11
> Repository: `gitea.abelgaard.no/lars/victron-trader` (branch: master)
> Installasjon: Abelgård, Ringerike — NO1 prisområde

---

## 1. Systemarkitektur

```
Internett
    │
    ▼
Spotpris API (hvakosterstrommen.no)
    │
    ▼
┌─────────────────────────────────────────────┐
│  Docker Compose (samme host)                │
│                                             │
│  victron-trader (main.py)                   │
│    - Trading-loop hver time                 │
│    - Peak-shaving hvert 10s                 │
│    - EVCS-koordinering hvert 10s            │
│    - ESS keepalive hvert 3s                 │
│                                             │
│  victron-web (web.py :8080)                 │
│    - Dashboard + REST API                   │
│    - Deler SQLite-volum med trader          │
└─────────────────────────────────────────────┘
    │                          │
    ▼ Modbus-TCP :502           ▼ HTTPS REST
Cerbo GX (192.168.1.60)    Home Assistant
    │                          │
    ▼                          ├─ Qubino ZMNHXD (3-fase smartmåler)
MultiPlus-II 48/5000 ×2        └─ EVCS HQ2309VTVNF (elbil-lader)
    │
    ├─ Batteri: 4×12.5kWh NMC (45.6 kWh brutto per SmartShunt 800Ah×57V)
    └─ Fronius Primo 5kW (AC-koblet sol)
```

---

## 2. Batteri og anlegg

| Parameter | Verdi | Kilde |
|---|---|---|
| Kapasitet (brutto) | 45.6 kWh | SmartShunt: 800 Ah × 57 V |
| Kapasitet (brutto oppgitt) | 4 × 12.5 kWh = 50 kWh | Victron spec |
| Max lading | 10 kW | 2× MultiPlus-II 48/5000 |
| Max utlading | 10 kW | 2× MultiPlus-II 48/5000 |
| Min SOC (kode) | 20 % | NMC konservativ (Victron floor: 10 %) |
| Max SOC (kode) | 90 % | NMC levetid |
| Brukbar kapasitet (20–90 %) | 31.9 kWh | 45.6 × 0.70 |
| Peak-reserve | 5 kWh | Alltid tilgjengelig for peak-shaving |
| Virkningsgrad | 0.95 | Round-trip |
| Sol | 5 kW Fronius Primo | AC-koblet |

### ESS-styring via Modbus-TCP
- **Hub4Mode = 2** (Optimized without BatteryLife) — normalstand
- **Hub4Mode = 3** (ESS Control Disabled) — aktiv trading/utlading
- **Reg37 / unit227** — grid setpoint (W, signed16, negativ = eksport)
- Keepalive: reg37 må skrives hvert < 10s i Mode 3, ellers nullstilles av MultiPlus
- Startup-reset: Hub4Mode=2 og reg37=0 alltid ved oppstart (krasj-sikring)

---

## 3. Prisstruktur (Føie AS / Kraftriket, april 2026)

### Kjøpspris (eks mva, deretter ×1.25)
```
Spotpris (variabel, eks mva)
+ Kraftriket påslag:    6.50 øre/kWh
+ Nettleie dag (06–22): 16.50 øre/kWh  → 20.63 inkl mva
+ Nettleie natt (22–06):10.00 øre/kWh  → 12.50 inkl mva
+ Forbruksavgift:        7.13 øre/kWh  → 8.91 inkl mva
+ Enova:                 1.00 øre/kWh  → 1.25 inkl mva
× 1.25 mva
− Norgespris:           96.53 øre/kWh  (ingen mva, statlig støtte)
= Total reell kjøpspris
```

### Salgspris (plusskunde, ingen mva)
```
Kraftriket betaler:  75.00 øre/kWh
− Føie tilbakebetaling: 6.25 øre/kWh
= Netto salgspris:   68.75 øre/kWh
```

### Kapasitetsledd (Føie AS 2026, inkl mva)
| Trinn | kW-grense | kr/mnd | Merknad |
|---|---|---|---|
| 1 | 0–1.99 kW | 237.5 | |
| 2 | 2–4.99 kW | 293.8 | |
| 3 | 5–9.99 kW | 418.8 | **MÅL: hold her** |
| 4 | 10–14.99 kW | 662.5 | Faktisk trinn apr 2026 (12.09 kW) |
| 5 | 15–19.99 kW | 837.5 | |
| 6 | 20–24.99 kW | 1075.0 | |
| 7 | 25–49.99 kW | 1437.5 | |

- **Beregning:** snitt av de 3 høyeste timer på ULIKE dager per mnd
- **Peak-shaving grense:** 9.5 kW (0.5 kW buffer til 10 kW-trinnet)
- **Besparelse Trinn 4→3:** 662.5 − 418.8 = **243.7 kr/mnd**

> **Merk — Norgespris i trading-beslutninger:**
> `should_discharge()` ekskluderer Norgespris fra spread-beregningen (korrekt).
> `buy_price_ore()` inkluderer Norgespris-fradraget (brukes til dashboard-visning).
> Dashboardet kan derfor vise negativ "reell kjøpspris" og likevel trigge utlading —
> dette er tilsiktet og matematisk riktig.

---

## 4. Optimaliseringsstrategi

### 4.1 Utlading (salg)
1. Hent priser for neste 24 timer
2. Beregn **råkjøpspris** for hver time (spot + alle avgifter eks Norgespris)
3. Sorter etter høyeste råkjøpspris, filtrer på median-terskel
4. Tildel batterikapasitet til topp-timene inntil batteri er tomt (20% min SOC − 5 kWh reserve)
5. **Resultat:** plan selger i de mest lønnsomme timene

**Lønnsomhetskrav utlading:**
```
råkjøpspris > salgspris × efficiency
(spot eks mva + avgifter) × mva > 68.75 × 0.95 = 65.3 øre
```

### 4.2 Natt-lading
1. Etter utladingsplan er beregnet: estimér SOC etter planlagt utlading
2. Finn billigste nattetimer (22–06) under lønnsomhetsterskel
3. Tildel ladekapasitet opp til 90% SOC
4. **Cap ladeeffekt mot peak-limit** — se seksjon 6.4

**Lønnsomhetskrav lading:**
```
kjøpspris < salgspris × efficiency
kjøpspris < 65.3 øre
```

### 4.3 Peak-shaving (kontinuerlig, hvert 10s)
- Hvis grid > 9.5 kW → utlad fra batteri med `excess_kw`
- Krever min 5 kWh reserve i batteriet
- Prioritet over trading (overstyrer setpoint midlertidig)

---

## 5. EVCS Elbil-lader koordinering

### Anlegg
- Victron EVCS (HQ2309VTVNF), **1-fase** (`EVCS_PHASES=1`), AC-input (grid-siden)
- Min ladestrøm: 6A, Max konfigurerbar: 16A (= 3.7 kW på 1 fase)
- 2 Polestarer lader hjemme: Polestar 2 (2022) og Polestar 4 (2025)
- Styres via Home Assistant REST API

### Prioritetsregler (hvert 10s)

| Situasjon | EVCS-handling | Begrunnelse |
|---|---|---|
| Batteri selger (discharge) | **Stopp helt** | EVCS er på grid-siden — ville spise opp eksport lokalt |
| Dag + sol-overskudd | Lad med `surplus_kw / 230V` A | Bruk gratis solstrøm |
| Natt / idle | Lad med `(9.5 − grid_uten_EVCS) kW` | Respekter peak-limit |
| Ikke nok kapasitet | Stopp | Peak-limit prioriteres |

### Kapasitetsberegning
```python
grid_without_evcs = grid_kw - evcs_kw          # Faktisk forbruk uten EVCS
available_kw = peak_limit(9.5) - grid_without_evcs
amps = int(available_kw * 1000 / (phases * 230))
amps = clamp(amps, min=6, max=16)
```

---

## 6. Sikkerhetsfunksjoner

### 6.1 Export-guard (keepalive-loop, hvert 3s)
- Under utlading: mål grid hvert 3s
- Hvis `grid_w > −(discharge_w × 0.6)` → lokalt forbruk spiser for mye → **stopp utlading**
- Eksempel: 10 kW utlading → grense = −6000W. Hvis grid > −6000W → stopp
- Forhindrer at batteriet tapper seg uten å faktisk selge til nett

### 6.2 Action-time guard
- Keepalive sendes **kun** hvis `action.timestamp.hour == now.hour`
- Forhindrer at gammel action holder seg aktiv etter time-skifte
- Ved time-skifte: logg faktisk kWh (SOC-endring × kapasitet), stopp ESS

### 6.3 Krasj-sikring
- Startup-reset: alltid Hub4Mode=2 + reg37=0 ved oppstart
- Docker `restart: unless-stopped` → gjenoppstart innen sekunder
- MultiPlus hardware-timeout: nullstiller reg37 automatisk etter ~10s uten keepalive
- `stop()` ved SIGTERM/SIGINT: rydder opp Hub4Mode og EVCS restore_auto()

### 6.4 Peak-limit cap ved lading (main.py `_execute_action`)
Lading kan overskride peak-grensen hvis husforbruket er høyt.
`_execute_action` leser live grid-effekt og capper ladeeffekten:
```python
headroom_kw = max(0, peak_limit_kw - max(0, grid_kw))
charge_kw = min(action.power_kw, headroom_kw)
```
- Hvis headroom < 0.5 kW → lading blokkeres helt
- Logg viser cappet effekt og begrunnelse

`optimizer.py` bruker også et statisk estimat (1.5 kW typisk nattforbruk) for å planlegge
konservativt fremover, men `_execute_action` bruker alltid live grid for faktisk cap.

---

## 7. Kjente scenarioer

### Scenario: Batteri selger kl 19, elbil plugger inn kl 21

```
kl 19:00  discharge 10kW — EVCS stoppes umiddelbart
kl 20:00  discharge 10kW — EVCS forblir stoppet
kl 21:00  discharge 6kW  — EVCS forblir stoppet
kl 22:00  idle           — EVCS: available = 9.5 − husforbruk → lader med ~8A
kl 02:00  charge 8kW    — live grid=1.5kW → headroom=8kW → lader 8kW (ikke 10kW)
kl 03:00  idle (SOC 90%) — EVCS: available = 9.5 − 1.5 = 8.0 kW → 16A = 3.7 kW
```

### Scenario: Sol-dag, elbil tilkoblet

```
Sol: 4 kW, husforbruk: 1 kW, batteri idle
grid_without_evcs = −3 kW (eksporterer)
surplus_kw = 4 − (−3) = 7 kW → for mye
available_kw = 9.5 − (−3) = 12.5 kW
amps = min(7000/230, 16) = 16A → 3.7 kW til elbil
```

### Scenario: Natt-lading med høyt husforbruk (peak-cap)

```
Husforbruk: 3.0 kW (gulvvarme + kjøl/frys)
Peak-limit: 9.5 kW → headroom = 6.5 kW
Planlagt lading: 10 kW
Faktisk lading: 6.5 kW (cappet av _execute_action)
Peak-grensen overholdes.
```

---

## 8. Kjente svakheter / forbedringspotensial

### 🟡 Medium prioritet
1. **Negativ reell kjøpspris trigger ikke alltid lading** — ved høy Norgespris-støtte kan
   reell kjøpspris bli negativ. `should_charge()` er profittpris-basert og lader ikke
   aggressivt nok i disse timene. Vurder å legge til spesialhåndtering.
2. **Ingen re-planlegging intratime** — hvis priser publiseres kl 13 oppdateres ikke planen
   før neste time-syklus. Bør trigge re-plan ved prisoppdatering.
3. **SOC-basert kWh-logging er approx** — `actual_kwh = capacity × delta_soc / 100` antar
   lineær SOC. Bør hente direkte fra SmartShunt via Modbus for nøyaktighet.
4. **EVCS støtter kun én lader** — to separate ladere håndteres ikke.

### 🟢 Lav prioritet
5. **Dashboard viser ikke EVCS-status** — `web.py` har ingen EVCS-widget.
6. **Ingen alarm ved Qubino Z-Wave "dead"** — koden logger warning men sender ikke varsel.
7. **Profitt-dashboard viser kun dagens handler** — ingen ukes/måneds-graf.

---

## 9. Filstruktur

| Fil | Ansvar |
|---|---|
| `main.py` | Trading-loop, peak-shaving, EVCS-koordinering, keepalive, peak-cap ved lading |
| `optimizer.py` | 24t-plan, discharge/charge-valg, peak_shave(), statisk peak-cap estimat |
| `tariff.py` | Prisberegning, kapasitetstrinn (autoritativ kilde), should_discharge/charge |
| `config.py` | Alle konfig-parametre via env-variabler |
| `victron_modbus.py` | Modbus-TCP kommunikasjon med Cerbo GX |
| `ha_qubino.py` | Qubino grid-måling + EVCSController via HA REST |
| `price_fetcher.py` | Spotpriser fra hvakosterstrommen.no |
| `profit_tracker.py` | SQLite trade-logging og statistikk |
| `web.py` | Flask dashboard + REST API |

> **Merk for Windsurf/AI-assistenter:**
> - Kapasitetstrinn er definert KUN i `tariff.py` (`CAPACITY_TIERS`) — ikke dupliser i andre filer
> - Live grid-cap skjer i `main.py._execute_action()`, ikke i `optimizer.py`
> - `optimizer.py` bruker statisk estimat (1.5 kW) for fremtidsplanlegging — dette er tilsiktet
> - `_decide_action_tariff()` er fjernet (var dead code) — bruk `get_immediate_action()` → `optimize()`

---

## 10. Konfigurasjon (miljøvariabler)

```env
# Victron
VICTRON_HOST=192.168.1.60
VICTRON_MODBUS_PORT=502

# Batteri
BATTERY_CAPACITY_KWH=45.6
BATTERY_MAX_CHARGE_KW=10
BATTERY_MAX_DISCHARGE_KW=10
BATTERY_EFFICIENCY=0.95
MIN_SOC=20
MAX_SOC=90

# Peak-shaving
PEAK_LIMIT_KW=9.5
PEAK_RESERVE_KWH=5.0

# EVCS (1-fase!)
EVCS_ENTITY_PREFIX=evcs_hq2309vtvnf
EVCS_MIN_CURRENT_A=6
EVCS_MAX_CURRENT_A=16
EVCS_PHASES=1

# Tariffer (Føie AS 2026, eks mva)
GRID_TARIFF_DAY_ORE=16.50
GRID_TARIFF_NIGHT_ORE=10.00
CONSUMPTION_TAX_ORE=7.13
ENOVA_ORE=1.00
SUPPLIER_MARKUP_ORE=6.50
SELL_PRICE_ORE=75.00
NET_SELL_BACK_ORE=6.25
CAPACITY_CHARGE_NOK=662.50

# Home Assistant
HA_URL=https://homeassistant.abelgaard.no
HA_TOKEN=<secret>
```

---

## 11. Endringslogg (teknisk)

| Dato | Endring |
|---|---|
| 2026-05-11 | Produksjonssetting: DESS deaktivert, Hub4Mode=3, Qubino primærmåler |
| 2026-05-11 | Export-guard: 30% → 60% toleranse |
| 2026-05-11 | `_decide_action_tariff()` fjernet (dead code) |
| 2026-05-11 | Natt-lading cappet mot live grid/peak-limit i `_execute_action` |
| 2026-05-11 | `optimizer.py`: statisk 1.5 kW nattforbruk-estimat for fremtidsplan |
| 2026-05-11 | SQLite `datetime('now', 'localtime')` for korrekt Oslo-tid |
| 2026-05-11 | `SYSTEM_ANALYSIS.md` oppdatert med seksjon 6.4 og Windsurf-notater |
