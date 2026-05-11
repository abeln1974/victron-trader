"""Optimalisering av lade/utlade-strategi."""
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import logging
from price_fetcher import PricePoint, PriceFetcher
from tariff import (
    buy_price_ore, sell_price_ore, should_charge, should_discharge,
    capacity_charge_for_kw, CAPACITY_CHARGE_NOK,
    GRID_TARIFF_DAY_ORE, GRID_TARIFF_NIGHT_ORE,
    SUPPLIER_MARKUP_ORE, CONSUMPTION_TAX_ORE, ENOVA_ORE, VAT as TARIFF_VAT,
    is_day_tariff
)
from config import CONFIG, OSLO_TZ

# Føie AS nettleiepriser 2026 — kapasitetsledd (snitt 3 høyeste timer på ULIKE dager/mnd)
# Trinn 3:  5– 9.99 kW =  418.8 kr/mnd inkl MVA
# Trinn 4: 10–14.99 kW =  662.5 kr/mnd inkl MVA  ← faktisk trinn apr 2026 (avregnet 12.09 kW)
# Trinn 5: 15–19.99 kW =  837.5 kr/mnd inkl MVA
# Besparelse ved å holde under 10kW: 662.5 - 418.8 = 243.7 kr/mnd
# Energiledd dag (06-22): 16.50 øre eks mva → 20.63 øre inkl mva (Føie AS)
# Energiledd natt (22-06): 10.00 øre eks mva → 12.50 øre inkl mva (Føie AS)
PEAK_SHAVING_LIMIT_KW    = float(9.5)  # Mål: hold under 10kW (buffer 0.5kW)
PEAK_SHAVING_RESERVE_KWH = float(5.0)  # Hold alltid 5 kWh reservert til peak-shaving

# Kapasitetstrinn inkl MVA (kr/mnd) — brukes til lønnsomhetsberegning
ELVIA_CAPACITY_STEPS = [
    (0,    1.99,  237.5),
    (2,    4.99,  293.8),
    (5,    9.99,  418.8),
    (10,  14.99,  662.5),
    (15,  19.99,  837.5),
    (20,  24.99, 1075.0),
    (25,  49.99, 1437.5),
    (50,  74.99, 2375.0),
    (75, 9999.0, 3000.0),
]


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

        1. Identifiser de N dyreste timene i planperioden → reserver batteri til dem
        2. Identifiser de M billigste natte-timene → lad da (ikke sløs på middels priser)
        3. Ikke utlad under salgsgrensen - det er sløsing av batteri
        4. Peak-shaving reserve alltid satt av

        solar_kw: Nåværende sol-produksjon (kW)
        """
        if not prices:
            return []

        # Beregn reell kjøpspris per time (bruk norsk time for dag/natt-tariff)
        buy_prices = [buy_price_ore(p.price_ore_kwh / CONFIG.vat, p.timestamp.astimezone(OSLO_TZ).hour)
                      for p in prices]
        sell_ore = sell_price_ore()

        # --- Finn de beste utlade-timene ---
        # Strategi: Selg i de N timene med HØYEST råkjøpspris (hva nett faktisk koster
        # uten Norgespris-fradrag). Sorter etter høyeste råkjøpspris, ikke tidspunkt.
        # Dette sikrer at vi sparer batteri til de mest verdifulle timene.
        def raw_buy(p):
            h = p.timestamp.astimezone(OSLO_TZ).hour
            grid = GRID_TARIFF_DAY_ORE if is_day_tariff(h) else GRID_TARIFF_NIGHT_ORE
            return (p.price_ore_kwh / CONFIG.vat + SUPPLIER_MARKUP_ORE + grid + CONSUMPTION_TAX_ORE + ENOVA_ORE) * TARIFF_VAT

        # Beregn median råkjøpspris i perioden — selg kun i timer over medianen
        all_raw = sorted([raw_buy(p) for p in prices])
        median_raw = all_raw[len(all_raw) // 2]

        profitable_hours = set()
        discharge_candidates = sorted(
            [(i, p) for i, p in enumerate(prices)
             if should_discharge(p.price_ore_kwh / CONFIG.vat, p.timestamp.astimezone(OSLO_TZ).hour)
             and raw_buy(p) >= median_raw],  # Kun topp-halvdelen av priser
            key=lambda x: raw_buy(x[1]),  # Høyeste råkjøpspris først
            reverse=True
        )
        # Beregn tilgjengelig kapasitet
        usable_kwh = self.capacity * (current_soc - self.min_soc) / 100 - self.peak_reserve
        remaining_kwh = max(0, usable_kwh)
        for idx, price_point in discharge_candidates:
            if remaining_kwh <= 0:
                break
            profitable_hours.add(idx)
            remaining_kwh -= min(self.max_discharge, remaining_kwh)

        # --- Finn de beste ladetimene (laveste kjøpspris, kun natt) ---
        charge_hours = set()
        night_candidates = sorted(
            [(i, bp) for i, bp in enumerate(buy_prices)
             if not (6 <= prices[i].timestamp.astimezone(OSLO_TZ).hour < 22)],  # Kun natt
            key=lambda x: x[1]  # Sorter etter laveste pris
        )
        # Beregn ladekapasitet etter planlagt utlading (ikke nåværende SOC)
        discharged_kwh = len(profitable_hours) * self.max_discharge  # Approx
        soc_after_discharge = max(self.min_soc, current_soc - (discharged_kwh / self.capacity * 100))
        space_kwh = self.capacity * (self.max_soc - soc_after_discharge) / 100
        remaining_charge = max(0, space_kwh)
        for idx, bp in night_candidates:
            if remaining_charge <= 0:
                break
            # Lad kun hvis prisen er lav nok til å tjene på det
            if bp < sell_ore * self.efficiency:
                charge_hours.add(idx)
                remaining_charge -= min(self.max_charge, remaining_charge)

        # --- Bygg handlingsplan ---
        actions = []
        soc = current_soc

        for i, p in enumerate(prices):
            spot_ore = p.price_ore_kwh / CONFIG.vat
            local_hour = p.timestamp.astimezone(OSLO_TZ).hour
            is_night = not (6 <= local_hour < 22)
            sol_lader = solar_kw >= CONFIG.solar_threshold_kw
            buy_ore = buy_prices[i]

            # UTLAD: Kun i de planlagte topp-timene
            if i in profitable_hours and soc > self.min_soc:
                avail_kwh = max(0, self.capacity * (soc - self.min_soc) / 100 - self.peak_reserve)
                power = min(self.max_discharge, avail_kwh)
                if power > 0:
                    savings = buy_ore - sell_ore
                    profit = power * savings / 100
                    actions.append(Action(
                        timestamp=p.timestamp, action='discharge',
                        power_kw=-power, expected_profit_nok=profit,
                        reason=f'Topp #{list(profitable_hours).index(i)+1}: {buy_ore:.0f}ø (salg {sell_ore:.0f}ø)'
                    ))
                    soc_change = (power / self.efficiency / self.capacity) * 100
                    soc = max(soc - soc_change, self.min_soc)
                    continue

            # LAD: Kun i de planlagte billige natte-timene
            if i in charge_hours and soc < self.max_soc and is_night and not sol_lader:
                avail_kwh = self.capacity * (self.max_soc - soc) / 100
                power = min(self.max_charge, avail_kwh)
                cost = power * buy_ore / 100
                actions.append(Action(
                    timestamp=p.timestamp, action='charge',
                    power_kw=power, expected_profit_nok=-cost,
                    reason=f'Billigste natt: {buy_ore:.0f}ø'
                ))
                soc_change = (power * self.efficiency / self.capacity) * 100
                soc = min(soc + soc_change, self.max_soc)
                continue

            # SOL LADER: La Fronius gjøre jobben
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
        Peak-shaving: Utlad batteriet for å hindre at effekttopper
        fører til høyere kapasitetstrinn hos Føie AS 2026.

        Føie AS bruker snitt av de 3 høyeste timer på ULIKE dager per mnd.
        Mål: Hold under 9.5kW (buffer til 10kW-grensen).
        Trinn 3 (5-9.99kW): 418.8 kr/mnd
        Trinn 4 (10-14.99kW): 662.5 kr/mnd  ← faktisk trinn nå (12.09 kW avregnet)
        Besparelse: 243.7 kr/mnd ved å holde seg i trinn 3.

        current_grid_kw: Nåværende effekt fra nettet (målt via Qubino)
        soc: Batteriets nåværende ladenivå (%)
        """
        if current_grid_kw <= self.peak_limit_kw:
            return None  # Ingen peak-shaving nødvendig

        # Beregn hvor mye vi må levere fra batteri
        excess_kw = current_grid_kw - self.peak_limit_kw
        avail_kwh = self.capacity * (soc - self.min_soc) / 100 - self.peak_reserve

        if avail_kwh <= 0:
            return None  # Ikke nok batteri

        discharge_kw = min(excess_kw, self.max_discharge, avail_kwh)

        # Gevinst: unngår kapasitetshopp Trinn 3→4 = 243.7 kr/mnd (Føie AS 2026)
        # Fordelt per hendelse (~5 peak-events per mnd)
        saving_per_event = 243.7 / 5

        return Action(
            timestamp=datetime.now(OSLO_TZ),
            action='peak_shave',
            power_kw=-discharge_kw,
            expected_profit_nok=saving_per_event,
            reason=f'Grid {current_grid_kw:.1f}kW > {self.peak_limit_kw}kW grense'
        )

    def _decide_action_tariff(self, price: PricePoint, soc: float,
                               spot_ore: float, hour: int,
                               solar_kw: float = 0.0) -> Action:
        """
        Beslutning basert på reelle priser + sol-produksjon.

        Prioritet:
        1. Utlad: Spotpris høy → spar dyr gridstrøm
        2. Lad fra nett: KUN om natten (22-06) når spotpris er lav
           - Om dagen lader sol gratis → ikke kast penger på nett-lading
        3. Idle: Sol håndterer lading, ESS styrer selv
        """
        local_hour = price.timestamp.astimezone(OSLO_TZ).hour
        buy_ore  = buy_price_ore(spot_ore, local_hour)
        sell_ore = sell_price_ore()
        is_night = not (6 <= local_hour < 22)
        sol_lader = solar_kw >= CONFIG.solar_threshold_kw  # Fronius Primo 5kW, terskel 0.5kW

        # --- UTLAD: Spotpris er høy → bruk batteri istedenfor dyr gridstrøm ---
        if should_discharge(spot_ore, local_hour) and soc > self.min_soc:
            avail_kwh = max(0, self.capacity * (soc - self.min_soc) / 100 - self.peak_reserve)
            if avail_kwh > 0:
                power = min(self.max_discharge, avail_kwh)
                savings_ore = buy_ore - sell_ore
                profit = power * savings_ore / 100
                return Action(timestamp=price.timestamp, action='discharge',
                             power_kw=-power, expected_profit_nok=profit,
                             reason=f'Spot {spot_ore:.0f}ø > salg {sell_ore:.0f}ø')

        # --- LAD FRA NETT: Kun om natten + spotpris er lav ---
        # Om dagen: la sol lade gratis, spar nett-kostnaden
        elif should_charge(spot_ore, local_hour) and soc < self.max_soc:
            if is_night:
                avail_kwh = self.capacity * (self.max_soc - soc) / 100
                power = min(self.max_charge, avail_kwh)
                cost = power * buy_ore / 100
                return Action(timestamp=price.timestamp, action='charge',
                             power_kw=power, expected_profit_nok=-cost,
                             reason=f'Natt-lading: spot {spot_ore:.0f}ø (billig)')
            elif sol_lader:
                # Sol lader batteriet gratis om dagen - idle fra nett
                return Action(timestamp=price.timestamp, action='idle', power_kw=0.0,
                             reason=f'Sol {solar_kw:.1f}kW lader gratis')

        return Action(timestamp=price.timestamp, action='idle', power_kw=0.0)

    def get_immediate_action(self, current_price: PricePoint,
                            prices: List[PricePoint],
                            soc: float, solar_kw: float = 0.0) -> Action:
        """Get action for current hour only.

        Prisene fra fetcher er filtrert til "future hours" (>= now), så
        plan[0] er enten nåværende time (hvis fortsatt aktiv) eller neste.
        Vi tar plan[0] direkte for å unngå tidssone-mismatch.
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
        emoji = "🔋" if a.action == 'charge' else "⚡" if a.action == 'discharge' else "⏸️"
        print(f"{emoji} {a.timestamp.strftime('%H:%M')}: {a.action} {a.power_kw:.1f}kW")
