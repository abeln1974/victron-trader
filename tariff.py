"""Beregning av reell kjøps- og salgspris for Abelgard.

Basert på Kraftriket Solstrøm-faktura april 2026:

KJØP (per kWh inkl mva):
  Spotpris           (variabel, eks mva)
  + Kraftriket påslag   6.50 øre  eks mva
  + Nettleie dag       20.63 øre  eks mva (06:00-22:00)
  + Nettleie natt      12.50 øre  eks mva (22:00-06:00)
  + Forbruksavgift      8.91 øre  eks mva
  + Enova               1.25 øre  eks mva
  × 1.25 (25% mva)
  - Norgespris         96.53 øre  INGEN mva (statlig støtte, trekkes fra nettleie)
  = Total reell innkjøpspris

SALG som plusskunde (ingen mva):
  Kraftriket betaler flat 75.00 øre/kWh uavhengig av spot
  Nettselskap betaler -6.25 øre/kWh for produsert energi (nettleie tilbake)
  → Netto salgspris: 75.00 - 6.25 = 68.75 øre/kWh

KAPASITETSLEDD (Elvia) - KRITISK:
  Trinn 0-2A:    175 kr/mnd
  Trinn 2-5A:    305 kr/mnd
  Trinn 5-10A:   475 kr/mnd
  Trinn 10-15A:  662.50 kr/mnd  ← Du er her (maks avlest 12.69 kW)
  Trinn 15-20A:  887.50 kr/mnd
  Trinn 20-25A: 1137.50 kr/mnd

  Beregning: Gjennomsnittet av de 3 høyeste enkelt-timer per mnd.
  Du hadde 12.69 kW som høyeste time → nær 15A grensen (3.45kW/fase × 3 = 10.35kW 3-fas)
  Elvia bruker fastopp: 15A × 230V × 3 faser = 10.35 kW

  → Batteriet kan UNNGÅ at du går opp til neste trinn (887.50 kr) = 225 kr spart!
"""
import os
from datetime import datetime
from config import CONFIG

# Konstanter fra faktura (alle eks mva)
SUPPLIER_MARKUP_ORE  = float(os.getenv("SUPPLIER_MARKUP_ORE",   "6.50"))
GRID_TARIFF_DAY_ORE  = float(os.getenv("GRID_TARIFF_DAY_ORE",  "20.63"))
GRID_TARIFF_NIGHT_ORE= float(os.getenv("GRID_TARIFF_NIGHT_ORE","12.50"))
CONSUMPTION_TAX_ORE  = float(os.getenv("CONSUMPTION_TAX_ORE",   "8.91"))
ENOVA_ORE            = float(os.getenv("ENOVA_ORE",              "1.25"))
NORGES_PRICE_ORE     = float(os.getenv("NORGES_PRICE_ORE",      "96.53"))  # Statlig støtte, ingen mva
CAPACITY_CHARGE_NOK  = float(os.getenv("CAPACITY_CHARGE_NOK",  "662.50"))
SELL_PRICE_ORE       = float(os.getenv("SELL_PRICE_ORE",        "75.00"))  # Kraftriket betaler eks mva
NET_SELL_BACK_ORE    = float(os.getenv("NET_SELL_BACK_ORE",      "6.25"))  # Nettselskap betaler tilbake
DAY_TARIFF_START     = int(os.getenv("DAY_TARIFF_START",            "6"))
DAY_TARIFF_END       = int(os.getenv("DAY_TARIFF_END",             "22"))

# Kapasitetstrinn (Elvia, 2026)
CAPACITY_TIERS = [
    (2,   175.00),
    (5,   305.00),
    (10,  475.00),
    (15,  662.50),
    (20,  887.50),
    (25, 1137.50),
    (63, 1537.50),
]

VAT = CONFIG.vat  # 1.25


def is_day_tariff(hour: int) -> bool:
    """Returner True hvis dag-tariff gjelder."""
    return DAY_TARIFF_START <= hour < DAY_TARIFF_END


def buy_price_ore(spot_ore: float, hour: int) -> float:
    """
    Beregn total reell innkjøpspris i øre/kWh inkl mva og etter Norgespris-støtte.

    spot_ore: Spotpris i øre eks mva (fra hvakosterstrommen.no)
    hour: Time på dagen (0-23) for riktig nettariff
    """
    grid = GRID_TARIFF_DAY_ORE if is_day_tariff(hour) else GRID_TARIFF_NIGHT_ORE

    # Strøm + nettleie + avgifter (eks mva), deretter mva
    total_inkl_mva = (spot_ore + SUPPLIER_MARKUP_ORE + grid + CONSUMPTION_TAX_ORE + ENOVA_ORE) * VAT

    # Norgespris-støtte trekkes fra ETTER mva (ingen mva på støtten)
    return total_inkl_mva - NORGES_PRICE_ORE


def sell_price_ore() -> float:
    """
    Netto salgspris i øre/kWh:
    - Kraftriket betaler 75 øre/kWh (ingen mva for privatperson)
    - Nettselskap betaler tilbake 6.25 øre/kWh for produsert energi
    → 75.00 - 6.25 = 68.75 øre/kWh netto
    """
    return SELL_PRICE_ORE - NET_SELL_BACK_ORE


def capacity_charge_for_kw(peak_kw: float) -> float:
    """Returner kapasitetsledd for gitt toppeffekt (kW)."""
    amps = peak_kw / (0.230 * 3)  # 3-fase 230V
    for limit_a, charge_nok in CAPACITY_TIERS:
        if amps <= limit_a:
            return charge_nok
    return CAPACITY_TIERS[-1][1]


def peak_reduction_savings(current_peak_kw: float, reduced_peak_kw: float) -> float:
    """Beregn månedlig besparelse ved å redusere toppeffekt."""
    current_charge = capacity_charge_for_kw(current_peak_kw)
    reduced_charge = capacity_charge_for_kw(reduced_peak_kw)
    return current_charge - reduced_charge


def profit_per_kwh_ore(spot_ore: float, hour: int) -> float:
    """
    Beregn netto fortjeneste per kWh ved å:
    1. Kjøpe strøm til spotpris (lage batteri)
    2. Selge tilbake til fast 75 øre

    Negativ = tap
    """
    buy = buy_price_ore(spot_ore, hour)
    sell = sell_price_ore()
    return sell - buy


def should_charge(spot_ore: float, hour: int, min_profit_ore: float = 20.0) -> bool:
    """
    Skal vi lade batteriet nå?
    Gir mening å lade når innkjøpspris er lav nok til at salg gir profitt.
    """
    # Kjøp er lønnsomt hvis vi kan selge med margin
    # inkl batteritap (efficiency)
    effective_sell = sell_price_ore() * CONFIG.battery_efficiency
    return buy_price_ore(spot_ore, hour) < (effective_sell - min_profit_ore)


def should_discharge(spot_ore: float, hour: int) -> bool:
    """
    Skal vi utlade batteriet nå?
    Utlading er lønnsomt hvis salgsprisen > det vi betalte for å lade.
    Siden vi bruker fast 75 øre som salgspris er dette alltid likt,
    men vi sjekker at vi faktisk sparer noe vs å kjøpe fra grid.
    """
    current_buy = buy_price_ore(spot_ore, hour)
    # Utlading sparer oss for å kjøpe fra grid
    return current_buy > sell_price_ore()


def format_prices(spot_ore: float, hour: int) -> str:
    """Human-readable prisinfo."""
    buy = buy_price_ore(spot_ore, hour)
    sell = sell_price_ore()
    profit = sell - buy
    tariff = "dag" if is_day_tariff(hour) else "natt"

    return (
        f"Spot: {spot_ore:.1f} øre | "
        f"Kjøp ({tariff}): {buy:.1f} øre inkl mva | "
        f"Salg: {sell:.1f} øre | "
        f"Margin: {profit:+.1f} øre"
    )


if __name__ == "__main__":
    print("=== Prisanalyse Abelgard (Kraftriket + Elvia) ===\n")

    test_prices = [
        (10,  3, "Natt, veldig billig"),
        (30,  3, "Natt, billig"),
        (60,  3, "Natt, middels"),
        (100, 10, "Dag, middels"),
        (146, 10, "Dag, april-snitt"),
        (200, 17, "Ettermiddag, dyrt"),
        (300, 17, "Ettermiddag, veldig dyrt"),
    ]

    print(f"{'Scenario':<30} {'Kjøp (reell)':>14} {'Salg':>8} {'Margin':>10} {'Beslutning':>12}")
    print("-" * 80)
    for spot, hour, label in test_prices:
        buy = buy_price_ore(spot, hour)
        sell = sell_price_ore()
        margin = sell - buy
        beslutning = "✅ Utlade" if should_discharge(spot, hour) else "🔋 Lade" if should_charge(spot, hour) else "⏸️ Vent"
        print(f"{label:<30} {buy:>12.1f}ø  {sell:>6.1f}ø  {margin:>+9.1f}ø  {beslutning:>12}")

    print(f"\n--- Kapasitetsledd ---")
    print(f"Din nåværende toppeffekt: 12.69 kW → trinn 10-15A = {CAPACITY_CHARGE_NOK:.2f} kr/mnd")
    print(f"Neste trinn (15-20A):  {capacity_charge_for_kw(11):.2f} kr/mnd")
    savings = peak_reduction_savings(12.69, 9.5)
    print(f"Spare ved å holde under 10.35 kW: {savings:.2f} kr/mnd")
    print(f"\nNorgespris-støtte: {NORGES_PRICE_ORE} øre/kWh trekkes fra din regning (ingen mva)")
    print(f"Netto salgspris: {sell_price_ore():.2f} øre/kWh (75 - 6.25 nettselskap)")
