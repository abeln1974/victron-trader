# Victron Energy Trader

Automatisk strømhandel med Victron ESS. Kjøper strøm når den er billig, selger (bruker fra batteri) når den er dyr.

**✅ Modbus-TCP versjon** - Industristandard protokoll, raskere og mer stabil enn MQTT.

**⚠️ Viktig for Abelgard-oppsett**: DESS (Dynamic ESS) må deaktiveres for ekstern styring:
1. Gå til VRM → Site 411797 → Settings → ESS
2. Skru av "Dynamic ESS"
3. Sett ESS Mode til "External control" (eller behold "Optimized" for Mode 2)

## Arkitektur

- **price_fetcher**: Henter spotpriser fra hvakosterstrommen.no
- **optimizer**: Beregner optimal lade/utlade-plan (48kWh-optimert for Abelgard)
- **victron_modbus**: Styrer ESS via Modbus-TCP (ESS Mode 2 - Grid Setpoint)
- **vrm_api**: Backup for monitoring via VRM API
- **profit_tracker**: SQLite-logging av inntjening

## Modbus-TCP vs MQTT

| Feature | Modbus-TCP | MQTT |
|---------|-----------|------|
| Responstid | ~100ms | ~1s |
| Keep-alive | Nei | Ja (krever broker) |
| Industristandard | ✅ | Nei |
| Oppsett | Enkelt | Krever MQTT broker |
| VRM korrekt | Nei | Nei |

**Valg for Abelgard**: Modbus-TCP (port 502) direkte til Cerbo GX.

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
