# Forslag til Gitea Issues (Private)

Kopier hver seksjon og lim inn som ny Issue i Gitea:
https://gitea.abelgaard.no/lars/victron-trader/issues

---

## Issue 1: Hev MIN_PRICE_DIFF_NOK 21. mai

**Tittel:** Hev MIN_PRICE_DIFF_NOK fra 0.10 til 0.50-0.80 (ca 21. mai)

**Beskrivelse:**
Per SYSTEM_ANALYSIS.md seksjon 12 og 8.5:

> Etter ~10 dager fra 2026-05-11 (dvs. rundt 2026-05-21): hev MIN_PRICE_DIFF_NOK fra 0.10 → 0.50 i .env. Bekreft først at handler er korrekte i dashboardet.

Daglig arbitrasje ved 0.10 sliter batteriet uten tilstrekkelig gevinst.

**Sjekkliste:**
- [ ] Verifiser at handler vises korrekt i dashboardet (http://localhost:8080)
- [ ] Endre `MIN_PRICE_DIFF_NOK=0.50` i `/opt/victron-trader/.env`
- [ ] Restart container: `docker compose restart victron-trader`
- [ ] Observere i 2-3 dager — færre handler men bedre margin per syklus

**Referanse:** SYSTEM_ANALYSIS.md seksjon 8.5 ( batterislitasje 1.00 kr/kWh)

---

## Issue 2: Overvåke max SOC-oppførsel

**Tittel:** Overvåke max SOC-oppførsel — AC-koblet Fronius

**Beskrivelse:**
Åpent problem fra SYSTEM_ANALYSIS.md seksjon 6.5:

DVCC reg 2705 = 0A virker IKKE for AC-koblet Fronius (kun DC MPPT). Victron i Mode 2 går i Absorption og lader forbi 90% ved høy sol.

**Observert 2026-05-12:**
- SOC nådde 90.4%
- Absorption ~10 min
- Deretter Float naturlig
- BMS (57.4V) beskytter mot overlading

**Konklusjon så langt:** Akseptabelt — ikke farlig, men overvåkes.

**Sjekkliste:**
- [ ] Observere SOC ved sterkt solskinn (mai-juni)
- [ ] Notere maks SOC nådd
- [ ] Vurdere MQTT-løsning (SocLimitForFloat) hvis problematisk

**Referanse:** SYSTEM_ANALYSIS.md seksjon 6.5

---

## Issue 3: Vurdere LP-optimering

**Tittel:** Vurdere LP-optimering vs topp-N greedy

**Beskrivelse:**
Fra SYSTEM_ANALYSIS.md seksjon 16.5:

> Bruk `scipy.optimize.linprog` eller `PuLP` for å løse hele 24t-problemet optimalt.
> 
> **Gevinst:** Garantert bedre schedule enn topp-N — spesielt ved mange timer med lik pris.

**Kompleksitet:** Medium — `scipy` er allerede tilgjengelig, ingen nye avhengigheter.

**Konseptuell formulering:**
```
Minimer: sum(buy_cost[t] * charge[t]) - sum(sell_price[t] * discharge[t])
Betingelser: 
  SOC[t+1] = SOC[t] + charge[t]*eff - discharge[t]/eff
  0 <= SOC[t] <= max_soc
  0 <= charge[t] <= max_charge
  0 <= discharge[t] <= max_discharge
```

**Sjekkliste:**
- [ ] Sammenligne topp-N vs LP på historiske data
- [ ] Implementere hvis gevinst > 10% bedre resultat

**Referanse:** SYSTEM_ANALYSIS.md seksjon 16.5

---

## Issue 4: MQTT for max SOC-float

**Tittel:** Implementere MQTT mot Cerbo GX for SocLimitForFloat

**Beskrivelse:**
Anbefalt løsning fra SYSTEM_ANALYSIS.md seksjon 6.5 for å løse max SOC-problemet med AC-koblet sol:

> MQTT mot Cerbo GX port 1883 (Venus OS broker), sett SocLimitForFloat

Dette vil gi eksplisitt kontroll over float-spenning vs Absorption.

**Teknisk:**
- Cerbo GX har innebygget MQTT broker på port 1883
- Topic: `W/+/settings/0/SocLimitForFloat` (eller tilsvarende)
- Krever autentisering mot Venus OS

**Sjekkliste:**
- [ ] Finne korrekt MQTT topic for SocLimitForFloat
- [ ] Teste mot lokal Cerbo GX (192.168.1.60)
- [ ] Implementere som fallback når `_enforce_max_soc()` ikke er tilstrekkelig

**Alternativ:** Fortsette med nåværende løsning (Mode 2 float) hvis problem ikke er alvorlig.

**Referanse:** SYSTEM_ANALYSIS.md seksjon 6.5

---

## Hvordan opprette i Gitea

1. Gå til https://gitea.abelgaard.no/lars/victron-trader/issues
2. Klikk **"New Issue"**
3. Kopier tittel og beskrivelse fra over
4. Velg **Label** (f.eks. `enhancement`, `bug`, `documentation`)
5. Klikk **"Create Issue"**
