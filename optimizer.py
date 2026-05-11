"""Optimalisering av lade/utlade-strategi."""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from price_fetcher import PricePoint, PriceFetcher
from tariff import buy_price_ore, sell_price_ore, should_charge, should_discharge
from config import CONFIG


@dataclass
class Action:
    timestamp: datetime
    action: str  # 'charge', 'discharge', 'idle'
    power_kw: float  # Positive for charge, negative for discharge
    expected_profit_nok: float = 0.0


class Optimizer:
    def __init__(self):
        self.capacity = CONFIG.battery_capacity_kwh
        self.max_charge = CONFIG.battery_max_charge_kw
        self.max_discharge = CONFIG.battery_max_discharge_kw
        self.efficiency = CONFIG.battery_efficiency
        self.min_soc = CONFIG.min_soc
        self.max_soc = CONFIG.max_soc
        self.min_diff = CONFIG.min_price_diff_nok

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

    def _decide_action_tariff(self, price: PricePoint, soc: float,
                               spot_ore: float, hour: int) -> Action:
        """
        Beslutning basert på reelle priser.
        
        Utlading: Når spotpris > 75 øre (salgspris) sparer vi kjøp fra grid
        Lading: Når innkjøpspris (inkl alle avgifter) er lav - lagrer billig strøm
        """
        buy_ore = buy_price_ore(spot_ore, hour)
        sell_ore = sell_price_ore()

        # --- UTLAD: Spotpris er høy → bruk batteri istedenfor dyr gridstrøm ---
        if should_discharge(spot_ore, hour) and soc > self.min_soc:
            avail_kwh = self.capacity * (soc - self.min_soc) / 100
            power = min(self.max_discharge, avail_kwh)
            # Gevinst: vi sparer innkjøpspris (buy_ore), men "ofrer" sell_ore
            savings_ore = buy_ore - sell_ore  # Spart per kWh ved å ikke kjøpe fra grid
            profit = power * savings_ore / 100  # kr
            return Action(timestamp=price.timestamp, action='discharge',
                         power_kw=-power, expected_profit_nok=profit)

        # --- LAD: Innkjøpspris er lav → fyll batteri for fremtidig bruk ---
        elif should_charge(spot_ore, hour) and soc < self.max_soc:
            avail_kwh = self.capacity * (self.max_soc - soc) / 100
            power = min(self.max_charge, avail_kwh)
            cost = power * buy_ore / 100  # kr
            return Action(timestamp=price.timestamp, action='charge',
                         power_kw=power, expected_profit_nok=-cost)

        else:
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
