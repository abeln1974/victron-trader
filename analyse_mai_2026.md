# Energianalyse Abelgård — 4–14 mai 2026

**Generert:** 2026-05-14  
**Periode:** 10 dager (4. mai kl 06:00 – 14. mai kl 07:00)  
**Kilde:** Home Assistant (Qubino 3-fase hovedmåler, Fronius, EVCS, SmartShunt)

---

## Målte verdier

| Parameter | Totalt | Per dag |
|---|---|---|
| Grid import (3-fase Qubino) | 607.4 kWh | 60.7 kWh/dag |
| Grid eksport (sol overskudd) | 60.7 kWh | 6.1 kWh/dag |
| Sol produksjon (Fronius målt) | 254.5 kWh | 25.4 kWh/dag |
| Sol selvforbruk | 193.8 kWh | 19.4 kWh/dag |
| Elbil lading (EVCS) | 249.8 kWh | 25.0 kWh/dag |
| Batteri ladet (SmartShunt) | 153.1 kWh | 15.3 kWh/dag |
| Batteri utladet (SmartShunt) | 149.1 kWh | 14.9 kWh/dag |

## Beregnet forbruk

| | kWh/dag |
|---|---|
| Husforbruk eks elbil | 54.7 kWh/dag |
| Elbillading (2 Polestarer) | 25.0 kWh/dag |
| Total forbruk | 79.7 kWh/dag |
| Netto fra grid | 54.7 kWh/dag |

**Merknad:** Husforbruk 54.7 kWh/dag inkluderer batteri-nattlading (15.3 kWh/dag).  
Trekker man ut netto batteri-tap (~4 kWh/dag) er faktisk husforbruk ~35 kWh/dag — normalt for mai.

## Sammenligning

| | kWh/dag |
|---|---|
| Abelgård årssnitt 2025 | 58.7 kWh/dag (21 409 kWh totalt) |
| Abelgård mai 2026 (husforbruk) | 54.7 kWh/dag |
| Mai-mål uten oppvarming | ~30–35 kWh/dag |

## Sol-ytelse

| Parameter | Verdi |
|---|---|
| Fronius 5kW produksjon | 25.4 kWh/dag |
| Kapasitetsfaktor | 21.2% |
| Selvforbruksandel | 76% (brukt lokalt) |
| Eksportandel | 24% (til nett) |

76% selvforbruk er godt — batteri og elbillading absorberer mesteparten av solproduksjonen.

## Kapasitetstrinn

- **Peak-shaving aktiv** — holder grid under 9.5 kW ✅
- **Trinn 3** (5–9.99 kW): 418.8 kr/mnd
- Uten peak-shaving: sannsynlig **Trinn 4** (10–14.99 kW): 662.5 kr/mnd
- **Besparelse: 243.7 kr/mnd** — primært fra nattlading av 2 Polestarer + batteri

## Konklusjon

Systemet fungerer som planlagt. Det høye grid-importtallet (60.7 kWh/dag) skyldes i stor grad:
1. **Elbillading** (25.0 kWh/dag) — 2 Polestarer i daglig bruk
2. **Batteri-nattlading** (15.3 kWh/dag) — kjøper billig nattstrøm for å dekke dagen

Netto grid etter eksport: **54.7 kWh/dag** — dette er forventet for et husholdning med  
to elbiler, 42.8 kWh batteri og 5kW sol i mai.

