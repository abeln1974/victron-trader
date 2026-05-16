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


def test_trade_logging_integration():
    """Integrasjonstest: simulerer hele _execute_trade_cycle-flyten.
    
    Dette er den kritiske testen som avdekker bugs som ikke fanges
    av enkle unit-tester — spesielt at trade-logging faktisk skjer
    når en action-time utgår.
    """
    print("\n" + "="*50)
    print("TEST 5: Integrasjonstest trade-logging (kritisk)")
    print("="*50)

    from profit_tracker import ProfitTracker
    from optimizer import Optimizer, Action
    from tariff import sell_price_ore, buy_price_ore
    from config import OSLO_TZ
    import time

    db_path = "/app/data/test_integration.db" if os.path.exists("/app") else "./data/test_integration.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    tracker = ProfitTracker(db_path=db_path)

    # --- Simuler state slik main.py holder det ---
    current_action = None
    action_start_soc = None
    action_start_counters = None
    last_price_nok = 0.73

    def mock_log_trade_cycle(action, fresh_soc, prev_action, now_hour, action_hour):
        """Gjenskaper logikken i main.py _execute_trade_cycle og action-timeout."""
        nonlocal current_action, action_start_soc, action_start_counters

        # Sett start-SOC ved ny aktiv action (FIKSEN vi implementerte)
        is_new_active = action.action != 'idle' and (prev_action is None or prev_action.action == 'idle')
        if is_new_active:
            action_start_soc = fresh_soc
            action_start_counters = (800.0, 0.0)  # Mock SmartShunt (reg 310 = 0)

        # Simuler _execute_action (bare sett current_action)
        if action.action != 'idle':
            current_action = action
        else:
            current_action = None

    def mock_action_timeout(end_soc, now_hour):
        """Gjenskaper action-timeout logikken i main.py."""
        nonlocal current_action, action_start_soc, action_start_counters
        if not current_action or current_action.action == 'idle':
            return False

        act = current_action.action
        actual_kwh = 0.0
        kwh_source = "soc-delta"

        # SmartShunt fallback (reg 310 = 0 på dette systemet)
        start_dis, start_chg = action_start_counters if action_start_counters else (0, 0)
        end_dis, end_chg = (800.0, 0.0)  # Mock: ingen endring i reg 310
        if act == 'discharge':
            actual_kwh = max(0.0, end_dis - start_dis)
        else:
            actual_kwh = max(0.0, end_chg - start_chg)
        kwh_source = "smartshunt"

        # Fallback til SOC-delta (kritisk at action_start_soc er satt)
        if actual_kwh < 0.05 and action_start_soc is not None:
            delta_soc = abs(end_soc - action_start_soc)
            actual_kwh = CONFIG.battery_capacity_kwh * delta_soc / 100
            kwh_source = "soc-delta-fallback"

        if actual_kwh > 0.05:
            db_action = "sell" if act == "discharge" else "buy"
            spot = last_price_nok / CONFIG.vat
            price_nok = (sell_price_ore(spot * 100) if db_action == "sell"
                         else buy_price_ore(spot * 100, now_hour)) / 100
            tracker.log_trade(db_action, actual_kwh, price_nok)
            print(f"  ✅ Handel logget: {db_action} {actual_kwh:.2f} kWh [{kwh_source}] "
                  f"(SOC {action_start_soc:.1f}%→{end_soc:.1f}%)")
            current_action = None
            action_start_soc = None
            action_start_counters = None
            return True
        else:
            print(f"  ❌ Handel IKKE logget: actual_kwh={actual_kwh:.3f}, "
                  f"action_start_soc={action_start_soc}")
            current_action = None
            action_start_soc = None
            action_start_counters = None
            return False

    try:
        passed = True

        # --- Scenario 1: Normal charge-syklus ---
        print("\nScenario 1: Charge 8kW, SOC 65→72%")
        charge_action = Action(
            timestamp=datetime.now(OSLO_TZ).replace(minute=0, second=0),
            action='charge', power_kw=8.0, expected_profit_nok=-5.0,
            reason='Test nattlading'
        )
        mock_log_trade_cycle(charge_action, 65.0, None, 22, 22)
        print(f"  action_start_soc satt til: {action_start_soc}")
        logged = mock_action_timeout(72.0, 23)  # Neste time
        if not logged:
            print("  ❌ FAIL: Charge-handel ble ikke logget")
            passed = False

        # --- Scenario 2: Charge der set_charge_power feiler (Modbus-feil) ---
        print("\nScenario 2: Charge 8kW men set_charge_power feiler (Modbus-feil)")
        mock_log_trade_cycle(charge_action, 66.7, None, 23, 23)
        print(f"  action_start_soc satt til: {action_start_soc} (skal være 66.7)")
        # SOC økte pga batteri ladet selv om Modbus feilet
        logged = mock_action_timeout(70.1, 0)
        if not logged:
            print("  ❌ FAIL: Handel med Modbus-feil ble ikke logget")
            passed = False

        # --- Scenario 3: Idle — ingen handel skal logges ---
        print("\nScenario 3: Idle — ingen handel")
        idle_action = Action(
            timestamp=datetime.now(OSLO_TZ),
            action='idle', power_kw=0.0, expected_profit_nok=0.0, reason='idle'
        )
        mock_log_trade_cycle(idle_action, 71.0, None, 6, 6)
        logged = mock_action_timeout(71.0, 7)
        if logged:
            print("  ❌ FAIL: Idle-action logget feilaktig som handel")
            passed = False
        else:
            print("  ✅ Idle logget ikke — korrekt")

        # --- Verifiser database ---
        print("\nDatabase verifisering:")
        trades = tracker.get_recent_trades(10)
        print(f"  Antall handler i DB: {len(trades)}")
        for t in trades:
            print(f"    {t['trade_type']:4s} {t['energy_kwh']:.2f} kWh @ {t['price_nok_kwh']:.3f} kr → {t['net_profit_nok']:+.2f} kr")

        stats = tracker.get_stats()
        print(f"  Dagens profitt: {stats['today_profit_nok']:.2f} kr")

        expected_trades = 2  # Scenario 1 + 2
        if len(trades) != expected_trades:
            print(f"  ❌ FAIL: Forventet {expected_trades} handler, fikk {len(trades)}")
            passed = False
        else:
            print(f"  ✅ Korrekt antall handler logget ({expected_trades})")

        return passed

    except Exception as e:
        print(f"❌ FEIL: {e}")
        traceback.print_exc()
        return False
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


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
    all_passed &= test_trade_logging_integration()
    
    print("\n" + "="*50)
    if all_passed:
        print("✅ ALLE TESTER BESTÅTT — trygt å deploye")
        print("="*50)
        return 0
    else:
        print("❌ NOEN TESTER FEILET — IKKE DEPLOY FØR FIKSET")
        print("="*50)
        return 1


if __name__ == "__main__":
    sys.exit(main())
