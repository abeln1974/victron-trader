"""
Komplett lønnsomhetsanalyse for Abelgard batteri-trading.

Basert på:
- Kraftriket Solstrøm-faktura april 2026
- 48 kWh Farco NMC batteri (4x12kWh)
- 2x MultiPlus-II 48/5000 (10 kW max)
- Forbruk: 1761.84 kWh i april (1 mnd)
- Prisområde: NO1
"""
from tariff import (
    buy_price_ore, sell_price_ore, should_charge, should_discharge,
    capacity_charge_for_kw, peak_reduction_savings,
    CAPACITY_CHARGE_NOK, NORGES_PRICE_ORE, SELL_PRICE_ORE,
    GRID_TARIFF_DAY_ORE, GRID_TARIFF_NIGHT_ORE,
    CONSUMPTION_TAX_ORE, ENOVA_ORE, SUPPLIER_MARKUP_ORE, VAT
)
from config import CONFIG

# ─────────────────────────────────────────────────────────
# KOSTNADER - BATTERISLITASJE OG DRIFT
# ─────────────────────────────────────────────────────────

# Farco 12kWh NMC batteri (4 stk = 48kWh)
# NMC-kjemi: typisk 2000-3000 sykluser til 80% kapasitet
BATTERY_COST_NOK        = 60_000    # 4x Farco 12kWh à 15 000 kr (tilbudspris)
BATTERY_CYCLES_LIFETIME = 2500      # Konservativt for NMC
BATTERY_KWH_USABLE      = 48 * (0.95 - 0.50)  # 22.8 kWh brukbart per syklus

# Kostnad per kWh gjennomstrømmet (sykluskostnad)
# En "syklus" = lade 22.8 kWh inn og ut
BATTERY_COST_PER_CYCLE  = BATTERY_COST_NOK / BATTERY_CYCLES_LIFETIME
BATTERY_COST_PER_KWH    = BATTERY_COST_PER_CYCLE / BATTERY_KWH_USABLE  # kr/kWh

# MultiPlus-II 48/5000 (2 stk)
INVERTER_COST_NOK       = 30_000    # Estimert 2x MultiPlus-II
INVERTER_LIFETIME_YEARS = 15        # Typisk levetid
INVERTER_COST_YEARLY    = INVERTER_COST_NOK / INVERTER_LIFETIME_YEARS

# Server/Raspberry Pi for trading-programmet
SERVER_COST_NOK         = 1_500     # RPi5 eller lignende
SERVER_LIFETIME_YEARS   = 5
SERVER_POWER_W          = 10        # Watt i drift
SERVER_KWH_YEARLY       = SERVER_POWER_W * 24 * 365 / 1000
# Strøm til server (reell kostnad etter Norgespris, ca 0.50 kr/kWh snitt)
SERVER_POWER_COST_YEARLY = SERVER_KWH_YEARLY * 0.50
SERVER_FIXED_COST_YEARLY = SERVER_COST_NOK / SERVER_LIFETIME_YEARS + SERVER_POWER_COST_YEARLY

# Vedlikehold (BMS-sjekk, tilkoblinger, etc.)
MAINTENANCE_YEARLY_NOK  = 500       # Konservativt estimat

# ─────────────────────────────────────────────────────────
# DINE TALL (fra april 2026-faktura)
# ─────────────────────────────────────────────────────────
MONTHLY_CONSUMPTION_KWH = 1761.84    # kWh/mnd totalt forbruk
DAY_CONSUMPTION_KWH     = 816.09     # kWh dag (06-22)
NIGHT_CONSUMPTION_KWH   = 945.75     # kWh natt (22-06)
CURRENT_PEAK_KW         = 12.69      # Høyeste enkelt-time (kW)
APRIL_SPOT_AVG_ORE      = 146.53     # Snitt spotpris april (eks mva)
MONTHLY_SOLAR_KWH       = 25.50      # kWh solgt som plusskunde (sol-produksjon)

# Batteri
BATTERY_KWH             = CONFIG.battery_capacity_kwh   # 48 kWh
BATTERY_KW              = CONFIG.battery_max_charge_kw  # 10 kW
EFFICIENCY              = CONFIG.battery_efficiency      # 0.95
MIN_SOC                 = CONFIG.min_soc / 100          # 0.50
MAX_SOC                 = CONFIG.max_soc / 100          # 0.95
USABLE_KWH              = BATTERY_KWH * (MAX_SOC - MIN_SOC)  # 22.8 kWh tilgjengelig

# ─────────────────────────────────────────────────────────
# 1. NÅSITUASJON (uten batteri-trading)
# ─────────────────────────────────────────────────────────
def current_monthly_cost():
    """Beregn hva du betaler i dag uten smart batteristyring."""
    # Strøm fra Kraftriket
    buy_day   = DAY_CONSUMPTION_KWH   * buy_price_ore(APRIL_SPOT_AVG_ORE, 10) / 100
    buy_night = NIGHT_CONSUMPTION_KWH * buy_price_ore(APRIL_SPOT_AVG_ORE, 3)  / 100

    # Solgt (plusskunde)
    sold = MONTHLY_SOLAR_KWH * sell_price_ore() / 100

    # Kapasitetsledd
    capacity = CAPACITY_CHARGE_NOK

    return {
        'buy_day_nok':    buy_day,
        'buy_night_nok':  buy_night,
        'sold_nok':       sold,
        'capacity_nok':   capacity,
        'total_nok':      buy_day + buy_night - sold + capacity,
    }


# ─────────────────────────────────────────────────────────
# 2. MED SMART BATTERISTYRING (scenarios)
# ─────────────────────────────────────────────────────────
def scenario_arbitrage():
    """
    Scenario A: Pris-arbitrasje
    Lad billig om natten (spot ~30 øre), utlad dyrt om dagen (spot ~146 øre).
    Realistisk: 1 full syklus per dag = 22.8 kWh/dag.
    """
    cycles_per_day   = 0.8   # Konservativt - ikke alltid nok prisgap
    days_per_month   = 30
    kwh_per_cycle    = USABLE_KWH * EFFICIENCY  # 21.66 kWh ut per syklus

    # Typisk natt: lade kl 02-05 (spot 30 øre), utlade kl 17-20 (spot 146 øre)
    charge_price_ore   = buy_price_ore(30, 3)    # Natt, billig spot
    discharge_save_ore = buy_price_ore(146, 17)  # Dag, dyrt - spart ved å ikke kjøpe

    # Gevinst per syklus: vi sparer "dyrt kjøp" men betalte "billig kjøp"
    # + batter-tap tas ut fra efficiency
    charge_cost_per_kwh  = charge_price_ore / 100    # kr/kWh inn
    discharge_save_per_kwh = discharge_save_ore / 100  # kr/kWh spart

    gain_per_kwh = discharge_save_per_kwh - (charge_cost_per_kwh / EFFICIENCY)
    monthly_gain = gain_per_kwh * kwh_per_cycle * cycles_per_day * days_per_month

    return {
        'label':              'A: Pris-arbitrasje (lad natt, utlad dag)',
        'charge_price_ore':   charge_price_ore,
        'discharge_save_ore': discharge_save_ore,
        'gain_per_kwh_nok':   gain_per_kwh,
        'kwh_cycled_monthly': kwh_per_cycle * cycles_per_day * days_per_month,
        'monthly_gain_nok':   monthly_gain,
        'annual_gain_nok':    monthly_gain * 12,
    }


def scenario_peak_shaving():
    """
    Scenario B: Peak-shaving (kapasitetsledd-reduksjon)
    Batteriet kutter toppbelastning slik at du holder deg under 10.35 kW.
    Du hadde 12.69 kW topp - batteriet kan enkelt klippe dette.
    """
    # Nåværende trinn: 662.50 kr/mnd
    # Ned til 10 kW-trinn: 475 kr/mnd
    # Beregning: snitt av 3 høyeste timer avgjør trinn
    current_charge  = capacity_charge_for_kw(CURRENT_PEAK_KW)   # 662.50
    target_peak_kw  = 10.0   # Hold under 10.35 kW (10A-trinn)
    reduced_charge  = capacity_charge_for_kw(target_peak_kw)     # 475.00

    monthly_saving  = current_charge - reduced_charge
    # Peak-shaving krever lite batteri (2-3 kWh per hendelse, 10 kW i maks 1 time)

    return {
        'label':              'B: Peak-shaving (kapasitetsledd ned)',
        'current_tier_nok':   current_charge,
        'target_tier_nok':    reduced_charge,
        'target_peak_kw':     target_peak_kw,
        'monthly_saving_nok': monthly_saving,
        'annual_saving_nok':  monthly_saving * 12,
    }


def scenario_combined():
    """
    Scenario C: Kombinert arbitrasje + peak-shaving (realistisk best case)
    """
    arb  = scenario_arbitrage()
    peak = scenario_peak_shaving()
    monthly = arb['monthly_gain_nok'] + peak['monthly_saving_nok']
    return {
        'label':              'C: Kombinert (arbitrasje + peak-shaving)',
        'arbitrage_nok':      arb['monthly_gain_nok'],
        'peak_saving_nok':    peak['monthly_saving_nok'],
        'monthly_total_nok':  monthly,
        'annual_total_nok':   monthly * 12,
    }


# ─────────────────────────────────────────────────────────
# 3. ROI-BEREGNING
# ─────────────────────────────────────────────────────────
def roi_analysis(annual_gain_nok: float):
    """
    Enkel ROI - du har allerede batteriet og invertere (ESS).
    Kostnaden er kun programvare/strøm til server.
    """
    # Anslått kostnad
    server_power_w       = 20     # Watts (Raspberry Pi / liten PC)
    server_kwh_monthly   = server_power_w * 24 * 30 / 1000   # 14.4 kWh/mnd
    server_cost_monthly  = server_kwh_monthly * 1.5           # ~21 kr/mnd (1.5 kr/kWh snitt)
    software_cost_yearly = 0      # Open source

    annual_cost = server_cost_monthly * 12 + software_cost_yearly
    net_annual  = annual_gain_nok - annual_cost

    return {
        'server_monthly_nok': server_cost_monthly,
        'annual_cost_nok':    annual_cost,
        'annual_gain_nok':    annual_gain_nok,
        'net_annual_nok':     net_annual,
        'payback_months':     0,  # Batteri allerede betalt
    }


# ─────────────────────────────────────────────────────────
# 4. SESONGVARIASJON
# ─────────────────────────────────────────────────────────
# Historiske NO1 spotpris-snitt (estimert)
SEASONAL_SPOT = {
    'Jan': 120, 'Feb': 110, 'Mar': 90,  'Apr': 80,
    'Mai': 60,  'Jun': 50,  'Jul': 45,  'Aug': 55,
    'Sep': 75,  'Okt': 100, 'Nov': 130, 'Des': 150,
}


def seasonal_analysis():
    """Beregn månedlig gevinst basert på sesongpriser."""
    results = {}
    for month, spot in SEASONAL_SPOT.items():
        charge_ore = buy_price_ore(max(spot * 0.4, 10), 3)  # Lad til 40% av snitt, natt
        discharge_ore = buy_price_ore(spot * 1.3, 17)        # Utlad ved 130% av snitt

        gain_per_kwh = (discharge_ore - charge_ore / EFFICIENCY) / 100
        monthly_kwh  = USABLE_KWH * EFFICIENCY * 0.8 * 30
        monthly_gain = gain_per_kwh * monthly_kwh + scenario_peak_shaving()['monthly_saving_nok']
        results[month] = monthly_gain
    return results


# ─────────────────────────────────────────────────────────
# MAIN OUTPUT
# ─────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  LØNNSOMHETSANALYSE - ABELGARD BATTERI-TRADING")
    print("=" * 65)

    # ── Systeminfo ──
    print(f"""
SYSTEM:
  Batteri:       {BATTERY_KWH} kWh Farco NMC (2x12kWh × 2)
  Inverter:      2× MultiPlus-II 48/5000 ({BATTERY_KW:.0f} kW)
  Brukbart:      {USABLE_KWH:.1f} kWh ({MIN_SOC*100:.0f}%-{MAX_SOC*100:.0f}% SOC)
  Effektivitet:  {EFFICIENCY*100:.0f}% round-trip
  Leverandør:    Kraftriket Solstrøm
  Nett:          Elvia, prisområde NO1
""")

    # ── Nåsituasjon ──
    cost = current_monthly_cost()
    print("─" * 65)
    print("NÅSITUASJON (april 2026, uten smart styring):")
    print(f"  Forbruk:             {MONTHLY_CONSUMPTION_KWH:.0f} kWh/mnd")
    print(f"  Kjøp dag (reell):    {buy_price_ore(APRIL_SPOT_AVG_ORE, 10):.1f} øre/kWh inkl Norgespris")
    print(f"  Kjøp natt (reell):   {buy_price_ore(APRIL_SPOT_AVG_ORE, 3):.1f} øre/kWh inkl Norgespris")
    print(f"  Salgspris (netto):   {sell_price_ore():.2f} øre/kWh")
    print(f"  Kapasitetsledd:      {CAPACITY_CHARGE_NOK:.2f} kr/mnd (10-15A, 12.69 kW topp)")
    print(f"  Total strømkostnad:  {cost['total_nok']:.2f} kr/mnd")

    # ── Scenario A: Arbitrasje ──
    arb = scenario_arbitrage()
    print(f"""
─────────────────────────────────────────────────────────────
{arb['label']}
─────────────────────────────────────────────────────────────
  Lade kl 02-05 (spot ~30 øre):   {arb['charge_price_ore']:.1f} øre/kWh reell innkjøp
  Utlade kl 17-20 (spot ~146 øre): {arb['discharge_save_ore']:.1f} øre/kWh spart
  Gevinst per kWh syklet:          {arb['gain_per_kwh_nok']*100:.1f} øre ({arb['gain_per_kwh_nok']:.3f} kr)
  kWh syklet per mnd:              {arb['kwh_cycled_monthly']:.0f} kWh
  ✅ Månedlig gevinst:              {arb['monthly_gain_nok']:.0f} kr/mnd
  ✅ Årlig gevinst:                 {arb['annual_gain_nok']:.0f} kr/år""")

    # ── Scenario B: Peak-shaving ──
    peak = scenario_peak_shaving()
    print(f"""
─────────────────────────────────────────────────────────────
{peak['label']}
─────────────────────────────────────────────────────────────
  Nåværende topp:   {CURRENT_PEAK_KW} kW → {peak['current_tier_nok']:.2f} kr/mnd
  Mål under:        {peak['target_peak_kw']} kW → {peak['target_tier_nok']:.2f} kr/mnd
  Batteriet klarer: Levere 10 kW i inntil {USABLE_KWH/10:.1f} timer
  ✅ Månedlig spart: {peak['monthly_saving_nok']:.2f} kr/mnd
  ✅ Årlig spart:    {peak['annual_saving_nok']:.2f} kr/år""")

    # ── Scenario C: Kombinert ──
    combined = scenario_combined()
    roi = roi_analysis(combined['annual_total_nok'])
    print(f"""
─────────────────────────────────────────────────────────────
{combined['label']}
─────────────────────────────────────────────────────────────
  Arbitrasje:        {combined['arbitrage_nok']:.0f} kr/mnd
  Peak-shaving:      {combined['peak_saving_nok']:.2f} kr/mnd
  ════════════════════════════════
  ✅ Total månedlig: {combined['monthly_total_nok']:.0f} kr/mnd
  ✅ Total årlig:    {combined['annual_total_nok']:.0f} kr/år

ROI:
  Driftskostnad (server):  {roi['annual_cost_nok']:.0f} kr/år
  Netto årlig gevinst:     {roi['net_annual_nok']:.0f} kr/år
  Tilbakebetaling:         UMIDDELBART (batteri allerede installert)""")

    # ── Sesongvariasjon ──
    seasonal = seasonal_analysis()
    print(f"""
─────────────────────────────────────────────────────────────
SESONGVARIASJON (estimert):
─────────────────────────────────────────────────────────────""")
    total_year = 0
    for month, gain in seasonal.items():
        bar = "█" * int(max(gain, 0) / 15)
        print(f"  {month:<4} {gain:>7.0f} kr  {bar}")
        total_year += gain
    print(f"  {'TOTAL':>4} {total_year:>7.0f} kr/år")

    # ── Full kostnadsanalyse ──
    arb_kwh_yearly = arb['kwh_cycled_monthly'] * 12
    battery_wear_yearly = arb_kwh_yearly * BATTERY_COST_PER_KWH

    total_costs_yearly = (
        battery_wear_yearly +
        INVERTER_COST_YEARLY +
        SERVER_FIXED_COST_YEARLY +
        MAINTENANCE_YEARLY_NOK
    )
    gross_yearly = combined['annual_total_nok']
    net_yearly   = gross_yearly - total_costs_yearly

    print(f"""
═════════════════════════════════════════════════════════════
FULL KOSTNADSANALYSE (inkl. slitasje og drift)
═════════════════════════════════════════════════════════════

  INNTEKTER (brutto):
  ├─ Arbitrasje:                  {arb['annual_gain_nok']:>8.0f} kr/år
  └─ Peak-shaving:                {peak['annual_saving_nok']:>8.0f} kr/år
     ────────────────────────────────────────
     Brutto gevinst:              {gross_yearly:>8.0f} kr/år

  KOSTNADER:
  ├─ Batterislitasje (NMC):
  │    Innkjøpspris batteri:      {BATTERY_COST_NOK:>8.0f} kr
  │    Levetid sykluser:          {BATTERY_CYCLES_LIFETIME:>8} sykluser
  │    Kostnad per syklus:        {BATTERY_COST_PER_CYCLE:>8.1f} kr
  │    Kostnad per kWh syklet:    {BATTERY_COST_PER_KWH*100:>8.2f} øre/kWh
  │    kWh syklet per år:         {arb_kwh_yearly:>8.0f} kWh
  │  → Batterislitasje/år:        {battery_wear_yearly:>8.0f} kr/år
  │
  ├─ Inverter (2× MultiPlus-II):
  │    Kostnad: {INVERTER_COST_NOK:,} kr / {INVERTER_LIFETIME_YEARS} år
  │  → Avskrivning/år:            {INVERTER_COST_YEARLY:>8.0f} kr/år
  │
  ├─ Server (RPi):
  │    {SERVER_POWER_W}W × 24t × 365 = {SERVER_KWH_YEARLY:.0f} kWh/år strøm
  │    + avskrivning hardware
  │  → Server totalt/år:          {SERVER_FIXED_COST_YEARLY:>8.0f} kr/år
  │
  └─ Vedlikehold (BMS, kabler):   {MAINTENANCE_YEARLY_NOK:>8.0f} kr/år
     ────────────────────────────────────────
     Total kostnad/år:            {total_costs_yearly:>8.0f} kr/år

  NETTO LØNNSOMHET:
  ┌─────────────────────────────────────────
  │  Brutto:     {gross_yearly:>8.0f} kr/år
  │  Kostnader: -{total_costs_yearly:>7.0f} kr/år
  │  ═══════════════════════════════
  │  NETTO:      {net_yearly:>8.0f} kr/år  ({net_yearly/12:.0f} kr/mnd)
  └─────────────────────────────────────────

  VIKTIG OM BATTERISLITASJE:
  • Batteriet er allerede kjøpt og installert
  • Slitasjekostnaden er en MULIGHETSKOSTAND
    (batteriet slites uansett over tid, men trading øker sykluser)
  • Med 0.8 sykluser/dag = {0.8*365:.0f} sykluser/år → batteri holder {BATTERY_CYCLES_LIFETIME/(0.8*365):.1f} år
  • Uten trading (kun solenergy buffer): ~{BATTERY_CYCLES_LIFETIME/(0.3*365):.1f} år
  • Ekstra slitasje pga trading: ~{(BATTERY_CYCLES_LIFETIME/(0.8*365) - BATTERY_CYCLES_LIFETIME/(0.3*365)):.1f} år kortere levetid

  KONKLUSJON:
  {'✅ LØNNSOMT' if net_yearly > 0 else '❌ ULØNNSOMT'} selv med alle kostnader inkludert
  Netto gevinst: {net_yearly:.0f} kr/år = {net_yearly/12:.0f} kr/mnd
═════════════════════════════════════════════════════════════
""")


if __name__ == "__main__":
    main()
