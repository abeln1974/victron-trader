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

### Alternativ 1: Docker (anbefalt for Proxmox)

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

## Proxmox Deployment

### Container/VM Setup

Anbefalt: **LXC Container** på PVE1, PVE2, PVE3 eller PVE4:

```bash
# På Proxmox host (f.eks. PVE1)
pct create 201 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname victron-trader \
  --cores 1 \
  --memory 256 \
  --swap 256 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --storage local-zfs

pct start 201
pct exec 201 -- bash -c "apt update && apt install -y docker.io docker-compose git"

# Klon og start
pct exec 201 -- bash -c "cd /opt && git clone https://gitea.abelgaard.no/lars/victron-trader.git"
pct exec 201 -- bash -c "cd /opt/victron-trader && docker-compose up -d"
```

### Docker på eksisterende server

Hvis du har Docker på f.eks. PVE1 eller LadeFiks (10.10.10.159):

```bash
ssh root@10.10.10.159
cd /opt
git clone https://gitea.abelgaard.no:3000/lars/victron-trader.git
cd victron-trader
docker-compose up -d
```

**Viktig**: Containeren må ha nettverkstilgang til Cerbo GX (192.168.1.x). Bruk `network_mode: host` i docker-compose.yml.

## Gitea (Abelgard)

```bash
git remote add origin http://gitea.abelgaard.no:3000/lars/victron-trader.git
# eller SSH: gitea.abelgaard.no:3000/lars/victron-trader.git

# Token for autentisering (hvis ikke SSH)
# git config http.extraHeader "Authorization: token 3c4843dc5ac4d93525bd2fe90d8eddac133592ad"

git add .
git commit -m "Initial"
git push -u origin main
