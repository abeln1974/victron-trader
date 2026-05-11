"""Beregning av reell kjøps- og salgspris for Abelgard.

Basert på Kraftriket Solstrøm-faktura april 2026:

KJØP (per kWh inkl mva):
  Spotpris           (variabel)
  + Kraftriket påslag   6.50 øre
  + Nettleie dag       20.63 øre  (06:00-22:00)
  + Nettleie natt      12.50 øre  (22:00-06:00)
  + Forbruksavgift      8.91 øre
  + Enova               1.25 øre
  × 1.25 (25% mva)
  = Total innkjøpspris

SALG (per kWh, ingen mva for privatperson):
  Spotpris × 0.75 (Kraftriket betaler 75 øre/kWh flat)
  ... nei: fakturaen viser flat 75.00 øre/kWh uavhengig av spot

KAPASITETSLEDD (Elvia):
  662.50 kr/mnd for 10-15A trinn
  → Unngå å trekke mer enn 15A (3.45kW per fase) i snitt per time
"""
import os
from datetime import datetime
from config import CONFIG

# Konstanter fra faktura (alle eks mva)
SUPPLIER_MARKUP_ORE = float(os.getenv("SUPPLIER_MARKUP_ORE", "6.50"))
GRID_TARIFF_DAY_ORE = float(os.getenv("GRID_TARIFF_DAY_ORE", "20.63"))
GRID_TARIFF_NIGHT_ORE = float(os.getenv("GRID_TARIFF_NIGHT_ORE", "12.50"))
CONSUMPTION_TAX_ORE = float(os.getenv("CONSUMPTION_TAX_ORE", "8.91"))
ENOVA_ORE = float(os.getenv("ENOVA_ORE", "1.25"))
CAPACITY_CHARGE_NOK = float(os.getenv("CAPACITY_CHARGE_NOK", "662.50"))
SELL_PRICE_ORE = float(os.getenv("SELL_PRICE_ORE", "75.00"))
DAY_TARIFF_START = int(os.getenv("DAY_TARIFF_START", "6"))
DAY_TARIFF_END = int(os.getenv("DAY_TARIFF_END", "22"))

VAT = CONFIG.vat  # 1.25


def is_day_tariff(hour: int) -> bool:
    """Returner True hvis dag-tariff gjelder."""
    return DAY_TARIFF_START <= hour < DAY_TARIFF_END


def buy_price_ore(spot_ore: float, hour: int) -> float:
    """
    Beregn total innkjøpspris i øre/kWh inkl mva.

    spot_ore: Spotpris i øre eks mva (fra hvakosterstrommen.no)
    hour: Time på dagen (0-23) for riktig netttariff
    """
    grid = GRID_TARIFF_DAY_ORE if is_day_tariff(hour) else GRID_TARIFF_NIGHT_ORE

    # Alt eks mva summeres
    total_eks_mva = spot_ore + SUPPLIER_MARKUP_ORE + grid + CONSUMPTION_TAX_ORE + ENOVA_ORE

    # Mva på alt
    return total_eks_mva * VAT


def sell_price_ore() -> float:
    """
    Salgspris i øre/kWh (ingen mva for privatperson/plusskunde).
    Kraftriket betaler fast 75 øre/kWh uavhengig av spotpris.
    """
    return SELL_PRICE_ORE


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
    print("=== Prisanalyse Abelgard ===\n")

    # Simuler typiske spotpriser
    test_prices = [
        (30, 3, "Natt, billig"),
        (60, 3, "Natt, middels"),
        (100, 10, "Dag, middels"),
        (146, 10, "Dag, april-snitt"),
        (200, 17, "Ettermiddag, dyrt"),
        (300, 17, "Ettermiddag, veldig dyrt"),
    ]

    print(f"{'Scenario':<30} {'Kjøp':>12} {'Salg':>10} {'Margin':>10} {'Lønnsomt?':>12}")
    print("-" * 80)
    for spot, hour, label in test_prices:
        buy = buy_price_ore(spot, hour)
        sell = sell_price_ore()
        margin = sell - buy
        lønnsomt = "✅ Utlade" if should_discharge(spot, hour) else "🔋 Lade" if should_charge(spot, hour) else "⏸️ Vent"
        print(f"{label:<30} {buy:>10.1f}ø  {sell:>8.1f}ø  {margin:>+9.1f}ø  {lønnsomt:>12}")

    print(f"\nKapasitetsledd: {CAPACITY_CHARGE_NOK:.2f} kr/mnd")
    print(f"Batteri-effektivitet: {CONFIG.battery_efficiency*100:.0f}%")
    print(f"\nKonklusjon: Med fast salgspris på {SELL_PRICE_ORE} øre er utlading")
    print(f"lønnsomt når spotpris > ~{(SELL_PRICE_ORE/VAT - SUPPLIER_MARKUP_ORE - GRID_TARIFF_DAY_ORE - CONSUMPTION_TAX_ORE - ENOVA_ORE):.0f} øre eks mva (dag)")
