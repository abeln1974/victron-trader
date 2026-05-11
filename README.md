# Victron Energy Trader

Automatisk strømhandel med Victron ESS. Kjøper strøm når den er billig, selger (bruker fra batteri) når den er dyr.

**⚠️ Viktig for Abelgard-oppsett**: DESS (Dynamic ESS) er aktiv på din Cerbo GX. DESS kolliderer med ekstern ESS-styring via MQTT. Du må enten:
1. Deaktivere DESS i VRM/Venus OS før du bruker dette programmet, ELLER
2. Bruke VRM API istedenfor MQTT (modifisert versjon)

## Arkitektur

- **price_fetcher**: Henter spotpriser fra hvakosterstrommen.no
- **optimizer**: Beregner optimal lade/utlade-plan
- **victron_mqtt**: Styrer ESS via MQTT mot Cerbo GX
- **profit_tracker**: Logger inntjening og ytelse

## Oppsett

```bash
cp .env.example .env
# Rediger .env med dine verdier
pip install -r requirements.txt
python main.py
```

## Gitea (Abelgard)

```bash
git remote add origin http://gitea.abelgaard.no:3000/lars/victron-trader.git
# eller SSH: gitea.abelgaard.no:3000/lars/victron-trader.git

# Token for autentisering (hvis ikke SSH)
# git config http.extraHeader "Authorization: token 3c4843dc5ac4d93525bd2fe90d8eddac133592ad"

git add .
git commit -m "Initial"
git push -u origin main
```
