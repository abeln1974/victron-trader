# Lokal Docker Testing

Test victron-trader på din lokale maskin.

## 1. Installer Docker (hvis ikke installert)

```bash
# Ubuntu/Debian - Quick install
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Logg ut og inn igjen (eller: newgrp docker)

# Verifiser
docker --version  # skal vise 20.x eller nyere
docker compose version  # skal vise v2.x
```

## Quick Test (uten å bygge container)

```bash
cd /home/lars/CascadeProjects/windsurf-project

# 1. Installer Python-avhengigheter direkte (test uten Docker)
pip install -r requirements.txt

# 2. Kjør mock-test (ingen Victron nødvendig)
python mock_test.py

# Forventet output:
# ✅ Hentet 24 priser
# ✅ Generert plan med 24 timer
# ✅ Trades i dag: ...
# ✅ ALLE TESTER BESTÅTT
```

## 2. Bygg og kjør lokalt

```bash
cd /home/lars/CascadeProjects/windsurf-project

# Kopier og rediger config
cp .env.example .env
# VIKTIG: Endre VICTRON_HOST til noe ugyldig for testing
# f.eks. VICTRON_HOST=192.168.99.99 (så den ikke kobler til ekte Victron)

# Bygg container
docker compose build

# Kjør i test-modus (vil feile på Modbus, men tester priser og optimalisering)
docker compose up

# Se logger
docker compose logs -f
```

## 3. Test kun pris-henting (uten Victron)

```bash
# Kjør price_fetcher isolert
docker compose run --rm victron-trader python price_fetcher.py
```

Forventet output:
```
12:00: 45.2 øre (0.565 kr)
13:00: 38.5 øre (0.481 kr)
...
```

## 4. Test optimalisering (mock Victron)

```bash
# Kjør optimizer test
docker compose run --rm victron-trader python optimizer.py
```

## 5. Full test med mock

For å teste hele flyten uten ekte Victron, endre `main.py` midlertidig:

```python
# I EnergyTrader.start(), kommenter ut:
# if not self.victron.connect():
#     logger.error("Failed to connect...")
#     sys.exit(1)

# Legg til mock:
logger.info("TEST MODE: Using mock Victron")
self.victron = MockVictronModbus()
```

## 6. Rens opp etter test

```bash
# Stopp container
docker compose down

# Slett volumer (data)
docker compose down -v

# Slett images
docker rmi victron-trader-victron-trader
```

## Når alt fungerer lokalt

Push til Gitea:
```bash
git push origin master
```
