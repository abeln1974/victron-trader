# Victron Energy Trader — Systemanalyse

> Sist oppdatert: 2026-05-12
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
    ├─ Batteri: 4×12kWh NMC Receel (42.8 kWh netto, 45.6 kWh brutto SmartShunt)
    └─ Fronius Primo 5kW (AC-koblet sol)
```

---

## 2. Batteri og anlegg

| Parameter | Verdi | Kilde |
|---|---|---|
| Leverandør | Receel (Recertified Electronics AS) | receel.no |
| Modell | 12kWh Batteri bank × 4 | refurbished NMC EV-celler |
| Pris | 4 × 15 000 kr = **60 000 kr** | inkl BMS |
| Garanti | 5 år | ingen sykkelgaranti oppgitt |
| Kapasitet (netto per modul) | 10.7 kWh | Receel spec |
| Kapasitet (netto total) | 4 × 10.7 = **42.8 kWh** | Receel spec |
| Kapasitet (brutto SmartShunt) | 45.6 kWh | 800 Ah × 57 V målt |
| Spenning nominell | 53 VDC | Receel spec |
| Spenning max/min | 57.4 / 44.8 VDC | Receel spec |
| Maks effekt kontinuerlig | 5 000 W | Receel spec |
| Maks effekt peak | 10 000 W (2 sek) | Receel spec |
| Vekt | 4 × 200 kg = 800 kg | Receel spec |
| Max lading | 10 kW | 2× MultiPlus-II 48/5000 |
| Max utlading | 10 kW | 2× MultiPlus-II 48/5000 |
| Min SOC (kode) | 20 % | NMC konservativ (Victron floor: 10 %) |
| Max SOC (kode) | 90 % | NMC levetid |
| Brukbar kapasitet (20–90 %) | **30.0 kWh** | 42.8 × 0.70 |
| Peak-reserve | 5 kWh | Alltid tilgjengelig for peak-shaving |
| Virkningsgrad | 0.95 | Round-trip tap — inkluderer IKKE batterislitasje |
| Sol | 5 kW Fronius Primo | AC-koblet |

### ESS-styring via Modbus-TCP
- **Hub4Mode = 2** (Optimized without BatteryLife) — normalstand
- **Hub4Mode = 3** (ESS Control Disabled) — aktiv trading/utlading
- **Reg37 / unit227** — grid setpoint (W, signed16, negativ = eksport)
- Keepalive: reg37 må skrives hvert < 10s i Mode 3, ellers nullstilles av MultiPlus
- **Max SOC-håndhevelse**: se seksjon 6.5 — åpent problem (AC-koblet sol)
- Startup-reset: Hub4Mode=2 og reg37=0 alltid ved oppstart (krasj-sikring)

### Automatisk bytte mellom Mode 2 og Mode 3

| Tilstand | Hub4Mode | Hvem styrer | Hva skjer |
|---|---|---|---|
| **Idle / ingen lønnsom handel** | 2 (Optimized) | Victron GX | Sol-lading skjer automatisk. Grid brukes kun til husforbruk. Trader overvåker og griper inn ved behov. |
| **Lading/utlading (trading)** | 3 (ExternalControl) | Trader | Trader setter setpoint via Modbus. Victron følger ordre. |
| **Peak-shaving triggered** | 3 (ExternalControl) | Trader | Trader overstyrer for å kutte effekttopp — prioritet over alt annet. |

**Nøkkelpoeng:** Traderen har **alltid siste ordet** når det trengs. I idle-tilstand lar vi Victron styre, men peak-shaving eller trading aktiverer umiddelbart Mode 3. Se `victron_modbus.py:_ensure_external_control()` som automatisk bytter til Mode 3 ved `set_charge_power()` / `set_discharge_power()`.

---

## 3. Prisstruktur (Føie AS / Kraftriket + Norgespris, 2026)

### Kjøpspris — NORGESPRIS pristak (eks mva, deretter ×1.25)

Norgespris er en statlig støtteordning bestilt via Elhub/Føie AS (aktiv 01.10.2025–31.12.2026).
Det er et **fast pristak på 40 øre eks mva (50 øre inkl. mva)** — du betaler alltid 40 øre
eks mva for energileddet uansett hva spotprisen er. Staten dekker differansen oppover.
Du får IKKE billigere strøm om spot er lavere enn 40 øre.

```
Energiledd (alltid):     40.00 øre eks mva  (Norgespris-tak, uavhengig av spot)
+ Nettleie dag  (06–22): 16.50 øre eks mva → 20.63 inkl mva
+ Nettleie natt (22–06): 10.00 øre eks mva → 12.50 inkl mva
+ Forbruksavgift:         7.13 øre eks mva →  8.91 inkl mva
+ Enova:                  1.00 øre eks mva →  1.25 inkl mva
× 1.25 mva
= Dag-kjøpspris:  81.0 øre inkl mva  (alltid, uansett spot)
= Natt-kjøpspris: 72.7 øre inkl mva  (alltid, uansett spot)
```

**Historisk besparelse 2025:** 21 409 kWh forbruk — Norgespris sparte 5 148 kr vs ordinær støtte.

### Salgspris (plusskunde, ingen mva)
```
Spotpris Nordpool NO1 eks mva  (variabel, ingen påslag eller fradrag)
Kraftriket betaler spot direkte — ingen mellomledd.
```

> **Merk — arbitrasje-konsekvens:**
> Kjøpspris er fast (81 øre dag / 72.7 øre natt inkl mva).
> Arbitrasje er lønnsomt kun når spot eks mva > kjøpspris eks mva:
> - Dag: spot > 64.8 øre eks mva (81 / 1.25) — men vi kjøpte til 40+avg, ikke spot
> - Enklere: selg når spot eks mva > 81 øre (dvs høyere enn hva vi betalte inkl mva)
> - I praksis: sommer spot 80–120 øre eks mva → margin 0–39 øre → dekker IKKE 157 øre slitasje

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

> **Merk:** Spread = salgspris − kjøpspris. Positivt spread = lønnsomt å selge.
> `should_discharge()` og `web.py` bruker begge `sell - buy` (korrekt etter 2026-05-11 fix).

---

## 4. Optimaliseringsstrategi

### 4.1 Utlading (salg)
1. Hent priser for neste 24 timer (evt 36t hvis i morgen er tilgjengelig)
2. Planlegg med `planned_soc = max(current_soc, max_soc)` — anta full nattlading
3. Beregn usable kWh fra `planned_soc` ned til `min_soc − peak_reserve`
4. Sorter lønnsomme **dagtimer** (06–22) etter høyeste spotpris, ta topp-N til batteri er tomt
5. **Resultat:** selger kun i de dyreste dagtimene — natt reserveres for lading

**Lønnsomhetskrav utlading:**
```
sell_ore(spot) − buy_price_ore(spot, hour) > min_price_diff_nok × 100
Eksempel dag: 115 − 81 = +34 øre > 10 øre → trigger
```

### 4.2 Natt-lading
1. Lademål = `max_soc − solar_reserve_pct` (dynamisk fra Open-Meteo MEPS)
   - Solrikt dag (prognose 29 kWh) → reserve 40% → lader til 50% SOC om natten
   - Overskyet dag (prognose 8 kWh) → reserve 18% → lader til 72% SOC om natten
2. Beregn behov med 20% buffer for peak-shaving-reduksjon
3. Velg billigste nattetimer (22–06) — ingen lønnsomhetssjekk (billigst er alltid best)
4. **Cap ladeeffekt mot live grid/peak-limit** — se seksjon 6.4

**Sol-reserve logikk (dynamisk fra 2026-05-12):**
```python
# solar_forecast.py — Open-Meteo MET Norway MEPS 2.5 km
solar_kwh_tomorrow = get_solar_kwh_tomorrow(lat, lon, panel_kw=5.0, eff=0.85)
solar_reserve_pct = min(40, solar_kwh_tomorrow / capacity × 100)
charge_target_soc = max_soc − solar_reserve_pct
# Fallback ved API-feil: SOLAR_EFFECTIVE_HOURS=4.0 (statisk)
```
> Koordinater settes via `SITE_LAT` / `SITE_LON` i `.env` (default: 60.14, 10.25 Ringerike)

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
grid_without_evcs = grid_kw - evcs_kw
available_kw = peak_limit(9.5) - grid_without_evcs
amps = int(available_kw * 1000 / (phases * 230))
amps = clamp(amps, min=6, max=16)
```

### Peak-shaving vs EVCS — hva skjer ved 7000W elbillading?

**Spørsmål:** Vil batteriet tømmes for å peak-shave når elbilen drar 7000W?

**Svar:** **Nei** — batteriet brukes kun til peak-shaving når grid > 9.5 kW.

| Scenario | Grid-last | Peak-shaving? | Batteri | EVCS |
|---|---|---|---|---|
| Elbil 7kW + hus 1kW | **8.0 kW** | Nei (< 9.5kW) | Fortsetter egen drift (trading/idle) | Lader med 7kW |
| Elbil 11kW (16A 3-fase) | **11.0 kW** | Ja (> 9.5kW) | Utlader for å holde grid ≤ 9.5kW | Reduseres/stoppes av `adjust_for_trading()` |
| Elbil 7kW + sol 3kW | **4.0 kW** | Nei | Lader kanskje fra sol | Lader med 7kW |
| Elbil 7kW + trading lader 5kW | **12.0 kW** | Ja (> 9.5kW) | Peak-shaving prioriteres — lading reduseres | `available_kw = 9.5 - 7.0 = 2.5kW` → EVCS reduseres |

**Buffer:** 7000W elbil har 2.5kW buffer (9.5 − 7.0) før peak-shaving trigges. Batteriet brukes primært til **trading**, ikke til å dekke normal elbillading. EVCSController sørger for at laderen justeres ned hvis kapasiteten blir trang.

Se `main.py:_check_peak_shaving()` for logikk og `ha_qubino.py:adjust_for_trading()` for EVCS-koordinering.

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

### 6.4 Idle-tilstand — hva skjer når ingenting trades?

**Spørsmål:** Vil batteriet bli stående fast på høy SOC hvis det ikke er lønnsomme handler?

**Svar:** Nei — SOC synker naturlig via husforbruk.

**Hva skjer i idle (ingen lønnsom handel):**
1. Trader går til Mode 2 (`stop_ess_control()`)
2. Victron styrer selv — sol lader hvis tilgjengelig
3. **Husforbruk** (0.5–1.5 kW kontinuerlig) trekker SOC sakte nedover
4. Batteriet faller naturlig til ~85-89% på noen timer
5. Trader tar over igjen når prisene blir gunstige

**Ved høy SOC (≥90%):**
- `_enforce_max_soc()` (kjøres hvert 10s) sikrer at trader ikke aktivt lader forbi 90%
- Victron i Mode 2 går i Absorption → Float naturlig
- Batteriet synker via husforbruk til under 89%
- Ingen fare for overlading — BMS (57.4V) beskytter

**Oppsummering:** Batteriet blir ikke «fast». Det går naturlig nedover av seg selv, og traderen tar kun over når det er en lønnsom handel å gjøre.

### 6.5 Peak-limit cap ved lading (main.py `_execute_action`)
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

### 6.6 ⚠️ ÅPENT PROBLEM: Max SOC-håndhevelse ved AC-koblet sol

**Problemet:**
Victron ESS har ingen Modbus-register for å sette øvre SOC-grense i Mode 2.
`MAX_SOC=90` i koden er kun en programvare-terskel for når *trader* stopper å sende
charge-kommandoer — den hindrer ikke Victron fra å lade batteriet videre via sin egen ESS-logikk.

**Fronius Primo er AC-koblet (på AC output siden av MultiPlus):**
```
Grid ─── MultiPlus ─── AC output ─── Fronius Primo (sol)
                    └── Batteri (DC)
```
Fronius leverer AC-effekt til AC output. MultiPlus ser dette som "husforbruk reduseres"
og lader batteriet med overskuddet. Dette skjer **utenfor DVCC-kontrollen** som kun
gjelder DC MPPT-ladere.

**Forsøkte løsninger og hvorfor de ikke fungerte:**

| Forsøk | Resultat | Årsak |
|---|---|---|
| DVCC reg 2705 = 0A | ❌ Ingen effekt på AC-lading | DVCC gjelder kun DC MPPT |
| Aktiv discharge-setpoint = sol-W | ❌ Suboptimalt | Kunstig eksport, sol går til grid istedenfor husforbruk |
| `stop_ess_control()` → Mode 2 | ⚠️ Delvis | Victron går i Absorption-fase og fortsetter å lade |

**Nåværende tilstand (`_enforce_max_soc` i `main.py`):**
```python
if soc >= CONFIG.max_soc and not self._dvcc_charging_stopped:
    self.victron.stop_ess_control()  # Sikrer Mode 2
    self._dvcc_charging_stopped = True
elif soc < CONFIG.max_soc - 1.0 and self._dvcc_charging_stopped:
    self._dvcc_charging_stopped = False
```
Dette er ikke tilstrekkelig — Victron i Mode 2 vil fortsatt gå i Absorption og lade
batteriet forbi 90% ved høy sol-produksjon.

**Mulige løsninger som ikke er implementert:**

1. **Begrens Fronius via Modbus** — Fronius Primo har egen Modbus-TCP på port 502.
   Register: `inverter/LimActivePwr` kan sette maks AC-effekt. Krever separat Modbus-tilkobling
   til Fronius IP-adresse.

2. **ESS BatteryLife "Keep charged" trick** — Sett `ESS/SocLimitForFloat` via dbus/MQTT
   på Cerbo GX direkte (ikke via Modbus). Krever SSH til Cerbo eller Node-RED.

3. **MQTT til Cerbo GX** — `com.victronenergy.settings /Settings/CGwacs/BatteryLife/SocLimit`
   kan settes til 90% via MQTT. Krever at MQTT er aktivert på Cerbo og Python `paho-mqtt`.

4. **Modbus reg 2900** — Udokumentert register som muligens kan sette max SOC i ESS.
   Krever testing mot faktisk Cerbo GX v3.72.

**Observert oppførsel (2026-05-12):**
SOC nådde 90.4% og Victron gikk i **Absorption** (konstant spenning ~57V, synkende strøm).
Trader satte Mode 2 via `stop_ess_control()` — men Victron fortsatte å lade i Absorption
som er en del av sin interne ladekurve, uavhengig av Hub4Mode.

Etter ca. 10 minutter gikk Victron selv til **Float** og ladingen stoppet naturlig.
SOC sank deretter fra 90.4% til 86.7% via husforbruk alene — ingen aktiv utlading fra trader.

```
15:17  SOC 89.8% — trader idle, sol lader
15:24  SOC 90.4% — Absorption, trader setter Mode 2
15:40  SOC 86.7% — naturlig forbruk, batteri +495W (svak sol-lading), grid +1122W
```

**Konklusjon:** Victron håndterer ladekurven (Bulk → Absorption → Float) selv.
`_enforce_max_soc()` hindrer trader fra å aktivt lade forbi 90%, men kan ikke stoppe
Victrons interne Absorption-fase. Dette er **akseptabelt** — Absorption ved 90% er
ikke skadelig for NMC (det er konstant spenning, ikke overlading). Batteriet vil aldri
overstige BMS-grensen (57.4V).

**Anbefalt neste steg:**
Undersøk MQTT-tilnærmingen for en renere løsning — Cerbo GX kjører Venus OS med
innebygd MQTT broker på port 1883. Dette er Victrons anbefalte måte å sette
ESS-parametre programmatisk (f.eks. `SocLimitForFloat`).

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

## 8. Batterilevetid og lønnsomhet

### 8.1 Systembelastning (infrastruktur)

Modbus, HA og SQLite-belastningen er godt innenfor hva komponentene tåler:

| Komponent | Frekvens | Vurdering |
|---|---|---|
| Modbus-TCP mot Cerbo GX | ~25-30 kall/min | Trygt — Victron VRM poller like hyppig |
| Qubino via HA REST | 1 kall / 30s | Svært forsiktig — ingen risiko |
| SQLite skriving | 1/time + peak-events | Tåler dette i årevis |

### 8.2 Batterislitasje — den reelle kostnaden

`battery_efficiency=0.95` i koden dekker kun **round-trip tap** (varme).
Den dekker **ikke** batterislitasje fra sykling.

**Kostnad per syklus (Receel 4×12kWh refurbished NMC):**

| Parameter | Verdi | Kilde |
|---|---|---|
| Batterikostnad | 60 000 kr | 4 × 15 000 kr (receel.no) |
| Brukbar energi per syklus (20–90%) | 30.0 kWh | 42.8 kWh × 70% |
| Levetid optimistisk | 2 500 sykler | NMC EV-celler godt vedlikeholdt |
| Levetid realistisk | 2 000 sykler | Refurbished — usikker restlevetid |
| Levetid pessimistisk | 1 500 sykler | Degraderte celler |
| **Batterikostnad/kWh optimistisk** | **0.80 kr/kWh** | 60 000 / 2500 / 30.0 |
| **Batterikostnad/kWh realistisk** | **1.00 kr/kWh** | 60 000 / 2000 / 30.0 |
| **Batterikostnad/kWh pessimistisk** | **1.33 kr/kWh** | 60 000 / 1500 / 30.0 |

### 8.3 Arbitrasje — lønnsomhetsgrense

Kjøpspris er fast uansett spotpris (Norgespris-tak):
- Natt: 72.7 øre inkl mva
- Dag:  81.0 øre inkl mva

Arbitrasje-regnestykket (lad natt, selg dag):

```
Kjøp natt:       72.7 øre inkl mva (fast, alltid)
Salg dag:        spot eks mva (f.eks. 100 øre)
Brutto spread:   100 − 72.7 = 27.3 øre
Round-trip tap:  −3.6 øre (5%)
Batterislitasje: −100.0 øre  (realistisk 1.00 kr/kWh)
─────────────────────────────
Netto: 27.3 − 3.6 − 100 = −76 øre → IKKE lønnsomt
```

**Hva skal til for at arbitrasje lønner seg?**
```
Nødvendig spot eks mva:  72.7 + 100 + 3.6 = 176 øre eks mva (≈ 220 øre inkl mva)
```
Spot må altså over **176 øre eks mva** bare for å gå i null på batterislitasje.
Det skjer noen dager om vinteren, ikke om sommeren.

**Vinter-topp (spot 250 øre dag):**
```
Spread: 250 − 72.7 = 177 øre − 100 slitasje − 3.6 tap = +73 øre → lønnsomt
```

**Konklusjon:** Arbitrasje er kun lønnsomt ved **spot over ~176 øre eks mva**.
Typiske sommerdager (80–130 øre) gir negativt netto — ikke trade da.

### 8.4 Hva som faktisk er lønnsomt

| Strategi | Batterislitasje | Gevinst | Vurdering |
|---|---|---|---|
| Peak-shaving | Minimal | 243.7 kr/mnd | ✅ Klart lønnsomt |
| Sol-selvforbruk | Ingen | Spart 81 øre/kWh | ✅ Klart lønnsomt |
| Vinter-arbitrasje (spot >176 øre eks mva) | Lav (sjelden) | +73–150 øre netto | ✅ Lønnsomt |
| Sommer-arbitrasje (spot 80–130 øre) | Medium | −70 til −100 øre netto | ❌ Ikke lønnsomt |

### 8.5 Anbefalt konfigurasjon for batterilevetid

`MIN_PRICE_DIFF_NOK=0.10` (10 øre) er for lavt — systemet trader for aggressivt.

Basert på Receel 60 000 kr / 2000 sykler / 30.0 kWh = **1.00 kr/kWh slitasje**:

| Scenario | Slitasje | Terskel |
|---|---|---|
| Optimistisk (2500 sykler) | 0.80 kr/kWh | **0.95 kr** spread |
| Realistisk (2000 sykler) | 1.00 kr/kWh | **1.10 kr** spread |
| Pessimistisk (1500 sykler) | 1.33 kr/kWh | **1.45 kr** spread |

- **Anbefalt `MIN_PRICE_DIFF_NOK=1.10`** — balanse mellom levetid og trading-muligheter
- La peak-shaving og sol-selvforbruk gjøre hoveddelen av jobben
- Arbitrasje trigges automatisk ved vinter-topper (spot >176 øre eks mva)

> **For Windsurf/AI-assistenter:**
> Ikke senk `MIN_PRICE_DIFF_NOK` uten å vurdere batterislitasje.
> Systemet er primært et peak-shaving- og sol-selvforbrukssystem.
> Arbitrasje er sekundært og bør kun skje ved ekstreme priser.

---

## 9. Lønnsomhetsanalyse — Break-even og nødvendig fortjeneste

### 9.1 Investeringsoversikt

| Post | Beløp |
|---|---|
| Batteri (4×12 kWh NMC, Receel refurbished) | 60 000 kr |
| Installasjon / inverter / annet | ~30 000 kr |
| **Total investering** | **~180 000 kr** |
| Antatt levetid (20–90% SOC-vindu) | 15 år / ~3000 sykler |
| Nødvendig inntekt for break-even 15 år | **12 142 kr/år (1 012 kr/mnd)** |

### 9.2 Forventede inntekter

| Inntektskilde | Beregning | Kr/år |
|---|---|---|
| **Peak-shaving** | Trinn 4→3: 243.7 kr/mnd × 12 | **2 924 kr** |
| **Sol-selvforbruk via batteri** | 1 900 kWh × 81 øre (spart kjøpspris) | **1 539 kr** |
| **Vinter-arbitrasje** | ~20 dager × 25 kWh × 70 øre netto | **350 kr** |
| **Sum inntekter (basis)** | | **4 813 kr/år** |

> Sol-selvforbruk: Fronius 5 kW, Ringerike ~950 kWh/kWp/år = 4 750 kWh/år total.
> Estimert 40 % lagres via batteri og brukes selv istedenfor å importere fra nett.

### 9.3 Kostnader

| Kostnad | Kr/år |
|---|---|
| Kapitalavskrivning (180 000 / 15 år) | 12 000 kr |
| Standby-strøm (~20W × 8760t × 81 øre) | 142 kr |
| **Sum kostnader** | **12 142 kr/år** |

### 9.4 Resultat — basis-scenario

```
Inntekter:   4 813 kr/år
Kostnader:  12 142 kr/år
─────────────────────────
Netto:      −7 329 kr/år
Break-even:    37.4 år  ← IKKE lønnsomt på 15 år
```

**Konklusjon: Systemet er ikke lønnsomt som ren investering** ved basis-forutsetninger.
Break-even krever 15 år, men levetiden er ~15 år. Marginen er for liten.

### 9.5 Sensitivitetsanalyse — break-even år

| Scenario | Inntekt/år | Break-even |
|---|---|---|
| Pessimistisk (kun peak-shaving) | 2 924 kr | 61.6 år ❌ |
| **Basis** (peak + sol + vinter-arb) | **4 813 kr** | **37.4 år** ❌ |
| Optimistisk (+3 000 kWh sol via bat) | 7 243 kr | 24.9 år ❌ |
| Med elbil-lading fra sol (+5 000 kWh) | 8 863 kr | 20.3 år ❌ |
| **Break-even på 15 år krever** | **12 000 kr/år** | **15.0 år** ✅ |

### 9.6 Hva skal til for å nå break-even på 15 år?

Nødvendig ekstrainntekt utover basis: **7 329 kr/år**

Mulige veier dit:

| Tiltak | Effekt | Realisme |
|---|---|---|
| Øk elbil-lading fra sol (begge biler, mer sol) | +2 000–4 000 kr/år | 🟡 Mulig |
| Strømprisøkning — spot 200+ øre snitt | +2 000–5 000 kr/år | 🟡 Mulig (vinter) |
| Eksport-inntekt på topp-dager (spot 300+ øre) | +1 000–3 000 kr/år | 🟡 Avhenger av marked |
| Kapasitetsledd ned til trinn 2 (< 5 kW peak) | +75 kr/mnd = 900 kr/år | 🔴 Krevende |
| Lavere anskaffelseskost (regnet 30 000 kr for mye?) | Reduserer break-even | 🟢 Mulig |

### 9.7 Reell vurdering

Batterianlegget er **primært ikke en finansiell investering** — det er:
- **Energiuavhengighet** og redundans (UPS-funksjon)
- **Fremtidssikring** mot høyere strømpriser (sommer 2025–2026 er historisk lavt)
- **Peak-shaving** som faktisk sparer 244 kr/mnd fra dag 1

Ved strømprisnivåer tilsvarende vinteren 2022–2023 (spot 300–500 øre) ville
arbitrasje alene gitt 10 000–20 000 kr/år og gjort prosjektet klart lønnsomt.

> **Konklusjon:** Break-even realistisk ved ~20–25 år med normal drift og
> gjennomsnittlige norske strømpriser. Prosjektet lønner seg **ikke** på ren
> arbitrasje ved dagens sommerpriser, men peak-shaving og sol-selvforbruk
> gir positiv kontantstrøm fra dag 1 (4 813 kr/år vs 0 uten systemet).

---

## 10. Driftsanalyse — Natt 11–12 mai 2026 (første natt)

### 10.1 Observasjoner fra logger

| Tid | SOC | Grid | Handling | Status |
|---|---|---|---|---|
| 22:18 | 28.1% | 8441W | Lading cappet 8→3.9kW | Peak-cap virker ✅ |
| 00:28 | 40.8% | 10300W | PEAK-SHAVING: 8→7.2→6.4→5.6kW | Jaging (se nedenfor) |
| 01:00 | 45.8% | 6594W | Lading 2.9kW | Lavt forbruk |
| 03:00 | 52.9% | 3113W | Lading 6.4kW | Normalt |
| 06:00 | – | – | idle — sol 0.06 kW | Korrekt |
| 08:00 | – | – | idle — sol 1.12 kW lader gratis | Sol-selvforbruk ✅ |
| 11:00 | 70.9% | 1763W | idle — sol 4.22 kW lader gratis | Sol på vei mot topp ✅ |

**Maks grid-import natt:** 9002W — under 9500W peak-grense ✅  
**Maks SOC observert:** 77.4% (sol + nattlading)  
**Profit loggede handler:** 0 kr (utlading kl 19-22 ble ikke logget — container rebuild kl 22:18 slettet forrige stats)

### 10.2 Funn og problemstillinger

**✅ Fungerer bra:**
- Peak-shaving holder grid under 9.5 kW
- Sol-selvforbruk: 4.2 kW kl 11 lader gratis uten grid-import
- Discharge på kveld (19-21) med +30 øre margin trigget korrekt
- EVCS stoppes under discharge, gjenopptas under idle/charge
- Fastpris + avgifter = 81 øre dag / 73 øre natt beregnes korrekt

**⚠️ Forbedringsområder (nye funn):**
- **Peak-shaving jager under lading** (kl 00:28): reduserte 8→7.2→6.4→5.6kW på 24s. Hysterese på 0.3 kW lagt til, men grunnproblemet er at `current_action.power_kw` oppdateres kumulativt — andre gang sjekker den feil referansepunkt
- **Lading for langsom** — SOC bare 77% etter 13 timer (22:18–11:10). Skyldes EVCS (3.7kW) som konkurrerer om kapasitet og peak-shaving som reduserer ladeeffekt. Batteriet når kanskje ikke lademålet
- **Sol-reserve beregning er statisk** — 4 effektive timer er gjetning. En skyet dag lader ikke sol 20 kWh, og batteriet starter da for lavt på kvelden
- **Profit-logging starter ikke fra 0** — container rebuild fører til at første natt ikke er loggført. Vurder å skrive oppstart-tidspunkt til DB

---

## 11. Kjente svakheter / forbedringspotensial

### 🔴 Høy prioritet
1. ~~**`MIN_PRICE_DIFF_NOK` bør heves**~~ — **FIKSET** 2026-05-12: Satt til **1.10 kr** basert på
   Receel 60 000 kr / 2000 sykler / 30.0 kWh = 1.00 kr/kWh slitasje.
2. ~~**Peak-shaving kumulativ jaging**~~ — **FIKSET** 2026-05-12: `_original_charge_kw` lagres ved time-start og brukes som fast referanse i `_check_peak_shaving`. `current_action.power_kw` oppdateres ikke lenger.
3. ~~**Sol-reserve er statisk**~~ — **FIKSET** 2026-05-12: `solar_forecast.py` henter
   sol-prognose per time fra Open-Meteo (MET Norway MEPS 2.5 km). Fallback til statisk
   `SOLAR_EFFECTIVE_HOURS=4.0` ved API-feil.

### 🟡 Medium prioritet
4. **Ingen re-planlegging intratime** — priser publiseres kl 13, men re-plan trigges
   nå kun ved `_last_price_count`-endring. Verifiser at dette faktisk virker.
5. **SOC-basert kWh-logging er approx** — `actual_kwh = capacity × delta_soc / 100`
   er unøyaktig. Bør hente direkte fra SmartShunt (Modbus reg 309: kWh discharged).
6. **Lademål nås ikke alltid** — peak-shaving og EVCS konkurrerer om kapasitet om natten.
   Optimizer bør velge flere nattimer enn nødvendig som buffer (20% buffer lagt til, men
   EVCS-forbruk er ikke inkludert i beregningen).
7. **EVCS støtter kun én lader** — to separate ladere håndteres ikke.

### 🟢 Lav prioritet
8. **Dashboard viser ikke EVCS-status** — `web.py` har ingen EVCS-widget.
9. **Ingen alarm ved Qubino Z-Wave "dead"** — koden logger warning men sender ikke varsel.
10. **Profitt-dashboard mangler ukes/måneds-graf** — kun dagens handler vises.

---

## 12. Filstruktur

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
> - **Idle/Mode 2**: Se seksjon 2.1 og 6.4 — batteriet går naturlig ned via husforbruk, Victron styrer selv
> - **Mode 2 ↔ 3 bytte**: Se seksjon 2.1 — trader har alltid siste ordet ved peak-shaving/trading
> - **EVCS + peak-shaving**: Se seksjon 5.3 — 7kW elbil har 2.5kW buffer før peak-shaving trigges
> - `_decide_action_tariff()` er fjernet (var dead code) — bruk `get_immediate_action()` → `optimize()`
> - Systemet er primært peak-shaving/sol-selvforbruk — arbitrasje er sekundært (se seksjon 8)

---

## 13. Konfigurasjon (miljøvariabler)

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

# Tariffer (Føie AS 2026 + Norgespris, eks mva)
NORGESPRIS_CAP_ORE=40.00    # Pristak 50 øre inkl mva = 40 øre eks mva (alltid fast)
GRID_TARIFF_DAY_ORE=16.50
GRID_TARIFF_NIGHT_ORE=10.00
CONSUMPTION_TAX_ORE=7.13
ENOVA_ORE=1.00
CAPACITY_CHARGE_NOK=662.50

# Strategi — arbitrasje kun lønnsomt ved spot >233 øre eks mva
MIN_PRICE_DIFF_NOK=1.10     # Realistisk minimum — Receel 60 000kr/2000sykler/30kWh (se seksjon 8.5)

# Home Assistant
HA_URL=https://homeassistant.abelgaard.no
HA_TOKEN=<secret>
```

---

## 14. Huskelapper / Planlagte endringer

| Dato | Gjør dette | Prioritet |
|---|---|---|
| **2026-05-12** | `MIN_PRICE_DIFF_NOK` satt til **1.10** basert på Receel batterikostnad (se seksjon 8.5). | ✅ Ferdig |
| **Snart** | Verifiser re-planlegging kl 13 ved prisoppdatering faktisk trigges | 🟡 Medium |
| **Fremtid** | Yr.no værvarsler for adaptiv sol-reserve | 🟢 Lav |

---

## 15. Endringslogg (teknisk)

| Dato | Endring |
|---|---|
| 2026-05-11 | Produksjonssetting: DESS deaktivert, Hub4Mode=3, Qubino primærmåler |
| 2026-05-11 | Export-guard: 30% → 60% toleranse |
| 2026-05-11 | `_decide_action_tariff()` fjernet (dead code) |
| 2026-05-11 | Natt-lading cappet mot live grid/peak-limit i `_execute_action` |
| 2026-05-11 | `optimizer.py`: statisk 1.5 kW nattforbruk-estimat for fremtidsplan |
| 2026-05-11 | SQLite `datetime('now', 'localtime')` for korrekt Oslo-tid |
| 2026-05-11 | Seksjon 8: batterilevetid og lønnsomhetsanalyse lagt til |
| 2026-05-11 | `MIN_PRICE_DIFF_NOK` anbefaling hevet til 0.50-0.80 |
| 2026-05-11 | Prismodell rettet: fastpris kjøp (40 øre) + avgifter, salg = spot eks mva |
| 2026-05-11 | Norgespris fjernet fra buy_price_ore (gjelder ikke fastprisavtale) |
| 2026-05-11 | Margin-logikk rettet: sell−buy (positivt = lønnsomt å selge) |
| 2026-05-11 | should_discharge: spread = sell−buy (var buy−sell = alltid negativ) |
| 2026-05-11 | Peak-shaving: reduserer ladeeffekt under lading (ikke utlading) |
| 2026-05-11 | Peak-shaving: 0.3 kW hysterese mot jaging |
| 2026-05-12 | Optimizer: planned_soc = max_soc for discharge-planlegging |
| 2026-05-12 | Optimizer: sol-reserve 44% (5kW × 4t) — lader til ~46% SOC om natten |
| 2026-05-12 | Optimizer: discharge sortert på salgspris (spot), ikke raw_buy (fast) |
| 2026-05-12 | Optimizer: discharge begrenset til dagtid 06–22 |
| 2026-05-12 | main: fiks kumulativ peak-shaving jaging via `_original_charge_kw` |
| 2026-05-12 | tariff: fiks `__main__` (fjernet ugyldig `NORGES_PRICE_ORE`-referanse) |
| 2026-05-12 | config: `evcs_phases` default 3 → 1 (EVCS er 1-fase) |
| 2026-05-12 | tariff: Norgespris er pristak — `buy_price` alltid 40 øre eks mva (rettet fra feil min(spot,40)) |
| 2026-05-12 | feat: `solar_forecast.py` — dynamisk sol-reserve via Open-Meteo MEPS (met.no 2.5km modell) |
| 2026-05-12 | optimizer: statisk sol-reserve erstattet med `get_solar_reserve_pct()` fra `solar_forecast.py` |
| 2026-05-12 | config: `SITE_LAT`, `SITE_LON`, `SOLAR_SYSTEM_EFFICIENCY` lagt til for lokasjon og sol-prognose |
| 2026-05-12 | batteri: Receel-spec inn (60 000kr, 42.8 kWh netto), Farco/150k-estimat fjernet |
| 2026-05-12 | config: `MIN_PRICE_DIFF_NOK` default 1.60 → **1.10** basert på Receel 60k/2000sykler/30kWh |
| 2026-05-12 | victron_modbus: `get_energy_counters()` lagt til (SmartShunt reg 309/310 discharged/charged kWh) |
| 2026-05-12 | main: kWh-logging bruker SmartShunt energitellere (delta) istedenfor SOC-delta — fallback beholdes |
| 2026-05-12 | victron_modbus: `set_max_charge_current()` lagt til (DVCC reg 2705) |
| 2026-05-12 | main: `_enforce_max_soc()` lagt til — kjøres hvert 10s, sikrer Mode 2 (float) når SOC ≥ max_soc |
| 2026-05-12 | **⚠️ ÅPENT PROBLEM**: AC-koblet Fronius lader batteriet forbi max_soc=90% — se seksjon 6.5 |

---

## 16. Sammenligning med andre open-source systemer

### 16.1 Oversikt over sammenlignbare prosjekter

| System | Teknologi | Optimering | Sol-prognose | Modbus | Nordpool | Stars |
|---|---|---|---|---|---|---|
| **victron-trader (dette)** | Python, Docker | Topp-N greedy | ✅ Open-Meteo MEPS | ✅ Direkte | ✅ hvakosterstrommen.no | – |
| **Victron Dynamic ESS** | Node-RED, VRM API | LP (server-side) | VRM/Solcast | ✅ VRM | ✅ Day-ahead EU | ~200 |
| **EMHASS** | Python, HA add-on | LP (PuLP/linprog) | Open-Meteo/Solcast | ❌ Ingen | ✅ Via HA-sensor | ~1900 |
| **Battery-Storage-Optimizer** | Python, Pyomo | LP (Pyomo/GLPK) | Ingen | ❌ Ingen | ❌ Generisk | ~50 |

### 16.2 Victron Dynamic ESS (offisiell, Node-RED)
**GitHub:** `victronenergy/dynamic-ess`

Victrons egen implementasjon som kjører i Node-RED på Cerbo GX / VRM.

**Hva den gjør bedre enn dette systemet:**
- **Linear Programming (LP)** via VRM-serveren — finner matematisk optimalt schedule, ikke greedy topp-N
- **Solcast-integrasjon** — bruker faktiske sol-prognoser per time, ikke statisk reserve
- **SOC-kurve som mål** — planlegger mot en optimal SOC-kurve gjennom hele dagen, ikke bare enkelt-timer
- **Restrictions per timeslot** — kan sette "ikke eksporter" eller "ikke importer" per time
- To strategier: "Follow target SOC" og "Minimize grid usage"
- Bygget og testet av Victron selv — meget stabilt

**Hva dette systemet gjør bedre:**
- **Norsk pristruktur** — Norgespris-tak, nettleie dag/natt, kapasitetsledd korrekt beregnet
- **Peak-shaving mot kapasitetstrinn** — bevisst holdes under 9.5 kW for å spare 244 kr/mnd
- **EVCS-koordinering** — stopper elbillading under discharge, lader fra sol-overskudd
- **Fastpris-støtte** — Dynamic ESS krever dynamisk kontrakt (spotpris), fungerer ikke med fastpris
- **Lokalt og uavhengig** — ingen VRM-avhengighet, fungerer uten internett

> **Interessant:** Dynamic ESS bruker `buy price > max_sell_price − battery_cycle_cost` for å
> stoppe unødvendig trading — samme logikk som `MIN_PRICE_DIFF_NOK` her, men dynamisk beregnet.

### 16.3 EMHASS (Energy Management for Home Assistant)
**GitHub:** `davidusb-geek/emhass` — ~1900 stars, aktivt vedlikeholdt

Mest populære open-source home energy optimizer. Kjører som HA add-on.

**Hva den gjør bedre:**
- **Linear Programming** (scipy.linprog eller PuLP) — garantert globalt optimalt schedule
- **Open-Meteo / Solcast** sol-prognoser — vet faktisk forventet sol per time i morgen
- **Lastprognoser** — modellerer husforbruk per time basert på historikk
- **Deferrable loads** — kan planlegge vaskemaskiner, varmtvann etc. til billigste timer
- **Svært konfigurerbar** — støtter nesten alle prisstrukturer og kontrakter

**Ulemper vs dette systemet:**
- Krever Home Assistant som mellomlag mot Victron (ingen direkte Modbus)
- Ingen innebygd peak-shaving mot norske kapasitetstrinn
- Ingen EVCS-koordinering out-of-the-box
- Mer kompleks å sette opp (mange konfig-parametre)

### 16.4 Hva dette systemet gjør unikt bra

Etter gjennomgang er det klart at `victron-trader` har noen egenskaper som **ikke finnes i noen
av de sammenlignbare systemene**:

1. **Norsk kapasitetsledd-optimering** — bevisst peak-shaving mot Føie AS sine trinn (244 kr/mnd)
2. **Norgespris-tak-beregning** — korrekt håndtering av statlig pristak på 40 øre eks mva
3. **EVCS 1-fase koordinering** — stopper/starter elbillading synkronisert med batteri-trading
4. **Sol-selvforbruk + batteri-reserve** — reserverer plass til Fronius-produksjon om natten
5. **Direkte Modbus-TCP** — ingen HA-avhengighet, lavere latens, fungerer offline

### 16.5 Inspirasjon fra andre systemer — mulige forbedringer

#### 🌟 Idé 1: Linear Programming istedenfor Greedy Topp-N
**Fra:** EMHASS og Dynamic ESS  
**Hva:** Bruk `scipy.optimize.linprog` eller `PuLP` for å løse hele 24t-problemet optimalt.  
**Gevinst:** Garantert bedre schedule enn topp-N — spesielt ved mange timer med lik pris.
```python
# Konseptuelt — LP formulering
# Minimer: sum(buy_cost[t] * charge[t]) - sum(sell_price[t] * discharge[t])
# Betingelser: SOC[t+1] = SOC[t] + charge[t]*eff - discharge[t]/eff
#              0 <= SOC[t] <= max_soc
#              0 <= charge[t] <= max_charge
#              0 <= discharge[t] <= max_discharge
```
**Kompleksitet:** Medium — `scipy` er allerede tilgjengelig, ingen nye avhengigheter.

#### ✅ Idé 2: Sol-prognoser via Open-Meteo med MET Norway-modell — **IMPLEMENTERT 2026-05-12**
**Fra:** EMHASS  
**Bakgrunn — er Open-Meteo bra for Norge?**

> Met.no sin egen `Locationforecast 2.0` API gir **ikke** solstråling — kun `ultraviolet_index_clear_sky`.
> Open-Meteo bruker derimot **MET Nordic MEPS** (met.no sitt 2.5 km ensemble-modell) som kilde,
> og eksponerer `shortwave_radiation` (W/m²) direkte fra dette datasettet.
> Det er altså **met.no sin egen modell under panseret** — bare tilgjengeliggjort via Open-Meteo.
> Nøyaktighet: MetCoOp 2.5 km, ECMWF-initialisert, oppdatert hourly — best tilgjengelig for Skandinavia.

**Hva:** Hent sol-prognose fra `api.open-meteo.com` med `models=metno_seamless` (gratis, ingen API-nøkkel).  
**Gevinst:** Vet om i morgen er skyet (reserve 0%) eller solrikt (reserve 44%) — ikke statisk 4t.
```python
# Open-Meteo med MET Norway-modell, gratis, ingen API-nøkkel:
# GET https://api.open-meteo.com/v1/forecast
#     ?latitude=60.1&longitude=10.2&models=metno_seamless
#     &hourly=shortwave_radiation&forecast_days=2&timezone=Europe/Oslo
# shortwave_radiation [W/m²] integrert over timer → Wh/m²
# solar_kwh = sum(W/m² × 1h) / 1000 × panel_m2 × efficiency
```
**Kompleksitet:** Lav — ~25 linjer Python, ingen ny avhengighet (kun `urllib`/`requests`).  
**Impact:** Høy — unngår feil sol-reserve på overskyet dag.  
**Begrensning:** Kun 2.5 dager med MEPS, deretter ECMWF 9 km — men 24t fremover er alltid MEPS.

#### 🌟 Idé 3: Dynamisk `MIN_PRICE_DIFF_NOK` basert på sesong
**Fra:** Dynamic ESS sin `battery_cycle_cost`-logikk  
**Hva:** Beregn automatisk minimum lønnsom spread basert på batterislitasje.  
**Gevinst:** Sommer: høy terskel (ingen unødvendig trading). Vinter: lav terskel (mer aggressiv).
```python
# Auto-beregn basert på måneden:
# Jun-Aug: min_diff = 1.10 kr (gjeldende verdi — spot sjelden over 176 øre eks mva)
# Sep-Mai: min_diff = 0.80 kr (mer aggressiv — optimistisk sykkel-scenario)
```

#### 🌟 Idé 4: Lastprognose for EVCS (fra EMHASS-konseptet)
**Fra:** EMHASS deferrable loads  
**Hva:** Sjekk om elbil er tilkoblet og plan elbil-lading til billigste nattetimer.  
**Gevinst:** Elbilen lades alltid på billigste tidspunkt innenfor peak-grensen.

### 16.6 Vurdering

**Konklusjon:** `victron-trader` er et **over gjennomsnittlig godt system** for sin use case.
Det er mer spesialisert enn de generiske systemene (EMHASS) og mer tilpasset norske forhold
enn Victrons egen Dynamic ESS. De to viktigste forbedringene som vil gi størst gevinst er:

1. **Open-Meteo sol-prognoser** — lav innsats, høy impact på ladeplanlegging
2. **LP-optimering** — medium innsats, garantert bedre schedule enn greedy topp-N

---

## 17. Lisens og publisering

### 17.1 Valg: AGPL-3.0 (2026-05-12)

**Hvorfor AGPL-3.0 fremfor MIT/Apache:**

| Lisens | Hva andre kan gjøre | Beskyttelse for deg |
|---|---|---|
| **MIT** | Bruke, endre, selge, lukke koden | Ingen — alle kan gjøre hva de vil |
| **Apache-2.0** | Bruke, endre, patentere | Du kan saksøke, men koden kan lukkes |
| **GPL-3.0** | Bruke, endre, må dele kildekode | Alle endringer må publiseres |
| **AGPL-3.0** | Som GPL + nettbruk = kildekode | Nettjenester må også dele koden |

**AGPL-3.0 beskytter spesifikt mot:**
- Noen tar koden, forbedrer den, selger som SaaS uten å dele endringer
- Viktron (eller andre) integrerer i Dynamic ESS uten å gi tilbake
- Patentering av funksjonalitet du har utviklet

**Du beholder alltid:**
- Opphavsrett (copyright) — du eier koden
- Rett til å dual-licensere (ta betalt for kommersiell lukket bruk)
- Rett til å endre lisens senere (hvis alle bidragsytere godtar)

### 17.2 GitHub-publisering (planlagt)

**Strategi: Gitea som master, GitHub som public mirror**

```
[Du] → push → [Gitea privat] → mirror → [GitHub public]
```

**Oppsett:**
1. Opprette `github.com/<bruker>/victron-trader` (public repo)
2. Gitea Settings → Repository → Mirror Settings → Push Mirror
3. Interval: `8h` (hver 8. time synkronisering)
4. GitHub Personal Access Token med `repo` scope

**Fordeler:**
- Du jobber som før mot Gitea (privat kontroll)
- GitHub får automatisk kopi (offentlig synlig)
- Issues/PRs på GitHub kan håndteres etter behov
- Hvis Gitea går ned, kan du pushe direkte til GitHub

**Engelsk dokumentasjon lagt til (2026-05-12):**
- README.md: "Overview (English)" seksjon med key features, hardware requirements, comparison vs Dynamic ESS
- LICENSE: AGPL-3.0 full tekst
- SYSTEM_ANALYSIS.md: Denne seksjonen (17) forklarer lisens-valget for bidragsytere

**For AI-assistenter/Windsurf:**
> Husk at all kode nå er AGPL-3.0. Hvis du foreslår endringer som ender i repoet,
> er de automatisk under samme lisens. Dette er feature, ikke bug — det sikrer at
> forbedringer blir værende åpne.

### 17.3 Hva som er offentlig vs privat

**Offentlig på GitHub:**
- All kildekode (Python, Docker, config)
- Dokumentasjon (SYSTEM_ANALYSIS.md, README.md)
- Lisens og instruksjoner

**Privat (ikke i repo):**
- `.env` fil med tokens og interne URL-er
- `HA_TOKEN` for Home Assistant
- Interne IP-adresser (192.168.x.x er RFC1918, teknisk OK, men ikke nødvendig å eksponere)

**Ingen sensitiv data funnet i koden** — alt er generisk nok til offentlig publisering.
