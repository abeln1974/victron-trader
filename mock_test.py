"""Test script for victron-trader uten ekte Victron-tilkobling.

Bruker mock-data for å teste:
- Pris-henting fra hvakosterstrommen.no
- Optimaliseringslogikk
- Profit tracking
- Database-skriving

Kjør i Docker:
    docker compose run --rm victron-trader python mock_test.py
"""
import os
import sys
import traceback
from datetime import datetime, timedelta

# Sikre at data-directory eksisterer (viktig for Docker)
os.makedirs('/app/data', exist_ok=True)
os.makedirs('/app/logs', exist_ok=True)

from config import CONFIG
from price_fetcher import PriceFetcher
from optimizer import Optimizer
from profit_tracker import ProfitTracker


class MockVictronModbus:
    """Mock Victron for testing uten hardware."""
    
    def __init__(self):
        self._connected = True
        self._soc = 65.0  # Simulert SOC
        self._grid_power = 0
        self.actions_log = []
    
    def connect(self):
        print("[MOCK] Modbus-TCP connected (simulated)")
        return True
    
    def disconnect(self):
        print("[MOCK] Disconnected")
    
    def get_soc(self):
        return self._soc
    
    def get_grid_power(self):
        return self._grid_power
    
    def set_grid_setpoint(self, power_watts):
        action = "import" if power_watts > 0 else "export" if power_watts < 0 else "idle"
        print(f"[MOCK] Set grid setpoint: {power_watts}W ({action})")
        self._grid_power = power_watts
        self.actions_log.append({
            'time': datetime.now(),
            'power': power_watts,
            'action': action
        })
        
        # Simuler SOC-endring
        if power_watts > 0:
            self._soc = min(100, self._soc + 2)  # Ladning
        elif power_watts < 0:
            self._soc = max(0, self._soc - 2)   # Utlading
            
        return True
    
    def set_charge_power(self, kw):
        return self.set_grid_setpoint(kw * 1000)
    
    def set_discharge_power(self, kw):
        return self.set_grid_setpoint(-kw * 1000)
    
    def stop_ess_control(self):
        return self.set_grid_setpoint(0)


def test_price_fetching():
    """Test pris-henting."""
    print("\n" + "="*50)
    print("TEST 1: Pris-henting fra hvakosterstrommen.no")
    print("="*50)
    
    try:
        fetcher = PriceFetcher()
        prices = fetcher.get_prices(24)
        
        if not prices:
            print("❌ FEIL: Kunne ikke hente priser")
            return False
        
        print(f"✅ Hentet {len(prices)} priser")
        print(f"   Prisområde: {CONFIG.price_area}")
        print("\nNeste 6 timer:")
        for p in prices[:6]:
            print(f"  {p.timestamp.strftime('%H:%M')}: {p.price_ore_kwh:.1f} øre ({p.price_nok_kwh:.3f} kr)")
        
        return True
    except Exception as e:
        print(f"❌ FEIL: {e}")
        traceback.print_exc()
        return False


def test_optimizer():
    """Test optimalisering."""
    print("\n" + "="*50)
    print("TEST 2: Optimaliseringslogikk")
    print("="*50)
    
    try:
        fetcher = PriceFetcher()
        prices = fetcher.get_prices(24)
        
        if not prices:
            print("❌ FEIL: Ingen priser tilgjengelig")
            return False
        
        opt = Optimizer()
        plan = opt.optimize(prices, current_soc=65.0)
        
        print(f"✅ Generert plan med {len(plan)} timer")
        print(f"   Batterikapasitet: {CONFIG.battery_capacity_kwh} kWh")
        print("\nNeste 6 timer:")
        for a in plan[:6]:
            emoji = "🔋" if a.action == 'charge' else "⚡" if a.action == 'discharge' else "⏸️"
            profit = f" ({a.expected_profit_nok:+.2f} kr)" if a.expected_profit_nok != 0 else ""
            print(f"  {emoji} {a.timestamp.strftime('%H:%M')}: {a.action} {a.power_kw:.1f}kW{profit}")
        
        return True
    except Exception as e:
        print(f"❌ FEIL: {e}")
        traceback.print_exc()
        return False


def test_profit_tracking():
    """Test profit tracking database."""
    print("\n" + "="*50)
    print("TEST 3: Profit tracking (SQLite)")
    print("="*50)
    
    try:
        # Bruk /app/data for Docker, ./data for lokal
        db_path = "/app/data/test_profit.db" if os.path.exists("/app") else "./data/test_profit.db"
        tracker = ProfitTracker(db_path=db_path)
        
        # Simuler noen trades
        tracker.log_trade("buy", 10, 0.5)
        tracker.log_trade("sell", 10, 1.2)
        tracker.log_trade("buy", 5, 0.4)
        
        stats = tracker.get_stats()
        print(f"✅ Trades i dag: {stats['today_bought_kwh']:.1f} kWh kjøpt, {stats['today_sold_kwh']:.1f} kWh solgt")
        print(f"✅ Dagens profitt: {stats['today_profit_nok']:.2f} kr")
        print(f"✅ Total profitt: {stats['total_profit_nok']:.2f} kr")
        
        # Rens test-database
        if os.path.exists(db_path):
            os.remove(db_path)
        print("✅ Test-database slettet")
        
        return True
    except Exception as e:
        print(f"❌ FEIL: {e}")
        traceback.print_exc()
        return False


def test_full_mock():
    """Test komplett flyt med mock Victron."""
    print("\n" + "="*50)
    print("TEST 4: Komplett mock-handelsrunde")
    print("="*50)
    
    vic = None
    try:
        # Setup
        vic = MockVictronModbus()
        vic.connect()
        
        fetcher = PriceFetcher()
        opt = Optimizer()
        
        # Bruk samme path-logikk som andre tester
        db_path = "/app/data/test_profit.db" if os.path.exists("/app") else "./data/test_profit.db"
        tracker = ProfitTracker(db_path=db_path)
        
        # Hent priser
        prices = fetcher.get_prices(24)
        current = prices[0] if prices else None
        
        if not current:
            print("❌ FEIL: Ingen priser")
            return False
        
        print(f"\nNåværende pris: {current.price_nok_kwh:.3f} kr/kWh")
        print(f"Start SOC: {vic.get_soc():.1f}%")
        
        # Kjør 3 simulerte handelsrunder
        for i in range(3):
            print(f"\n--- Runde {i+1} ---")
            soc = vic.get_soc()
            action = opt.get_immediate_action(current, prices, soc)
            
            print(f"SOC: {soc:.1f}%")
            print(f"Beslutning: {action.action} @ {action.power_kw:.1f}kW")
            
            # Eksekver
            if action.action == 'charge':
                vic.set_charge_power(action.power_kw)
                tracker.log_trade("buy", abs(action.power_kw), current.price_nok_kwh)
            elif action.action == 'discharge':
                vic.set_discharge_power(abs(action.power_kw))
                tracker.log_trade("sell", abs(action.power_kw), current.price_nok_kwh)
            else:
                vic.stop_ess_control()
            
            print(f"Ny SOC: {vic.get_soc():.1f}%")
        
        vic.disconnect()
        
        # Vis resultat
        stats = tracker.get_stats()
        print(f"\n📊 Resultat:")
        print(f"  Handler: {len(vic.actions_log)}")
        print(f"  Profitt: {stats['today_profit_nok']:.2f} kr")
        
        # Rens
        if os.path.exists(db_path):
            os.remove(db_path)
        
        return True
    except Exception as e:
        print(f"❌ FEIL: {e}")
        traceback.print_exc()
        if vic:
            vic.disconnect()
        return False


def main():
    print("="*50)
    print("Victron Trader - Lokal Testing (MOCK)")
    print("="*50)
    print(f"Konfig:")
    print(f"  Prisområde: {CONFIG.price_area}")
    print(f"  Batteri: {CONFIG.battery_capacity_kwh} kWh")
    print(f"  Max effekt: {CONFIG.battery_max_charge_kw} kW")
    
    all_passed = True
    
    all_passed &= test_price_fetching()
    all_passed &= test_optimizer()
    all_passed &= test_profit_tracking()
    all_passed &= test_full_mock()
    
    print("\n" + "="*50)
    if all_passed:
        print("✅ ALLE TESTER BESTÅTT")
        print("="*50)
        print("\nSystemet er klart for bruk!")
        print("Konfigurer VICTRON_HOST i .env og kjør med Docker.")
        return 0
    else:
        print("❌ NOEN TESTER FEILET")
        print("="*50)
        return 1


if __name__ == "__main__":
    sys.exit(main())
