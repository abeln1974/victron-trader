"""Optimalisering av lade/utlade-strategi."""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from price_fetcher import PricePoint, PriceFetcher
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
        Simple peak-shaving strategy:
        - Buy when price is in bottom N%
        - Sell when price is in top N%
        """
        if not prices:
            return []

        sorted_prices = sorted(prices, key=lambda p: p.price_nok_kwh)
        n = len(sorted_prices)
        
        # Define thresholds (bottom 30% charge, top 30% discharge)
        charge_threshold = sorted_prices[int(n * 0.3)].price_nok_kwh
        discharge_threshold = sorted_prices[int(n * 0.7)].price_nok_kwh

        actions = []
        for p in prices:
            action = self._decide_action(p, current_soc, charge_threshold, discharge_threshold)
            actions.append(action)
            
            # Simulate SOC change for planning (rough estimate)
            if action.action == 'charge':
                current_soc = min(current_soc + 10, self.max_soc)
            elif action.action == 'discharge':
                current_soc = max(current_soc - 10, self.min_soc)

        return actions

    def _decide_action(self, price: PricePoint, soc: float, 
                       charge_thresh: float, discharge_thresh: float) -> Action:
        """Decide action for a single price point."""
        if price.price_nok_kwh <= charge_thresh and soc < self.max_soc:
            # Buy/charge
            power = min(self.max_charge, self.capacity * (self.max_soc - soc) / 100)
            cost = power * price.price_nok_kwh
            return Action(timestamp=price.timestamp, action='charge', 
                         power_kw=power, expected_profit_nok=-cost)
        
        elif price.price_nok_kwh >= discharge_thresh and soc > self.min_soc:
            # Sell/discharge
            power = -min(self.max_discharge, self.capacity * (soc - self.min_soc) / 100)
            revenue = abs(power) * price.price_nok_kwh * self.efficiency
            return Action(timestamp=price.timestamp, action='discharge', 
                         power_kw=power, expected_profit_nok=revenue)
        
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
