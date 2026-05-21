"""Web dashboard for Victron Energy Trader."""
import os
import json
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string, request

from profit_tracker import ProfitTracker
from price_fetcher import PriceFetcher
from tariff import buy_price_ore, sell_price_ore, CAPACITY_CHARGE_NOK, GRID_TARIFF_DAY_ORE, GRID_TARIFF_NIGHT_ORE, NORGESPRIS_CAP_ORE, CONSUMPTION_TAX_ORE, ENOVA_ORE, is_day_tariff
from config import CONFIG, OSLO_TZ

app = Flask(__name__)
tracker = ProfitTracker()
fetcher = PriceFetcher()

# Cache priser
_price_cache = {"data": [], "fetched": None}
_price_lock = threading.Lock()

# Cache live Cerbo GX data (polles hvert 10s i bakgrunn)
_live_cache = {
    "soc": None, "grid_w": None, "grid_l1": None, "grid_l2": None, "grid_l3": None,
    "grid_source": None,  # 'qubino' eller 'modbus'
    "solar_w": None, "battery_w": None, "updated": None, "error": None
}
_live_lock = threading.Lock()

def _poll_cerbo():
    """Bakgrunnstråd: les Cerbo GX via Modbus + Qubino via HA hvert 10s."""
    import time
    from victron_modbus import VictronModbus
    from ha_qubino import QubinoReader
    vic = VictronModbus()
    qubino = QubinoReader()
    connected = False
    while True:
        try:
            if not connected:
                connected = vic.connect()
            if connected:
                soc     = vic.get_soc()
                solar_w = vic.get_solar_power()
                bat_raw = vic.get_battery_power()  # Bruk get_battery_power() for korrekt fortegn

                # Grid: Qubino primær (total inkl L3 via _w_6), VM-3P75CT fallback
                qpower = qubino.get_grid_power()
                if qpower:
                    grid_w   = qpower["total"]
                    grid_l1  = qpower["l1"]
                    grid_l2  = qpower["l2"]
                    grid_l3  = qpower["l3"]
                    grid_src = "qubino"
                else:
                    phases  = vic.get_grid_phases()
                    grid_l1 = phases.get("l1")
                    grid_l2 = phases.get("l2")
                    grid_l3 = 0.0
                    grid_w  = (grid_l1 or 0) + (grid_l2 or 0)
                    grid_src = "modbus"

                with _live_lock:
                    _live_cache.update({
                        "soc": soc,
                        "grid_w": grid_w,
                        "grid_l1": grid_l1, "grid_l2": grid_l2, "grid_l3": grid_l3,
                        "grid_source": grid_src,
                        "solar_w": solar_w,
                        "battery_w": bat_raw,
                        "updated": datetime.now(OSLO_TZ).isoformat(),
                        "error": None
                    })
        except Exception as e:
            connected = False
            with _live_lock:
                _live_cache["error"] = str(e)
        time.sleep(10)

# Start Modbus-polling kun om VICTRON_HOST er satt
if os.getenv("VICTRON_HOST"):
    _t = threading.Thread(target=_poll_cerbo, daemon=True)
    _t.start()

def get_prices_cached():
    with _price_lock:
        now = datetime.now(OSLO_TZ)
        if not _price_cache["fetched"] or (now - _price_cache["fetched"]).seconds > 1800:
            try:
                _price_cache["data"] = fetcher.get_prices(36)  # Hent i dag + i morgen (alle tilgjengelige)
                _price_cache["fetched"] = now
            except Exception:
                pass
        return _price_cache["data"]


@app.route("/api/status")
def api_status():
    prices = get_prices_cached()
    current = prices[0] if prices else None
    stats = tracker.get_stats()

    spot_ore = current.price_ore_kwh / CONFIG.vat if current else 0
    hour_now = datetime.now(OSLO_TZ).hour
    buy_ore = buy_price_ore(spot_ore, hour_now) if current else 0
    sell_ore = sell_price_ore(spot_ore)
    discharge_margin = round(sell_ore - buy_ore, 1)  # Positivt = lønnsomt å selge

    return jsonify({
        "timestamp": datetime.now(OSLO_TZ).isoformat(),
        "price": {
            "spot_ore": round(spot_ore, 1),
            "buy_ore": round(buy_ore, 1),
            "sell_ore": round(sell_ore, 2),
            "margin_ore": round(sell_ore - buy_ore, 1),
            "discharge_margin_ore": discharge_margin,
        },
        "profit": {
            "today_nok": round(stats.get("today_profit_nok", 0), 2),
            "total_nok": round(stats.get("total_profit_nok", 0), 2),
            "today_bought_kwh": round(stats.get("today_bought_kwh", 0), 1),
            "today_sold_kwh": round(stats.get("today_sold_kwh", 0), 1),
        },
        "capacity_charge_nok": CAPACITY_CHARGE_NOK,
        "solar_max_kw": CONFIG.solar_max_kw,
        "min_spread_ore": int(CONFIG.min_price_diff_nok * 100),
        "min_soc": int(CONFIG.min_soc) if hasattr(CONFIG, 'min_soc') else 20,
        "max_soc": int(CONFIG.max_soc) if hasattr(CONFIG, 'max_soc') else 90,
    })


@app.route("/api/prices")
def api_prices():
    prices = get_prices_cached()
    return jsonify([{
        "time": p.timestamp.astimezone(OSLO_TZ).strftime("%d.%m %H:%M"),
        "spot_ore": round(p.price_ore_kwh / CONFIG.vat, 1),
        "buy_ore": round(buy_price_ore(p.price_ore_kwh / CONFIG.vat, p.timestamp.astimezone(OSLO_TZ).hour), 1),
        "sell_ore": round(sell_price_ore(p.price_ore_kwh / CONFIG.vat), 2),
    } for p in prices])


@app.route("/api/trades")
def api_trades():
    trades = tracker.get_recent_trades(20)
    return jsonify(trades)


@app.route("/api/trades/hourly")
def api_trades_hourly():
    """Trades gruppert per time med sum kjøpt/solgt."""
    hours = request.args.get('hours', 24, type=int)
    return jsonify(tracker.get_hourly_trades(hours))


@app.route("/api/live")
def api_live():
    """Live data fra Cerbo GX via Modbus."""
    with _live_lock:
        return jsonify(dict(_live_cache))


@app.route("/api/activity")
def api_activity():
    """Live trader-aktivitet: current_action + live power."""
    state_file = os.path.join(os.path.dirname(CONFIG.db_path) or ".", "trader_state.json")
    current_action = {"action": "idle", "power_kw": 0.0, "reason": "", "since": None}
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
            if state.get("action") and state.get("action") != "idle":
                current_action = {
                    "action": state.get("action", "idle"),
                    "power_kw": state.get("power_kw", 0.0),
                    "reason": state.get("reason", ""),
                    "since": state.get("timestamp", ""),
                }
        except Exception:
            pass
    with _live_lock:
        live = dict(_live_cache)
    return jsonify({
        "current_action": current_action,
        "battery_w": live.get("battery_w", 0),
        "solar_w": live.get("solar_w", 0),
        "grid_w": live.get("grid_w", 0),
        "soc": live.get("soc", 0),
        "updated": live.get("updated"),
    })


@app.route("/api/plan")
def api_plan():
    try:
        from optimizer import Optimizer
        prices = get_prices_cached()
        if not prices:
            return jsonify([])
        opt = Optimizer()
        current_soc = _live_cache.get("soc", 70.0)
        plan, _ = opt.optimize(prices, current_soc=current_soc)
        return jsonify([{
            "time": a.timestamp.astimezone(OSLO_TZ).strftime("%d.%m %H:%M"),
            "action": a.action,
            "power_kw": round(a.power_kw, 1),
            "reason": a.reason,
            "profit_nok": round(a.expected_profit_nok, 3),
        } for a in plan])
    except Exception as e:
        import traceback
        error_msg = f"Plan API error: {str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        return jsonify({"error": str(e)}), 500


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Abelgård Energi</title>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg: #050d1a;
  --panel: rgba(10,25,47,0.85);
  --border: rgba(0,180,255,0.15);
  --accent: #00b4ff;
  --solar: #f59e0b;
  --grid-col: #60a5fa;
  --bat: #22c55e;
  --red: #ef4444;
  --text: #e2f0ff;
  --muted: #4a6080;
}

body {
  font-family: 'Rajdhani', sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── SKY SCENE ── */
.sky {
  position: relative;
  width: 100%;
  height: 260px;
  background: linear-gradient(180deg, #020a18 0%, #0a1e3d 40%, #1a3a6b 70%, #0d2442 100%);
  overflow: hidden;
}

/* Stars */
.sky::before {
  content: '';
  position: absolute; inset: 0;
  background-image:
    radial-gradient(1px 1px at 10% 20%, rgba(255,255,255,0.8) 0%, transparent 100%),
    radial-gradient(1px 1px at 25% 8%, rgba(255,255,255,0.6) 0%, transparent 100%),
    radial-gradient(1px 1px at 40% 15%, rgba(255,255,255,0.9) 0%, transparent 100%),
    radial-gradient(1px 1px at 60% 5%, rgba(255,255,255,0.7) 0%, transparent 100%),
    radial-gradient(1px 1px at 75% 18%, rgba(255,255,255,0.5) 0%, transparent 100%),
    radial-gradient(1px 1px at 88% 12%, rgba(255,255,255,0.8) 0%, transparent 100%),
    radial-gradient(1px 1px at 15% 35%, rgba(255,255,255,0.4) 0%, transparent 100%),
    radial-gradient(1px 1px at 50% 28%, rgba(255,255,255,0.6) 0%, transparent 100%),
    radial-gradient(1px 1px at 82% 30%, rgba(255,255,255,0.5) 0%, transparent 100%);
}

/* Sol-bue SVG */
.sun-arc-wrap {
  position: absolute;
  width: 100%; height: 100%;
  top: 0; left: 0;
}

#sunArcSvg {
  width: 100%; height: 100%;
}

/* Sol-orb */
.sun-orb {
  position: absolute;
  width: 36px; height: 36px;
  border-radius: 50%;
  background: radial-gradient(circle, #fff9c4 0%, #fbbf24 40%, #f59e0b 70%, transparent 100%);
  box-shadow: 0 0 30px 15px rgba(251,191,36,0.5), 0 0 60px 30px rgba(251,191,36,0.2);
  transform: translate(-50%, -50%);
  transition: left 1s ease, top 1s ease;
}

/* Horizon / ground */
.horizon {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  height: 60px;
  background: linear-gradient(180deg, transparent 0%, rgba(0,20,50,0.8) 100%);
}

/* Sunrise/sunset labels */
.sun-time {
  position: absolute;
  bottom: 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.72rem;
  color: rgba(255,255,255,0.5);
}
.sun-time.rise { left: 8%; }
.sun-time.noon { left: 50%; transform: translateX(-50%); color: rgba(251,191,36,0.7); }
.sun-time.set  { right: 8%; }

/* Header overlay */
.header-overlay {
  position: absolute;
  top: 0; left: 0; right: 0;
  padding: 1rem 1.5rem;
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.header-overlay h1 {
  font-size: 1.3rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  color: #fff;
  text-shadow: 0 0 20px rgba(0,180,255,0.8);
}
.live-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 6px #22c55e;
  animation: pulse 1.5s infinite;
  margin-left: auto;
}
@keyframes pulse { 0%,100%{opacity:1;box-shadow:0 0 6px #22c55e;} 50%{opacity:0.4;box-shadow:0 0 2px #22c55e;} }

.action-badge {
  display: inline-block;
  padding: 0.2rem 0.75rem;
  border-radius: 9999px;
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  border: 1px solid;
}

/* ── ENERGY FLOW SCENE ── */
.flow-scene {
  position: relative;
  background: linear-gradient(180deg, #0d2442 0%, #050d1a 100%);
  padding: 0.5rem 1.5rem 1rem;
}

.flow-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  max-width: 600px;
  margin: 0 auto;
}

.flow-node {
  text-align: center;
  flex: 1;
}

.flow-icon {
  font-size: 2.2rem;
  margin-bottom: 0.3rem;
  display: block;
}

.flow-label {
  font-size: 0.65rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.flow-value {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1.1rem;
  font-weight: 600;
  margin: 0.15rem 0;
}

/* Animated flow arrow */
.flow-arrow {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  position: relative;
  width: 60px;
}

.arrow-line {
  height: 2px;
  width: 100%;
  border-radius: 2px;
  position: relative;
  overflow: hidden;
  background: rgba(255,255,255,0.08);
}

.arrow-line::after {
  content: '';
  position: absolute;
  top: 0; left: -100%;
  width: 50%;
  height: 100%;
  border-radius: 2px;
  animation: flow-anim 1.2s linear infinite;
}

.arrow-line.active-right::after {
  animation: flow-right 1.2s linear infinite;
}
.arrow-line.active-left::after {
  animation: flow-left 1.2s linear infinite;
}

@keyframes flow-right {
  0%  { left: -50%; }
  100%{ left: 100%; }
}
@keyframes flow-left {
  0%  { left: 100%; }
  100%{ left: -50%; }
}

.arrow-w {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.7rem;
  color: var(--muted);
  white-space: nowrap;
}

/* ── BATTERY VISUAL ── */
.battery-visual {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.3rem;
  flex: 1;
}

.bat-shell {
  width: 52px;
  height: 80px;
  border: 2px solid rgba(34,197,94,0.6);
  border-radius: 6px;
  position: relative;
  background: rgba(0,0,0,0.3);
  overflow: hidden;
}

.bat-nub {
  width: 18px; height: 5px;
  background: rgba(34,197,94,0.6);
  border-radius: 2px 2px 0 0;
  margin: 0 auto;
  position: relative; top: -5px;
}

.bat-fill {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  background: linear-gradient(180deg, #16a34a 0%, #22c55e 100%);
  transition: height 1s ease;
  box-shadow: 0 -4px 12px rgba(34,197,94,0.4);
}

.bat-pct {
  position: absolute;
  inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.9rem;
  font-weight: 700;
  color: #fff;
  text-shadow: 0 1px 4px rgba(0,0,0,0.8);
  z-index: 2;
}

/* ── CARDS ── */
.cards-section {
  padding: 0.75rem 1.5rem;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.6rem;
  max-width: 700px;
  margin: 0 auto;
}

@media(min-width: 600px) {
  .cards-section { grid-template-columns: repeat(4, 1fr); }
}

.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0.8rem 0.9rem;
  backdrop-filter: blur(8px);
  position: relative;
  overflow: hidden;
}

.card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: var(--card-accent, var(--accent));
  opacity: 0.6;
}

.card-icon { font-size: 1.2rem; margin-bottom: 0.3rem; display: block; }
.card-label { font-size: 0.62rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
.card-val {
  font-family: 'JetBrains Mono', monospace;
  font-size: 1.3rem;
  font-weight: 600;
  margin: 0.15rem 0 0.1rem;
  line-height: 1;
}
.card-sub { font-size: 0.65rem; color: var(--muted); }

/* ── PRICE STRIP ── */
.price-strip {
  padding: 0.75rem 1.5rem;
  max-width: 700px;
  margin: 0 auto;
}

.price-strip h2 {
  font-size: 0.7rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 0.5rem;
}

.price-bars {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 60px;
}

.price-bar-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
  gap: 2px;
}

.price-bar {
  width: 100%;
  border-radius: 2px 2px 0 0;
  min-height: 3px;
  transition: height 0.5s ease;
}

.price-bar-time {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.5rem;
  color: var(--muted);
  white-space: nowrap;
}

.price-bar-now {
  background: var(--accent) !important;
  box-shadow: 0 0 6px var(--accent);
}

/* ── TRADE LOG ── */
.trade-section {
  padding: 0.5rem 1.5rem 1.5rem;
  max-width: 700px;
  margin: 0 auto;
}

.trade-section h2 {
  font-size: 0.7rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 0.5rem;
}

.trade-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  font-size: 0.78rem;
}

.trade-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.trade-time { font-family: 'JetBrains Mono', monospace; color: var(--muted); font-size: 0.7rem; min-width: 38px; }
.trade-type { font-weight: 600; min-width: 42px; }
.trade-kwh { font-family: 'JetBrains Mono', monospace; color: var(--muted); margin-left: auto; font-size: 0.7rem; }
.trade-profit { font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; min-width: 60px; text-align: right; }

.update-line {
  font-size: 0.62rem;
  color: var(--muted);
  text-align: right;
  padding: 0.3rem 1.5rem 0.5rem;
  font-family: 'JetBrains Mono', monospace;
}

/* ── PLAN TABLE ── */
.plan-section {
  padding: 0.5rem 1.5rem 1.5rem;
  max-width: 700px;
  margin: 0 auto;
}

.plan-section h2 {
  font-size: 0.7rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 0.5rem;
}

.plan-table {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.75rem;
}

.plan-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.35rem 0.5rem;
  background: rgba(255,255,255,0.03);
  border-radius: 6px;
  border-left: 3px solid transparent;
}

.plan-row.charge { border-left-color: #22c55e; }
.plan-row.discharge { border-left-color: #fb923c; }
.plan-row.idle { border-left-color: #60a5fa; }

.plan-time { font-family: 'JetBrains Mono', monospace; color: var(--muted); min-width: 55px; }
.plan-action { font-weight: 600; min-width: 70px; text-transform: uppercase; font-size: 0.7rem; }
.plan-action.charge { color: #22c55e; }
.plan-action.discharge { color: #fb923c; }
.plan-action.idle { color: #60a5fa; }
.plan-reason { color: var(--muted); flex: 1; font-size: 0.7rem; }
.plan-profit { font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; min-width: 50px; text-align: right; }
.plan-profit.positive { color: #22c55e; }
.plan-profit.negative { color: #ef4444; }
.plan-power { font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; min-width: 50px; text-align: right; color: var(--muted); }

/* ── SETTINGS ── */
.settings-section {
  padding: 0.5rem 1.5rem 1.5rem;
  max-width: 700px;
  margin: 0 auto;
}

.settings-section h2 {
  font-size: 0.7rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 0.5rem;
}

.settings-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.5rem;
}

.setting-item {
  display: flex;
  justify-content: space-between;
  padding: 0.5rem 0.75rem;
  background: rgba(255,255,255,0.03);
  border-radius: 6px;
  font-size: 0.75rem;
}

.setting-label { color: var(--muted); }
.setting-val { font-family: 'JetBrains Mono', monospace; color: var(--text); }

/* ── SOLAR FORECAST ── */
.solar-section {
  padding: 0.5rem 1.5rem 1.5rem;
  max-width: 700px;
  margin: 0 auto;
}

.solar-section h2 {
  font-size: 0.7rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 0.5rem;
}

.solar-forecast {
  display: flex;
  gap: 1rem;
}

.forecast-item {
  flex: 1;
  display: flex;
  justify-content: space-between;
  padding: 0.5rem 0.75rem;
  background: rgba(255,255,255,0.03);
  border-radius: 6px;
  font-size: 0.75rem;
}

.forecast-label { color: var(--muted); }
.forecast-val { font-family: 'JetBrains Mono', monospace; color: var(--solar); }

</style>
</head>
<body>

<!-- SKY SCENE -->
<div class="sky" id="skyScene">
  <div class="sun-arc-wrap">
    <svg id="sunArcSvg" viewBox="0 0 100 40" preserveAspectRatio="none">
      <path id="arcPath" d="M5,38 Q50,-5 95,38"
        fill="none" stroke="rgba(251,191,36,0.25)" stroke-width="0.4"
        stroke-dasharray="1,1"/>
    </svg>
  </div>
  <div class="sun-orb" id="sunOrb"></div>
  <div class="horizon"></div>
  <span class="sun-time rise" id="labelRise">05:07</span>
  <span class="sun-time noon">12:00</span>
  <span class="sun-time set" id="labelSet">21:15</span>

  <div class="header-overlay">
    <h1>⚡ ABELGÅRD</h1>
    <span class="action-badge" id="actionBadge" style="border-color:#60a5fa;color:#60a5fa;">IDLE</span>
    <div class="live-dot"></div>
  </div>
</div>

<!-- ENERGY FLOW -->
<div class="flow-scene">
  <div class="flow-row">

    <!-- Grid -->
    <div class="flow-node">
      <span class="flow-icon">🔌</span>
      <div class="flow-label">Grid</div>
      <div class="flow-value" id="gridW" style="color:var(--grid-col)">— W</div>
    </div>

    <!-- Arrow grid→hus -->
    <div class="flow-arrow">
      <div class="arrow-line" id="arrowGrid" style="--c:#60a5fa;"></div>
      <div class="arrow-w" id="arrowGridW"></div>
    </div>

    <!-- Hus -->
    <div class="flow-node">
      <span class="flow-icon">🏠</span>
      <div class="flow-label">Forbruk</div>
      <div class="flow-value" id="loadW" style="color:#e2e8f0">— W</div>
    </div>

    <!-- Arrow sol→hus -->
    <div class="flow-arrow">
      <div class="arrow-line" id="arrowSolar" style="--c:#f59e0b;"></div>
      <div class="arrow-w" id="arrowSolarW"></div>
    </div>

    <!-- Sol -->
    <div class="flow-node">
      <span class="flow-icon">☀️</span>
      <div class="flow-label">Sol</div>
      <div class="flow-value" id="solarW" style="color:var(--solar)">— W</div>
    </div>

    <!-- Arrow bat -->
    <div class="flow-arrow">
      <div class="arrow-line" id="arrowBat" style="--c:#22c55e;"></div>
      <div class="arrow-w" id="arrowBatW"></div>
    </div>

    <!-- Batteri -->
    <div class="flow-node battery-visual">
      <div class="bat-nub"></div>
      <div class="bat-shell">
        <div class="bat-fill" id="batFill" style="height:0%"></div>
        <div class="bat-pct" id="batPct">—%</div>
      </div>
      <div class="flow-label" style="margin-top:0.3rem">Batteri</div>
      <div class="flow-value" id="batW" style="color:var(--bat);font-size:0.85rem">— W</div>
    </div>

  </div>
</div>

<!-- STATS CARDS -->
<div class="cards-section">
  <div class="card" style="--card-accent:var(--solar)">
    <span class="card-icon">🌤️</span>
    <div class="card-label">Sol i dag</div>
    <div class="card-val" id="cardSolarToday">— kWh</div>
    <div class="card-sub" id="cardSolarForecast">prognose i morgen: —</div>
  </div>
  <div class="card" style="--card-accent:var(--bat)">
    <span class="card-icon">🔋</span>
    <div class="card-label">Batteri</div>
    <div class="card-val" id="cardBatPct">—%</div>
    <div class="card-sub" id="cardBatSub">— W</div>
  </div>
  <div class="card" style="--card-accent:#a78bfa">
    <span class="card-icon">💰</span>
    <div class="card-label">Spotpris</div>
    <div class="card-val" id="cardSpot">— ø</div>
    <div class="card-sub" id="cardBuyOre">kjøp: — ø</div>
  </div>
  <div class="card" style="--card-accent:#34d399">
    <span class="card-icon">📈</span>
    <div class="card-label">Profitt i dag</div>
    <div class="card-val" id="cardProfit">— kr</div>
    <div class="card-sub" id="cardProfitSub">total: — kr</div>
  </div>
</div>

<!-- PRICE BARS -->
<div class="price-strip">
  <h2>Spotpris neste 24t</h2>
  <div class="price-bars" id="priceBars"></div>
</div>

<!-- TRADE LOG -->
<div class="trade-section">
  <h2>Siste handler</h2>
  <div id="tradeLog"></div>
</div>

<!-- PLAN TABLE -->
<div class="plan-section">
  <h2>Handelsplan neste 24t</h2>
  <div class="plan-table" id="planTable"></div>
</div>

<!-- SETTINGS -->
<div class="settings-section">
  <h2>Innstillinger</h2>
  <div class="settings-grid" id="settingsGrid">
    <div class="setting-item">
      <span class="setting-label">Min SOC:</span>
      <span class="setting-val" id="minSoc">--%</span>
    </div>
    <div class="setting-item">
      <span class="setting-label">Max SOC:</span>
      <span class="setting-val" id="maxSoc">--%</span>
    </div>
    <div class="setting-item">
      <span class="setting-label">Min spread:</span>
      <span class="setting-val" id="minSpread">--ø</span>
    </div>
    <div class="setting-item">
      <span class="setting-label">Kapasitetsavgift:</span>
      <span class="setting-val" id="capCharge">-- kr</span>
    </div>
  </div>
</div>

<!-- SOLAR FORECAST -->
<div class="solar-section">
  <h2>Solprognose</h2>
  <div class="solar-forecast" id="solarForecast">
    <div class="forecast-item">
      <span class="forecast-label">I dag:</span>
      <span class="forecast-val" id="solarToday">-- kWh</span>
    </div>
    <div class="forecast-item">
      <span class="forecast-label">I morgen:</span>
      <span class="forecast-val" id="solarTomorrow">-- kWh</span>
    </div>
  </div>
</div>

<div class="update-line" id="updateLine">oppdaterer...</div>

<script>
// ── SUN ARC ──
const SITE_LAT = 60.14, SITE_LON = 10.25;

function getSunTimes(date) {
  // Enkel soloppgang/solnedgang-beregning for fast breddegrad
  const J = date.getTime()/86400000 + 2440587.5;
  const n = J - 2451545.0 + 0.0008;
  const Jstar = n - SITE_LON/360;
  const M = (357.5291 + 0.98560028*Jstar) % 360;
  const C = 1.9148*Math.sin(M*Math.PI/180) + 0.0200*Math.sin(2*M*Math.PI/180) + 0.0003*Math.sin(3*M*Math.PI/180);
  const lam = (M + C + 180 + 102.9372) % 360;
  const Jtransit = 2451545.0 + Jstar + 0.0053*Math.sin(M*Math.PI/180) - 0.0069*Math.sin(2*lam*Math.PI/180);
  const sinDec = Math.sin(lam*Math.PI/180)*Math.sin(23.44*Math.PI/180);
  const cosHa = (Math.sin(-0.83*Math.PI/180) - Math.sin(SITE_LAT*Math.PI/180)*sinDec)
               / (Math.cos(SITE_LAT*Math.PI/180)*Math.cos(Math.asin(sinDec)));
  if (Math.abs(cosHa) > 1) return null;
  const Ha = Math.acos(cosHa)*180/Math.PI;
  const Jrise = Jtransit - Ha/360;
  const Jset  = Jtransit + Ha/360;
  const toDate = J0 => new Date((J0 - 2440587.5)*86400000);
  return { rise: toDate(Jrise), set: toDate(Jset), transit: toDate(Jtransit) };
}

function pad2(n){ return String(n).padStart(2,'0'); }
function fmtTime(d){ return d ? `${pad2(d.getHours())}:${pad2(d.getMinutes())}` : '--:--'; }

function updateSun() {
  const now = new Date();
  const sun = getSunTimes(now);
  if (!sun) return;

  document.getElementById('labelRise').textContent = fmtTime(sun.rise);
  document.getElementById('labelSet').textContent  = fmtTime(sun.set);

  // Sol-posisjon langs buen (0–1)
  const total = sun.set - sun.rise;
  const elapsed = now - sun.rise;
  const t = Math.max(0, Math.min(1, elapsed / total));

  // Parametrisk punkt på kvadratisk Bezier: P(t) = (1-t)^2 * P0 + 2t(1-t)*P1 + t^2*P2
  // P0=(5%,97%), P1=(50%,-12%), P2=(95%,97%) — i prosent av sky-boksen
  const p0x=5, p0y=97, p1x=50, p1y=-12, p2x=95, p2y=97;
  const sx = (1-t)*(1-t)*p0x + 2*t*(1-t)*p1x + t*t*p2x;
  const sy = (1-t)*(1-t)*p0y + 2*t*(1-t)*p1y + t*t*p2y;

  const sky = document.getElementById('skyScene');
  const W = sky.offsetWidth, H = sky.offsetHeight;
  const orb = document.getElementById('sunOrb');
  orb.style.left = (sx/100*W) + 'px';
  orb.style.top  = (sy/100*H) + 'px';

  // Glød ved horisonten
  const nearHorizon = t < 0.08 || t > 0.92;
  orb.style.boxShadow = nearHorizon
    ? '0 0 40px 20px rgba(251,120,36,0.7), 0 0 80px 40px rgba(251,120,36,0.3)'
    : '0 0 30px 15px rgba(251,191,36,0.5), 0 0 60px 30px rgba(251,191,36,0.2)';

  // Skjul orb om natten
  const isDay = now >= sun.rise && now <= sun.set;
  orb.style.display = isDay ? 'block' : 'none';
}

// ── FLOW ARROWS ──
function setArrow(id, wattId, watts, direction, color) {
  const line = document.getElementById(id);
  const label = document.getElementById(wattId);
  if (Math.abs(watts) < 20) {
    line.className = 'arrow-line';
    line.style.background = 'rgba(255,255,255,0.06)';
    if (label) label.textContent = '';
    return;
  }
  line.className = 'arrow-line ' + (direction === 'right' ? 'active-right' : 'active-left');
  line.style.background = `linear-gradient(90deg, transparent, ${color}, transparent)`;
  line.style.setProperty('--c', color);
  if (line.querySelector && !line._pseudo) {
    // inject pseudo via dynamic style
  }
  // override ::after color via a sibling approach
  line.style.backgroundSize = '200%';
  if (label) label.textContent = Math.abs(Math.round(watts)) + ' W';
}

// ── DATA FETCH ──
async function fetchAll() {
  console.log('fetchAll called');
  try {
    const safeFetch = async (url, defaultVal) => {
      try {
        const r = await fetch(url);
        if (!r.ok) {
          console.error(`API ${url} returned ${r.status}`);
          return defaultVal;
        }
        return await r.json();
      } catch(e) {
        console.error(`API ${url} error:`, e);
        return defaultVal;
      }
    };
    
    const [live, status, trades, prices, plan] = await Promise.all([
      safeFetch('/api/live', {}),
      safeFetch('/api/status', {}),
      safeFetch('/api/trades', []),
      safeFetch('/api/prices', []),
      safeFetch('/api/plan', []),
    ]);
    console.log('Data fetched:', {live, status, trades: trades?.length, prices: prices?.length, plan: plan?.length});
    updateUI(live, status, trades, prices, plan);
  } catch(e) {
    console.error('fetchAll error:', e);
  }
}

function fmtW(w) {
  if (w == null) return '— W';
  const abs = Math.abs(w);
  return abs >= 1000 ? (w/1000).toFixed(1)+' kW' : Math.round(w)+' W';
}

function updateUI(live, status, trades, prices, plan) {
  const soc     = live.soc ?? 0;
  const solarW  = live.solar_w ?? 0;
  const gridW   = live.grid_w ?? 0;
  const batW    = live.battery_w ?? 0;
  // Estimated load = solar + grid import - grid export + battery discharge
  const loadW = Math.max(0, solarW + Math.max(0, gridW) - Math.max(0, -gridW) + Math.max(0, -batW) - Math.max(0, batW));
  const loadEst = solarW + gridW - batW;

  // Grid display
  document.getElementById('gridW').textContent = fmtW(gridW);
  document.getElementById('gridW').style.color = gridW > 50 ? '#60a5fa' : gridW < -50 ? '#22c55e' : '#94a3b8';

  document.getElementById('solarW').textContent = fmtW(solarW);
  document.getElementById('loadW').textContent  = fmtW(Math.max(0, loadEst));
  document.getElementById('batW').textContent   = fmtW(batW);
  document.getElementById('batW').style.color   = batW > 50 ? '#22c55e' : batW < -50 ? '#fb923c' : '#94a3b8';

  // Battery visual
  document.getElementById('batFill').style.height = Math.max(0, Math.min(100, soc)) + '%';
  document.getElementById('batPct').textContent   = soc ? soc.toFixed(1)+'%' : '—%';
  document.getElementById('cardBatPct').textContent = soc ? soc.toFixed(1)+'%' : '—%';
  document.getElementById('cardBatSub').textContent = fmtW(batW) + (batW > 50 ? ' 🔼' : batW < -50 ? ' 🔽' : '');

  // Battery fill color by SOC
  const fill = document.getElementById('batFill');
  fill.style.background = soc > 70
    ? 'linear-gradient(180deg,#15803d 0%,#22c55e 100%)'
    : soc > 35
    ? 'linear-gradient(180deg,#ca8a04 0%,#facc15 100%)'
    : 'linear-gradient(180deg,#b91c1c 0%,#ef4444 100%)';
  fill.style.boxShadow = soc > 70
    ? '0 -4px 12px rgba(34,197,94,0.4)'
    : soc > 35
    ? '0 -4px 12px rgba(250,204,21,0.4)'
    : '0 -4px 12px rgba(239,68,68,0.4)';

  // Arrows
  // Grid → Hus (import positive, export negative)
  if (gridW > 50) setArrow('arrowGrid','arrowGridW', gridW, 'right', '#60a5fa');
  else if (gridW < -50) setArrow('arrowGrid','arrowGridW', gridW, 'left', '#22c55e');
  else setArrow('arrowGrid','arrowGridW', 0, 'right', '#60a5fa');

  // Sol → Hus
  if (solarW > 50) setArrow('arrowSolar','arrowSolarW', solarW, 'left', '#f59e0b');
  else setArrow('arrowSolar','arrowSolarW', 0, 'right', '#f59e0b');

  // Bat ↕
  if (batW > 50) setArrow('arrowBat','arrowBatW', batW, 'left', '#22c55e');
  else if (batW < -50) setArrow('arrowBat','arrowBatW', batW, 'right', '#fb923c');
  else setArrow('arrowBat','arrowBatW', 0, 'right', '#22c55e');

  // Cards
  document.getElementById('cardSpot').textContent   = status.price ? Math.round(status.price.spot_ore)+' ø' : '— ø';
  document.getElementById('cardBuyOre').textContent = status.price ? 'kjøp: '+Math.round(status.price.buy_ore)+' ø' : '';
  document.getElementById('cardProfit').textContent  = status.profit ? (status.profit.today_nok >= 0 ? '+' : '')+status.profit.today_nok.toFixed(2)+' kr' : '— kr';
  document.getElementById('cardProfitSub').textContent = status.profit ? 'total: '+status.profit.total_nok.toFixed(2)+' kr' : '';

  // Solar today (use sold_kwh as proxy, or show from live)
  document.getElementById('cardSolarToday').textContent = solarW >= 0 ? (solarW/1000).toFixed(2)+' kW nå' : '—';

  // Update trades
  if (trades && Array.isArray(trades)) {
    const tradeLog = document.getElementById('tradeLog');
    tradeLog.innerHTML = trades.slice(0, 10).map(t => {
      const profit = t.net_profit_nok || 0;
      const isPos = profit >= 0;
      const color = isPos ? '#22c55e' : '#ef4444';
      return `<div class="trade-row">
        <div class="trade-dot" style="background:${t.action === 'buy' ? '#22c55e' : '#fb923c'}"></div>
        <span class="trade-time">${t.timestamp?.split('T')[1]?.slice(0,5) || '--:--'}</span>
        <span class="trade-type">${t.action === 'buy' ? 'KJØPT' : 'SOLGT'}</span>
        <span class="trade-kwh">${(t.energy_kwh || 0).toFixed(2)} kWh</span>
        <span class="trade-profit" style="color:${color}">${isPos ? '+' : ''}${profit.toFixed(2)} kr</span>
      </div>`;
    }).join('');
  }

  // Update plan
  if (plan && Array.isArray(plan)) {
    const planTable = document.getElementById('planTable');
    planTable.innerHTML = plan.slice(0, 24).map(p => {
      const profit = p.profit_nok || 0;
      const isPos = profit > 0;
      const cls = p.action || 'idle';
      return `<div class="plan-row ${cls}">
        <span class="plan-time">${p.time || '--'}</span>
        <span class="plan-action ${cls}">${cls.toUpperCase()}</span>
        <span class="plan-reason">${p.reason || ''}</span>
        <span class="plan-power">${p.power_kw ? p.power_kw.toFixed(1) : '0'} kW</span>
      </div>`;
    }).join('');
  }

  // Update settings
  if (status) {
    document.getElementById('minSoc').textContent = (status.min_soc || 20) + '%';
    document.getElementById('maxSoc').textContent = (status.max_soc || 90) + '%';
    document.getElementById('minSpread').textContent = (status.min_spread_ore || 110) + 'ø';
    document.getElementById('capCharge').textContent = (status.capacity_charge_nok || 117) + ' kr';
  }

  // Update solar forecast (placeholder - fetch from solar API if available)
  document.getElementById('solarToday').textContent = 'Estimerer...';
  document.getElementById('solarTomorrow').textContent = 'Estimerer...';

  // Action badge
  try {
    fetch('/api/activity').then(r=>r.json()).then(act => {
      const badge = document.getElementById('actionBadge');
      const a = act.current_action?.action || 'idle';
      const colors = {charge:'#22c55e', discharge:'#fb923c', idle:'#60a5fa'};
      const labels = {charge:'LADER', discharge:'UTLADER', idle:'IDLE'};
      badge.textContent = labels[a] || a.toUpperCase();
      badge.style.color = colors[a] || '#60a5fa';
      badge.style.borderColor = colors[a] || '#60a5fa';
      badge.style.background = (colors[a] || '#60a5fa')+'22';
    });
  } catch(e){}

  // Price bars
  renderPriceBars(prices);

  // Trades
  renderTrades(trades);

  // Update time
  const upd = live.updated ? new Date(live.updated) : new Date();
  document.getElementById('updateLine').textContent = 'Oppdatert ' + fmtTime(upd);
}

function renderPriceBars(prices) {
  if (!prices || !prices.length) return;
  const container = document.getElementById('priceBars');
  const maxOre = Math.max(...prices.map(p=>p.buy_ore));
  const now = new Date();
  const nowH = now.getHours();
  container.innerHTML = prices.slice(0,24).map((p,i) => {
    const h = parseInt(p.time.slice(-5));
    const isCurrent = p.time.includes(pad2(nowH)+':');
    const pct = Math.max(8, Math.round(p.buy_ore/maxOre*100));
    const color = p.buy_ore > maxOre*0.8 ? '#ef4444' : p.buy_ore < maxOre*0.4 ? '#22c55e' : '#60a5fa';
    return `<div class="price-bar-wrap">
      <div class="price-bar${isCurrent?' price-bar-now':''}" 
           style="height:${pct}%;background:${isCurrent?'var(--accent)':color}33;border-top:2px solid ${isCurrent?'var(--accent)':color};"
           title="${p.buy_ore.toFixed(0)}ø"></div>
      <div class="price-bar-time">${p.time.slice(-5,-3)}</div>
    </div>`;
  }).join('');
}

function renderTrades(trades) {
  const container = document.getElementById('tradeLog');
  if (!trades || !trades.length) {
    container.innerHTML = '<div style="color:var(--muted);font-size:0.78rem;padding:0.5rem 0">Ingen handler ennå</div>';
    return;
  }
  container.innerHTML = trades.slice(0,8).map(t => {
    const isBuy = t.action === 'buy';
    const color = isBuy ? '#60a5fa' : '#22c55e';
    const profit = t.net_profit_nok >= 0 ? '+'+t.net_profit_nok.toFixed(2) : t.net_profit_nok.toFixed(2);
    const profitColor = t.net_profit_nok >= 0 ? '#22c55e' : '#ef4444';
    const time = t.timestamp ? t.timestamp.slice(11,16) : '--:--';
    return `<div class="trade-row">
      <div class="trade-dot" style="background:${color}"></div>
      <div class="trade-time">${time}</div>
      <div class="trade-type" style="color:${color}">${isBuy ? 'Kjøp' : 'Salg'}</div>
      <div style="color:var(--muted);font-size:0.7rem">${t.energy_kwh?.toFixed(1)||'—'} kWh @ ${Math.round((t.price_nok_kwh||0)*100)}ø</div>
      <div class="trade-profit" style="color:${profitColor}">${profit} kr</div>
    </div>`;
  }).join('');
}

// ── INIT ──
updateSun();
setInterval(updateSun, 60000);
fetchAll();
setInterval(fetchAll, 10000);
</script>
</body>
</html>

"""

if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
