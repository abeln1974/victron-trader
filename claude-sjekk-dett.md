# Claude Code Review - Victron-Trader

Dato: 2026-05-17
Reviewer: Claude (Cascade AI)
Scope: Full kodebase analyse

---

## 1. Urealistisk SOC-antagelse i optimizer

**Fil:** `optimizer.py:82`
```python
planned_soc = max(current_soc, self.max_soc)  # Anta full lading før neste dag
```

**Problem:** Optimizer antar alltid at batteriet vil være fulladet (90%) i morgen, selv om SOC er 30% nå. Dette fører til:
- Over-planlegging av utlading (tror vi har mer energi enn vi har)
- Potensiell tomgang av batteriet før planlagt ladeperiode

**Eksempel:** Hvis SOC=30% og max_soc=90% → planned_soc=90% (urealistisk)

**Forslag:** Bruk `current_soc` direkte eller mer konservativ estimering.

---

## 2. Profit-tracker regner "fortjeneste" feil

**Fil:** `profit_tracker.py:57-63`
```python
elif action == "sell":
    net_profit = energy_kwh * price_nok_kwh * efficiency
```

**Problem:** Beregner kun **inntekt** fra salg (minus effektivitet), men trekker IKKE fra innkjøpspris. Systemet viser feilaktig positiv "profit" selv om du selger billigere enn du kjøpte.

**Realitet:** Netto profitt = (salgspris - innkjøpspris) × kWh × effektivitet

**Forslag:** Implementer "matched pair" tracking eller bruk gjennomsnittlig innkjøpspris per kWh solgt.

---

## 3. Duplisert storm-mode sjekking (ytelsesproblem)

**Filer:**
- `optimizer.py:103-116`
- `main.py:352-365` (`_check_peak_shaving`)
- `main.py:419-424` og `main.py:455-464` (`_execute_action`)

**Problem:** Samme `get_solar_kwh_tomorrow()` kalles fra 3 ulike steder per syklus. Dette er:
- Unødvendig tregt (API-kall tar ~500ms)
- Potensiell race condition
- Kodeduplisering

**Forslag:** Cache resultatet i `EnergyTrader`-klassen, gyldig i f.eks. 1 time.

---

## 4. Unødvendige inline-imports

**Filer:**
- `solar_forecast.py:52`: `__import__('datetime').timedelta(days=1)`
- `ha_qubino.py:216`: `__import__("time").monotonic()`
- `ha_qubino.py:334`: `__import__("datetime").datetime.now(...)`

**Problem:** Kodestil-problem. Inline imports brukes unødvendig i stedet for vanlige imports på toppen.

**Forslag:** Refactor til normale imports.

---

## 5. Feil prisbruk ved handelslogging

**Fil:** `main.py:243-247`
```python
price_nok = (sell_price_ore(spot_eks_mva * 100) if db_action == "sell"
             else buy_price_ore(spot_eks_mva * 100, prev_hour)) / 100
```

**Problem:** For kjøp brukes `spot_eks_mva` som input, men `buy_price_ore()` bruker Norgespris-tak (40 øre) konstant. Dette gir:
- Riktig kjøpspris i beregning
- Men logges med feil spotpris som metadata
- Forvirrende for analyser

**Forslag:** Logg faktisk betalt pris (Norgespris + avgifter) i metadata, ikke spot.

---

## 6. Export-guard timing-issue

**Fil:** `main.py:206-217`
```python
if current_time - self._action_start_time < 45:
    time.sleep(3)
    continue
```

**Problem:** Venter 45 sekunder før export-guard aktiveres. Hvis batteriet:
- Ikke kommer i gang med utlading (BMS-grense, feil, etc.)
- Får export uten at batteriet utlades

45 sekunder utlading ved 10kW = 0.125 kWh potensiell uønsket eksport.

**Forslag:** Reduser til 10-15 sekunder, eller sjekk kontinuerlig med retry.

---

## 7. Konfigurasjonskonflikt MIN_SOC

**Filer:**
- `config.py:32`: `MIN_SOC = 35` (default)
- `README.md:72`: "Min SOC: 20%"
- `README.md:133`: `MIN_SOC=20` i env-tabell

**Problem:** Uklart hva som er faktisk verdi. 35% eller 20%?

**Betydning:** 
- 35% = ~14 kWh nødstrøm (800W × 17 timer)
- 20% = ~11 kWh nødstrøm (800W × 14 timer)

**Forslag:** Synkroniser dokumentasjon og kode.

---

## 8. Peak-shave action mangler profitt-data

**Fil:** `main.py:406-412`
```python
self.current_action = Action(
    timestamp=datetime.now(OSLO_TZ),
    action='peak_shave',
    power_kw=-discharge_kw,
    expected_profit_nok=0.0,  # <-- Feil
    reason=f'Grid {grid_kw:.1f}kW > {peak_kw}kW minimum shave'
)
```

**Problem:** `expected_profit_nok` settes til 0.0, men `peak_shave()` i optimizer beregner `saving_per_event = 243.7 / 5`.

**Forslag:** Bruk beregnet sparing i `expected_profit_nok`.

---

## 9. Potensiell EVCS-kalkulasjonsfeil

**Fil:** `ha_qubino.py:341`
```python
charge_kw = max(0, min(surplus_kw, available_kw,
                       self._max_a * self._phases * 0.23))
```

**Problem:** Bruker 0.23 kW/A (230V), men IT-nett er 230V L-N. EVCS lader fra L-N, så dette er korrekt for 1-fase. Men:
- Hvis noen endrer til 3-fase EVCS → må være 0.4 kW/A (400V)
- Hardkodet spenning er skjørt

**Forslag:** Bruk målt spenning fra Qubino eller config-basert spenning.

---

## 10. Keepalive bruker _last_setpoint som kan være gammel

**Fil:** `victron_modbus.py:347-354`
```python
def send_keepalive(self) -> bool:
    last = getattr(self, '_last_setpoint', 0)
    return self.set_grid_setpoint(last)
```

**Problem:** Hvis trader har endret strategi men ikke oppdatert `_last_setpoint`, sendes gammel setpoint.

**Forslag:** Sørg for at `_last_setpoint` alltid oppdateres når `current_action` endres.

---

## Oppsummering prioritet

| # | Issue | Alvorlighet | Type |
|---|-------|-------------|------|
| 1 | SOC-antagelse | Medium | Logikk |
| 2 | Profit-beregning | **Høy** | Økonomi |
| 3 | Storm-mode duplisering | Medium | Ytelse |
| 4 | Inline imports | Lav | Kodestil |
| 5 | Prislogging feil | Medium | Data |
| 6 | Export-guard timing | Medium | Sikkerhet |
| 7 | MIN_SOC konflikt | Lav | Konfig |
| 8 | Peak-shave profit | Lav | Data |
| 9 | EVCS spenning | Lav | Robusthet |
| 10 | Keepalive state | Medium | Styring |

---

## Generelle observasjoner

### Styrker:
- God dokumentasjon (README, SYSTEM_ANALYSIS)
- Robust feilhåndtering (try/except mange steder)
- Bra logging
- Fallback-mekanismer (Qubino → VM-3P75CT, hvakosterstrommen → Nordpool)
- READONLY_MODE for testing

### Forbedringsområder:
- Mer caching av API-kall
- Klarere skille mellom spot-pris og Norgespris i logging
- Unittest dekning (kun `mock_test.py` eksisterer)
- State machine for ESS-modus istedenfor bool-flagg
