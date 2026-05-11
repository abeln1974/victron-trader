"""Optimalisering av lade/utlade-strategi."""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from price_fetcher import PricePoint, PriceFetcher
from tariff import (
    buy_price_ore, sell_price_ore, should_charge, should_discharge,
    capacity_charge_for_kw, CAPACITY_CHARGE_NOK
)
from config import CONFIG

# Elvia: kapasitetsledd beregnes som snitt av 3 høyeste timer per mnd
# Vi holder oss under 10.35 kW (10A-trinn = 475 kr/mnd vs 662.50 kr nå)
PEAK_SHAVING_LIMIT_KW = float(10.0)   # Mål: hold under dette
PEAK_SHAVING_RESERVE_KWH = float(5.0) # Hold alltid 5 kWh reservert til peak-shaving


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

    def optimize(self, prices: List[PricePoint], current_soc: float = 50.0) -> List[Action]:
        """
        Strategi basert på reelle kjøps- og salgspriser (Kraftriket/Elvia):
        - Lad når innkjøpspris er lav nok til at vi tjener på å selge senere
        - Utlad når spotpris er over salgspris (75 øre) - vi sparer å kjøpe dyr strøm
        """
        if not prices:
            return []

        actions = []
        soc = current_soc

        for p in prices:
            spot_ore = p.price_ore_kwh / CONFIG.vat  # Konverter tilbake til eks mva
            hour = p.timestamp.hour
            action = self._decide_action_tariff(p, soc, spot_ore, hour)
            actions.append(action)

            # Simuler SOC-endring for planlegging
            kwh_change = abs(action.power_kw)  # Per time
            if action.action == 'charge':
                soc_change = (kwh_change * self.efficiency / self.capacity) * 100
                soc = min(soc + soc_change, self.max_soc)
            elif action.action == 'discharge':
                soc_change = (kwh_change / self.efficiency / self.capacity) * 100
                soc = max(soc - soc_change, self.min_soc)

        return actions

    def peak_shave(self, current_grid_kw: float, soc: float) -> Optional[Action]:
        """
        Peak-shaving: Utlad batteriet for å hindre at effekttopper
        fører til høyere kapasitetstrinn hos Elvia.

        Elvia bruker snitt av de 3 høyeste enkelt-timene per mnd.
        Mål: Hold under 10.35 kW (10A-trinn = 475 kr vs 662.50 kr)

        current_grid_kw: Nåværende effekt fra nettet (målt)
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

        # Gevinst: unngår kapasitetshopp på 225 kr/mnd
        # Fordelt per time der peak-shaving skjer (~5 timer/mnd)
        saving_per_event = 225.0 / 5

        return Action(
            timestamp=datetime.now(),
            action='peak_shave',
            power_kw=-discharge_kw,
            expected_profit_nok=saving_per_event,
            reason=f'Grid {current_grid_kw:.1f}kW > {self.peak_limit_kw}kW grense'
        )

    def _decide_action_tariff(self, price: PricePoint, soc: float,
                               spot_ore: float, hour: int) -> Action:
        """
        Beslutning basert på reelle priser.

        Prioritet:
        1. Peak-shaving (alltid først - kapasitetsledd er garantert gevinst)
        2. Arbitrasje utlad (spotpris høy)
        3. Arbitrasje lad (spotpris lav)
        """
        buy_ore  = buy_price_ore(spot_ore, hour)
        sell_ore = sell_price_ore()

        # --- UTLAD: Spotpris er høy → bruk batteri istedenfor dyr gridstrøm ---
        if should_discharge(spot_ore, hour) and soc > self.min_soc:
            # Reserver peak_reserve kWh til peak-shaving
            avail_kwh = max(0, self.capacity * (soc - self.min_soc) / 100 - self.peak_reserve)
            if avail_kwh > 0:
                power = min(self.max_discharge, avail_kwh)
                savings_ore = buy_ore - sell_ore
                profit = power * savings_ore / 100
                return Action(timestamp=price.timestamp, action='discharge',
                             power_kw=-power, expected_profit_nok=profit,
                             reason=f'Spot {spot_ore:.0f}ø > salg {sell_ore:.0f}ø')

        # --- LAD: Innkjøpspris er lav → fyll batteri ---
        elif should_charge(spot_ore, hour) and soc < self.max_soc:
            avail_kwh = self.capacity * (self.max_soc - soc) / 100
            power = min(self.max_charge, avail_kwh)
            cost = power * buy_ore / 100
            return Action(timestamp=price.timestamp, action='charge',
                         power_kw=power, expected_profit_nok=-cost,
                         reason=f'Billig spot {spot_ore:.0f}ø, kjøp billig')

        return Action(timestamp=price.timestamp, action='idle', power_kw=0.0)

    def get_immediate_action(self, current_price: PricePoint, 
                            prices: List[PricePoint], 
                            soc: float) -> Action:
        """Get action for current hour only."""
        plan = self.optimize(prices, soc)
        for action in plan:
            if action.timestamp.hour == datetime.now().hour:
                return action
        return Action(timestamp=datetime.now(), action='idle', power_kw=0.0)


if __name__ == "__main__":
    fetcher = PriceFetcher()
    prices = fetcher.get_prices(24)
    
    opt = Optimizer()
    plan = opt.optimize(prices, current_soc=60.0)
    
    print("Plan for neste 24t:")
    for a in plan[:8]:
        emoji = "🔋" if a.action == 'charge' else "⚡" if a.action == 'discharge' else "⏸️"
        print(f"{emoji} {a.timestamp.strftime('%H:%M')}: {a.action} {a.power_kw:.1f}kW")
