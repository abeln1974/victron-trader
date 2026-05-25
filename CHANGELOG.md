# Changelog

Alle endringer på Victron Trader-systemet dokumenteres her.

## 2026-05-25

### Cerbo GX Firmware Oppdatering
- **Versjon:** v3.72 → v3.73
- **Server:** 192.168.1.10 (produksjon)
- **Tidspunkt:** ~20:40-20:45 CEST
- **Utført av:** Lars (med assistanse)

#### Endringer i v3.73 relevant for systemet:
- Modbus TCP: Fikset overlappende registre (870/871, 5430) - forbedret stabilitet
- Modbus TCP: Nye registre for VE.Bus effekt og relay-tester (påvirker ikke eksisterende funksjonalitet)
- Ingen endringer i ESS Mode 3 styring eller setpoint-registre (37, 2705)

#### Verifisering etter oppdatering:
- ✅ Modbus TCP port 502 tilgjengelig
- ✅ Trader får kontakt med Cerbo GX
- ✅ ESS Mode 3 (ekstern kontroll) aktiv
- ✅ Trade cycle kjører normalt (SOC 54.1%, setpoint=0W)
- ✅ Self-consume fungerer
- ✅ Ingen ERROR i logger etter reboot

### Produksjonsflytting (samme dag)
- Trader flyttet fra dev-miljø (Cascade) til produksjonsserver 192.168.1.10
- Dev-miljø stoppet og deaktivert
- SSH-nøkkel (id_abelnet) konfigurert for root@192.168.1.10

### Systemoppdateringer på 192.168.1.10
- Debian 13 (trixie) pakker oppdatert
- Docker CE: 29.4.3 → 29.5.0
- OpenSSH: sikkerhetsoppdatering (10.0p1-7+deb13u4)
- Python 3.13: 3.13.5-2+deb13u2
- tzdata: 2026b-0+deb13u1
- Trader containere restartet og verifisert healthy

## 2026-05-23

### Dokumentasjonsoppdateringer
- README: Korrekt prioritetsrekkefølge i `_control_setpoint()` (P1-P6)
- README: Fjernet sol-reserve utlading (funksjon fjernet fra optimizer)
- README: Presisert at DVCC kun styrer lading, ikke utlading
- victron_modbus.py: Oppdatert doc-streng for `set_max_charge_current()`
- main.py: Presisert DVCC-kommentar

### Bugfixes (fra logganalyse 22. mai)
- price_fetcher.py: 5-min cache på `get_prices()` for å unngå blokkerende HTTP-kall
- main.py: Export-guard bruker `_effective_discharge_kw` (muterer ikke `power_kw`)
- main.py: Peak-shaving bruker `_cached_grid_w` (ikke redundant HTTP-kall)
- solar_forecast.py: Fjernet `__import__` hack, bruker direkte `timedelta` import
- ha_qubino.py: `_fetch()` oppdaterer `_last_fetch` ved feil (unngår spam)
- ha_qubino.py: Throttle warnings til 1/min
- ha_qubino.py: Z-Wave status logger nå som DEBUG (ikke WARNING)

### Arkitektur
- `_control_setpoint()` implementert med hierarkisk prioritetsrekkefølge:
  - P1: MIN_SOC nødstopp
  - P2: Peak-shaving
  - P3: Fullt batteri eksport
  - P4/5: Arbitrasje (charge/discharge)
  - P6: Self-consume
  - Idle: Keepalive

- Sol-reserve utlading fjernet fra optimizer — natt-lading til `charge_target_soc` er tilstrekkelig
