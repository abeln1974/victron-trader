"""Optimalisering av lade/utlade-strategi."""
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import logging
from price_fetcher import PricePoint, PriceFetcher
from solar_forecast import get_solar_reserve_pct
from tariff import (
    buy_price_ore, sell_price_ore, should_charge, should_discharge,
    capacity_charge_for_kw, CAPACITY_CHARGE_NOK,
    GRID_TARIFF_DAY_ORE, GRID_TARIFF_NIGHT_ORE,
    FIXED_PRICE_ORE, CONSUMPTION_TAX_ORE, ENOVA_ORE, VAT as TARIFF_VAT,
    is_day_tariff
)
from config import CONFIG, OSLO_TZ

# Føie AS nettleiepriser 2026 — kapasitetsledd (snitt 3 høyeste timer på ULIKE dager/mnd)
# Trinn 3:  5– 9.99 kW =  418.8 kr/mnd inkl MVA  ← MÅL
# Trinn 4: 10–14.99 kW =  662.5 kr/mnd inkl MVA  ← faktisk trinn apr 2026 (avregnet 12.09 kW)
# Trinn 5: 15–19.99 kW =  837.5 kr/mnd inkl MVA
# Besparelse ved å holde under 10kW: 662.5 - 418.8 = 243.7 kr/mnd
# Kapasitetstrinn-konstanter er definert i tariff.py (CAPACITY_TIERS) — ikke dupliser her
PEAK_SHAVING_LIMIT_KW    = float(9.5)  # Mål: hold under 10kW (buffer 0.5kW)
PEAK_SHAVING_RESERVE_KWH = float(5.0)  # Hold alltid 5 kWh reservert til peak-shaving


@dataclass
class Action:
    timestamp: datetime
    action: str  # 'charge', 'discharge', 'idle', 'peak_shave'
    power_kw: float  # Positive for charge, negative for discharge
    expected_profit_nok: float = 0.0
    reason: str = ''


class Optimizer:
    def __init__(self):
        self.capacity       = CONFIG.battery_capacity_kwh
        self.max_charge     = CONFIG.battery_max_charge_kw
        self.max_discharge  = CONFIG.battery_max_discharge_kw
        self.efficiency     = CONFIG.battery_efficiency
        self.min_soc        = CONFIG.min_soc
        self.max_soc        = CONFIG.max_soc
        self.min_diff       = CONFIG.min_price_diff_nok
        self.peak_limit_kw  = PEAK_SHAVING_LIMIT_KW
        self.peak_reserve   = PEAK_SHAVING_RESERVE_KWH

    def optimize(self, prices: List[PricePoint], current_soc: float = 50.0,
                solar_kw: float = 0.0) -> List[Action]:
        """
        Smart topp-optimering med planlegging fremover:

        1. Identifiser de N dyreste timene i planperioden -> reserver batteri til dem
        2. Identifiser de M billigste natte-timene -> lad da (ikke slos pa middels priser)
        3. Ikke utlad under salgsgrensen - det er slosing av batteri
        4. Peak-shaving reserve alltid satt av

        solar_kw: Navarende sol-produksjon (kW)
        """
        if not prices:
            return []

        # Beregn reell kjopspris per time (bruk norsk time for dag/natt-tariff)
        buy_prices = [buy_price_ore(p.price_ore_kwh / CONFIG.vat, p.timestamp.astimezone(OSLO_TZ).hour)
                      for p in prices]
        def spot_ore(p):
            return p.price_ore_kwh / CONFIG.vat

        def sell_ore(p):
            return sell_price_ore(spot_ore(p))

        def raw_buy(p):
            h = p.timestamp.astimezone(OSLO_TZ).hour
            grid = GRID_TARIFF_DAY_ORE if is_day_tariff(h) else GRID_TARIFF_NIGHT_ORE
            return (FIXED_PRICE_ORE + grid + CONSUMPTION_TAX_ORE + ENOVA_ORE) * TARIFF_VAT

        # Topp-N strategi: velg de beste timene batteriet faktisk rekker
        # Bruk max_soc som planlagt SOC — vi antar batteriet lades fullt om natten
        # før neste dags discharge-timer. Dette sikrer at vi planlegger for full kapasitet.
        planned_soc = max(current_soc, self.max_soc)  # Anta full lading før neste dag
        usable_kwh = self.capacity * (planned_soc - self.min_soc) / 100 - self.peak_reserve
        remaining_kwh = max(0, usable_kwh)

        discharge_candidates = sorted(
            [(i, p) for i, p in enumerate(prices)
             if should_discharge(p.price_ore_kwh / CONFIG.vat, p.timestamp.astimezone(OSLO_TZ).hour)
             and 6 <= p.timestamp.astimezone(OSLO_TZ).hour < 22],  # Kun dagtid — natt reserveres for lading
            key=lambda x: sell_ore(x[1]),  # Sorter på salgspris (spot) — høyest spot gir mest inntekt
            reverse=True  # Beste pris først
        )

        profitable_hours = set()
        for idx, price_point in discharge_candidates:
            if remaining_kwh <= 0:
                break
            profitable_hours.add(idx)
            remaining_kwh -= min(self.max_discharge, remaining_kwh)

        # --- Finn de beste ladetimene (laveste kjopspris, kun natt) ---
        # Lademål: max_soc minus sol-reserve (dynamisk fra Open-Meteo MEPS / fallback statisk)
        solar_reserve_pct = get_solar_reserve_pct(
            lat=CONFIG.site_lat,
            lon=CONFIG.site_lon,
            panel_peak_kw=CONFIG.solar_max_kw,
            battery_capacity_kwh=self.capacity,
            system_efficiency=CONFIG.solar_system_efficiency,
            fallback_hours=CONFIG.solar_fallback_hours,
        )
        charge_target_soc = self.max_soc - solar_reserve_pct
        charge_hours = set()
        night_candidates = sorted(
            [(i, bp) for i, bp in enumerate(buy_prices)
             if not (6 <= prices[i].timestamp.astimezone(OSLO_TZ).hour < 22)],
            key=lambda x: x[1]
        )
        discharged_kwh = len(profitable_hours) * self.max_discharge
        soc_after_discharge = max(self.min_soc, current_soc - (discharged_kwh / self.capacity * 100))
        # Beregn hvor mye som MÅ lades — legg til 20% buffer for peak-shaving-reduksjon
        space_kwh = self.capacity * (charge_target_soc - soc_after_discharge) / 100
        remaining_charge = max(0, space_kwh * 1.2)  # 20% buffer for peak-shaving-tap
        for idx, bp in night_candidates:
            if remaining_charge <= 0:
                break
            charge_hours.add(idx)
            remaining_charge -= min(self.max_charge, remaining_charge)

        # --- Bygg handlingsplan ---
        actions = []
        soc = current_soc

        for i, p in enumerate(prices):
            p_spot_ore = p.price_ore_kwh / CONFIG.vat
            local_hour = p.timestamp.astimezone(OSLO_TZ).hour
            is_night = not (6 <= local_hour < 22)
            sol_lader = solar_kw >= CONFIG.solar_threshold_kw
            buy_ore = buy_prices[i]

            # UTLAD: Kun i de planlagte topp-timene
            if i in profitable_hours and soc > self.min_soc:
                avail_kwh = max(0, self.capacity * (soc - self.min_soc) / 100 - self.peak_reserve)
                power = min(self.max_discharge, avail_kwh)
                if power > 0:
                    s_ore = sell_ore(p)
                    savings = buy_ore - s_ore
                    profit = power * savings / 100
                    actions.append(Action(
                        timestamp=p.timestamp, action='discharge',
                        power_kw=-power, expected_profit_nok=profit,
                        reason=f'Topp #{list(profitable_hours).index(i)+1}: {buy_ore:.0f}o (salg {s_ore:.0f}o)'
                    ))
                    soc_change = (power / self.efficiency / self.capacity) * 100
                    soc = max(soc - soc_change, self.min_soc)
                    continue

            # LAD: Kun i de planlagte billige natte-timene
            # Cap charge_kw slik at grid ikke overstiger peak_limit_kw
            if i in charge_hours and soc < self.max_soc and is_night and not sol_lader:
                avail_kwh = self.capacity * (self.max_soc - soc) / 100
                power = min(self.max_charge, avail_kwh)
                # Peak-limit-koordinering: begrens lading til hva peak-grensen tillater
                # Antar typisk nattforbruk 1.5 kW (konservativt estimat uten live-data her)
                typical_night_load_kw = 1.5
                charge_headroom_kw = max(0, self.peak_limit_kw - typical_night_load_kw)
                power = min(power, charge_headroom_kw)
                if power > 0:
                    cost = power * buy_ore / 100
                    actions.append(Action(
                        timestamp=p.timestamp, action='charge',
                        power_kw=power, expected_profit_nok=-cost,
                        reason=f'Billigste natt: {buy_ore:.0f}o (cap {power:.1f}kW for peak-limit)'
                    ))
                    soc_change = (power * self.efficiency / self.capacity) * 100
                    soc = min(soc + soc_change, self.max_soc)
                    continue

            # SOL LADER: La Fronius gjore jobben
            if sol_lader and not is_night:
                actions.append(Action(
                    timestamp=p.timestamp, action='idle', power_kw=0.0,
                    reason=f'Sol {solar_kw:.1f}kW lader gratis'
                ))
                continue

            # IDLE
            actions.append(Action(timestamp=p.timestamp, action='idle', power_kw=0.0))

        return actions

    def peak_shave(self, current_grid_kw: float, soc: float) -> Optional[Action]:
        """
        Peak-shaving: Utlad batteriet for a hindre at effekttopper
        forer til hoyere kapasitetstrinn hos Foe AS 2026.

        Foe AS bruker snitt av de 3 hoyeste timer pa ULIKE dager per mnd.
        Mal: Hold under 9.5kW (buffer til 10kW-grensen).
        Trinn 3 (5-9.99kW): 418.8 kr/mnd
        Trinn 4 (10-14.99kW): 662.5 kr/mnd  <- faktisk trinn na (12.09 kW avregnet)
        Besparelse: 243.7 kr/mnd ved a holde seg i trinn 3.

        current_grid_kw: Navarende effekt fra nettet (malt via Qubino)
        soc: Batteriets navarende ladeniva (%)
        """
        if current_grid_kw <= self.peak_limit_kw:
            return None

        excess_kw = current_grid_kw - self.peak_limit_kw
        avail_kwh = self.capacity * (soc - self.min_soc) / 100 - self.peak_reserve

        if avail_kwh <= 0:
            return None

        discharge_kw = min(excess_kw, self.max_discharge, avail_kwh)

        saving_per_event = 243.7 / 5  # ~5 peak-events per mnd, konservativt

        return Action(
            timestamp=datetime.now(OSLO_TZ),
            action='peak_shave',
            power_kw=-discharge_kw,
            expected_profit_nok=saving_per_event,
            reason=f'Grid {current_grid_kw:.1f}kW > {self.peak_limit_kw}kW grense'
        )

    def get_immediate_action(self, current_price: PricePoint,
                            prices: List[PricePoint],
                            soc: float, solar_kw: float = 0.0) -> Action:
        """Get action for current hour only.

        Prisene fra fetcher er filtrert til "future hours" (>= now), sa
        plan[0] er enten navarende time (hvis fortsatt aktiv) eller neste.
        Vi tar plan[0] direkte for a unnga tidssone-mismatch.
        """
        plan = self.optimize(prices, soc, solar_kw)
        if plan:
            return plan[0]
        return Action(timestamp=datetime.now(OSLO_TZ), action='idle', power_kw=0.0)


if __name__ == "__main__":
    fetcher = PriceFetcher()
    prices = fetcher.get_prices(24)

    opt = Optimizer()
    plan = opt.optimize(prices, current_soc=60.0)

    print("Plan for neste 24t:")
    for a in plan[:8]:
        emoji = "\U0001f50b" if a.action == 'charge' else "\u26a1" if a.action == 'discharge' else "\u23f8\ufe0f"
        print(f"{emoji} {a.timestamp.strftime('%H:%M')}: {a.action} {a.power_kw:.1f}kW")
