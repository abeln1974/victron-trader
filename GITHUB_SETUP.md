# GitHub Publisering — Steg-for-Steg Guide

> Opprettet: 2026-05-12  
> For: victron-trader (gitea.abelgaard.no/lars/victron-trader)

---

## Oversikt

**Strategi:** Gitea privat master → GitHub public mirror  
**Fordeler:** Du beholder privat kontroll, får offentlig synlighet automatisk

```
[Deg] → push → [Gitea privat] ──mirror──→ [GitHub public]
                        ↑                    ↓
                        └──────── Issues/PRs ─┘
```

---

## Steg 1: Opprette GitHub Repository

1. Gå til https://github.com/new
2. **Repository name:** `victron-trader`
3. **Description:** "Norwegian-specific Victron ESS battery optimizer with peak-shaving, solar self-consumption, and arbitrage trading"
4. **Visibility:** ☑️ Public
5. **Add a README:** ☐ Nei (vi har allerede)
6. **Add .gitignore:** ☐ Nei (vi har allerede)
7. **Choose a license:** ☐ Nei (vi har allerede AGPL-3.0)
8. Klikk **Create repository**

**Noter URL:** `https://github.com/DITT_BRUKERNAVN/victron-trader.git`

---

## Steg 2: Generere Personal Access Token (PAT)

1. Gå til https://github.com/settings/tokens (eller GitHub → Settings → Developer settings → Personal access tokens)
2. Klikk **Tokens (classic)** → **Generate new token (classic)**
3. **Note:** `Gitea Mirror for victron-trader`
4. **Expiration:** 90 days (eller 1 year)
5. **Scopes:** ☑️ `repo` (Full control of private repositories)
6. Klikk **Generate token**
7. **Kopier token med én gang!** (vises kun én gang)

**Token ser ut som:** `ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

---

## Steg 3: Konfigurere Gitea Push Mirror

1. Gå til https://gitea.abelgaard.no/lars/victron-trader/settings
2. I venstre meny, klikk **Repository** → **Mirror Settings**
3. Under **Push Mirrors** klikk **Add Push Mirror**
4. Fyll ut:
   - **Git Remote Repository URL:** `https://github.com/DITT_BRUKERNAVN/victron-trader.git`
   - **Authorization:** `username:token`
   - **Username:** Ditt GitHub brukernavn
   - **Password/Access Token:** Token fra steg 2 (`ghp_...`)
5. **Mirror Interval:** `8h` (hver 8. time, kan endres til `1h` for hyppigere)
6. Klikk **Add Push Mirror**

---

## Steg 4: Verifisere at Sync Fungerer

### Test 1: Sjekk mirror-status i Gitea
1. Gå til https://gitea.abelgaard.no/lars/victron-trader/settings → Mirror Settings
2. Se etter grønn hake ved mirror-innlegget
3. Sjekk **Last Updated** tidspunkt

### Test 2: Manuell synk-verifikasjon
```bash
# Lokal maskin
cd /home/lars/CascadeProjects/windsurf-project

# Lag en liten endring (f.eks. i denne filen)
echo "# GitHub mirror aktiv: $(date)" >> GITHUB_SETUP.md
git add GITHUB_SETUP.md
git commit -m "docs: verifiser GitHub mirror sync"
git push origin master

# Vent 1-8 timer (avhengig av mirror-interval)
# Sjekk deretter GitHub:
```

### Test 3: Sjekk GitHub
1. Gå til `https://github.com/DITT_BRUKERNAVN/victron-trader`
2. Bekreft at filene dukker opp
3. Sjekk at commit-historikk matcher Gitea

---

## Steg 5: Første Issues/PRs (valgfritt)

Når mirror fungerer, kan du:

1. **Aktivere Issues** på GitHub:
   - GitHub repo → Settings → General → Issues ☑️\n   - Dette lar folk rapportere bugs/foreslå features

2. **Aktivere Discussions** (forum-lignende):
   - Settings → General → Discussions ☑️

3. **Legge til topics/tags**:
   - Repo hovedside → About (gear) → Topics
   - Forslag: `victron`, `ess`, `energy-trading`, `peak-shaving`, `nordpool`, `norway`, `battery-optimization`, `modbus`

---

## Feilsøking

### Mirror feiler med "Authentication failed"
- Sjekk at token ikke er utløpt
- Generer nytt token hvis nødvendig
- Oppdater mirror-innstillingene i Gitea med nytt token

### Filene dukker ikke opp på GitHub
- Sjekk at Gitea mirror viser "Success" eller grønn hake
- Sjekk GitHub repo Settings → Actions (hvis du har workflows)
- Prøv manuell sync: Gitea → Mirror Settings → "Sync Now" (hvis tilgjengelig)

### Konflikter hvis du pushet til begge steder
- **Løsning:** Push ALLTID kun til Gitea
- GitHub er kun mirror/mottaker
- Aldri push direkte til GitHub

---

## Oppsummering: Din Workflow Etter Oppsett

| Hva | Hvor | Kommando |
|-----|------|----------|
| Daglig utvikling | Gitea (privat) | `git push origin master` |
| Offentlig synlighet | GitHub (automatisk) | Skjer automatisk hver 8. time |
| Issues/feedback | GitHub | Les og svar på https://github.com/.../issues |
| Kode-endringer | Gitea kun | Aldri push til GitHub direkte |

---

## Neste Steg Etter GitHub Publisering

1. **Overvåke første uke** — se om Issues kommer inn
2. **Vurdere MIN_PRICE_DIFF_NOK heving** — rundt 21. mai (se system memory)
3. **Observere max SOC-oppførsel** — seksjon 6.5 i SYSTEM_ANALYSIS.md
4. **Dele prosjektet** — Victron Community, Reddit r/solar, etc.

---

**Lykke til! 🚀**
