# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Infrastruktur

- **Docker-server**: 192.168.1.10 (root-tilgang via `~/.ssh/id_abelnet`)
- **Cerbo GX**: 192.168.1.60:502 (Modbus-TCP, unit IDs: 100=system, 226=SmartShunt, 227=VE.Bus)
- **EVCS**: 192.168.1.45:502 (Victron EV Charging Station, direkte Modbus TCP)
- **Home Assistant**: 192.168.1.34 / https://homeassistant.abelgaard.no (kun Qubino grid-måler)
- **Persistent data på Docker-server**: `/opt/victron-trader/data/` og `/opt/victron-trader/logs/`
- **Gitea**: https://gitea.abelgaard.no/lars/victron-trader

Python-filene er **bakt inn i Docker-imaget** (ikke mountet som volum). Endringer krever rebuild:

```bash
# Deploy endringer permanent
ssh -i ~/.ssh/id_abelnet root@192.168.1.10 "cd /opt/victron-trader && docker compose build && docker compose up -d"

# Rask midlertidig deploy (forsvinner ved rebuild)
scp -i ~/.ssh/id_abelnet <fil>.py root@192.168.1.10:/opt/victron-trader/<fil>.py
ssh -i ~/.ssh/id_abelnet root@192.168.1.10 "docker cp /opt/victron-trader/<fil>.py victron-trader:/app/<fil>.py && docker compose restart victron-trader"
```

## Kjøre og teste

```bash
# Kjør tester uten ekte Victron-tilkobling
docker compose run --rm victron-trader python mock_test.py

# Kjør enkelttest (funksjoner i mock_test.py: test_price_fetching, test_optimizer, test_profit_tracking, test_full_mock, test_trade_logging_integration)
docker compose run --rm victron-trader python -c "from mock_test import test_optimizer; test_optimizer()"

# Les alle Modbus-registre fra Cerbo GX (diagnostikk)
docker compose run --rm victron-trader python observe.py

# Grid-måler sammenligning (Qubino vs VM-3P75CT)
docker exec victron-trader python3 /app/grid_analysis.py sample
```

```bash
# Logg-overvåking
ssh -i ~/.ssh/id_abelnet root@192.168.1.10 "docker logs victron-trader --tail=50 -f"
ssh -i ~/.ssh/id_abelnet root@192.168.1.10 "docker logs victron-trader 2>&1 | grep -E 'Trade cycle|Action:|Storm|ERROR|WARNING'"
```

## Arkitektur

### Kontrollflyt

`main.py` (`EnergyTrader`) eier Cerbo GX via **ESS Mode 3** (ekstern kontroll) kontinuerlig. Keepalive sendes hvert 8s — hvis den stopper, går Victron tilbake til Mode 2 etter ~60s (crash-safe).

Hovedloopen kjører med `time.sleep(1)` og tre frekvenser:
- **Hvert 60. min** (`_execute_trade_cycle`): henter priser, beregner optimizer-plan, setter `current_action`
- **Hvert 10s** (`_control_setpoint` + `_check_peak_shaving`): leser sanntidsdata, velger setpoint basert på prioritetsrekkefølge P1–P6
- **Hvert 8s**: keepalive til Cerbo GX

### Prioritetsrekkefølge for setpoint (P1 overstyrer P6)

| Prioritet | Betingelse | Setpoint |
|-----------|-----------|---------|
| P1 | SOC ≤ MIN_SOC | Stopp utlading |
| P2 | Grid > 9.5 kW | Peak-shaving (utlad batteri) |
| P3 | SOC ≥ 90% og sol > 200W | Eksporter sol til nett |
| P4/5 | Arbitrasje-time aktiv | Lad/utlad per optimizer-plan |
| P6 | Dagtid, SOC > lademål+1%, grid > 0.15 kW | Self-consume (setpoint 0W) |
| — | Ellers | Idle (keepalive 0W) |

### DVCC vs ESS setpoint

Dette er den viktigste distinksjonen i hele systemet:
- **ESS setpoint** (reg 2716/2717): styrer **utlading** — negativt = batteri leverer til hus/nett
- **DVCC** (reg 2705): styrer kun **maks ladestrøm** inn i batteri — `0A` stopper lading, `-1` frigjør
- Fronius Primo er AC-koblet på AC-out, **ikke** DC-bussen — DVCC 0A stopper batteriladingen men Fronius kan fortsatt eksportere til nett

### Dynamisk lademål (`charge_target_soc`)

Open-Meteo sol-prognose for i morgen bestemmer lademålet:
- Høy prognose → lavt lademål (spar plass til sol)
- Lav prognose → høyt lademål (storm mode: MIN_SOC 45%, lad til 90%)
- Brukes i: natt-lading stopper her, DVCC settes til 0A ved SOC ≥ target, self-consume stopper ved SOC ≤ target+1%

### Modbus unit IDs (Abelgård-spesifikt)

| Unit ID | Enhet | Viktige registre |
|---------|-------|-----------------|
| 100 | Cerbo GX system | 266=SOC, 820=grid L1, 850=PV, 2716=setpoint, 2705=DVCC |
| 226 | SmartShunt | 266=SOC (autoritativ), 309=discharge kWh, 310=charge kWh |
| 227 | VE.Bus | 37=Hub4Mode (2=Optimized, 3=ExternalControl) |
| 1 | EVCS | 5015=status, 5009=set_current, 5012=power |

### Grid-måler (to kilder)

`ha_qubino.py::QubinoReader` er primær (alle 3 faser, ~10s oppdatering via HA).
Fallback: `victron_modbus.py::get_grid_power()` via Cerbo GX (mangler L3 på IT-nett).
`_get_grid_power()` i `main.py` kombinerer begge med timeout-håndtering.

### Feilhåndtering

- **ESS modus er 2**: Victron GX resetter av og til til Mode 2 — systemet oppdager og korrigerer ved neste syklus. Normal oppførsel.
- **EVCS Broken pipe**: EVCS lukker Modbus-forbindelsen. Reconnect-backoff på 60s + `_client.close()` før ny tilkobling (se `_ensure_connected` i `ha_qubino.py`).
- **Pris-API nede**: `price_fetcher.py` prøver hvakosterstrommen.no, deretter Nordpool direkte. Hvis begge feiler, krasjer trade cycle — systemet fortsetter på forrige plan til neste time.
- **HA nede**: Qubino er utilgjengelig, grid-lesing faller tilbake til Victron Modbus.
