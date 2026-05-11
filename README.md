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

### Alternativ 1: Docker (anbefalt)

```bash
# 1. Klon repo
git clone https://gitea.abelgaard.no/lars/victron-trader.git
cd victron-trader

# 2. Konfigurer
cp .env.example .env
# Rediger .env med VICTRON_HOST og andre verdier

# 3. Bygg og start
docker-compose up -d

# 4. Se logger
docker-compose logs -f victron-trader

# 5. Stopp
docker-compose down
```

### Alternativ 2: Python direkte

```bash
cp .env.example .env
# Rediger .env med dine verdier
pip install -r requirements.txt
python main.py
```

## Deployment Arkitektur

**Kun lokal Docker** - dette programmet kjøres på én maskin med Docker.

**Gitea** brukes kun for kode-lagring og versjonskontroll.

### Oppsett

1. **Utvikling**: På denne maskinen (lokal Docker)
2. **Produksjon**: Samme Docker-container på samme måte
3. **Kode**: Pushet til Gitea for backup/deling

```bash
# Klon fra Gitea (eller bruk lokalt repo)
git clone https://gitea.abelgaard.no:3000/lars/victron-trader.git
cd victron-trader

# Konfigurer
cp .env.example .env
# Rediger .env med VICTRON_HOST=192.168.1.x (Cerbo GX IP)

# Bygg og kjør
docker compose up -d

# Se logger
docker compose logs -f
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
