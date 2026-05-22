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


@app.route("/api/solar")
def api_solar():
    """Sol-prognose fra Open-Meteo for i dag og i morgen."""
    try:
        from solar_forecast import get_solar_kwh_tomorrow, _fetch_radiation
        from datetime import date, timedelta
        import math
        kwh_tomorrow = get_solar_kwh_tomorrow(CONFIG.site_lat, CONFIG.site_lon, CONFIG.solar_max_kw)
        data = _fetch_radiation(CONFIG.site_lat, CONFIG.site_lon)
        times = data["hourly"]["time"]
        swrad = data["hourly"]["shortwave_radiation"]
        today_str    = date.today().isoformat()
        tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
        def build_profile(day_str):
            out = []
            for i, t in enumerate(times):
                if not t.startswith(day_str): continue
                h = int(t[11:13])
                if h < 4 or h > 22: continue
                kw = round(min(CONFIG.solar_max_kw, (swrad[i] or 0) / 1000 * CONFIG.solar_max_kw * CONFIG.solar_system_efficiency), 2)
                out.append({"hour": h, "kw": kw, "wm2": round(swrad[i] or 0)})
            return out
        today_profile    = build_profile(today_str)
        tomorrow_profile = build_profile(tomorrow_str)
        kwh_today = round(sum(p["kw"] for p in today_profile), 1)
        return jsonify({
            "kwh_today":    kwh_today,
            "kwh_tomorrow": round(kwh_tomorrow, 1),
            "charge_target_soc": round(CONFIG.max_soc - min(40, (kwh_tomorrow / CONFIG.battery_capacity_kwh) * 100), 1),
            "today_profile":    today_profile,
            "tomorrow_profile": tomorrow_profile,
        })
    except Exception as e:
        return jsonify({"error": str(e), "kwh_today": 0, "kwh_tomorrow": 0, "today_profile": [], "tomorrow_profile": []})


@app.route("/api/daily_plan")
def api_daily_plan():
    """Hent siste daily_plan-rader fra SQLite."""
    try:
        rows = tracker.get_daily_plan(limit=48)
        return jsonify(rows)
    except Exception as e:
        return jsonify([])


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
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={theme:{extend:{colors:{brand:'#0ea5e9',solar:'#f59e0b',bat:'#22c55e',grid:'#818cf8',load:'#f472b6'},fontFamily:{mono:['JetBrains Mono','Fira Mono','monospace']}}}}</script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
body{font-family:'Inter',sans-serif;background:#0b1120;color:#e2e8f0;}
.mono{font-family:'JetBrains Mono',monospace;}
.card{background:#111827;border:1px solid #1e2d45;border-radius:12px;}
.card-glow-solar{box-shadow:0 0 0 1px #f59e0b22,0 4px 24px #f59e0b0a;}
.card-glow-bat{box-shadow:0 0 0 1px #22c55e22,0 4px 24px #22c55e0a;}
.card-glow-grid{box-shadow:0 0 0 1px #818cf822,0 4px 24px #818cf80a;}
.card-glow-brand{box-shadow:0 0 0 1px #0ea5e922,0 4px 24px #0ea5e90a;}
.pill{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:999px;font-size:0.72rem;font-weight:600;letter-spacing:.04em;}
.dot-pulse{width:7px;height:7px;border-radius:50%;background:#22c55e;box-shadow:0 0 6px #22c55e;animation:dp 1.8s ease-in-out infinite;}
@keyframes dp{0%,100%{opacity:1;box-shadow:0 0 6px #22c55e}50%{opacity:.3;box-shadow:0 0 2px #22c55e}}
.flow-dash{stroke-dasharray:6 4;animation:fdash 1.2s linear infinite;}
.flow-dash-rev{stroke-dasharray:6 4;animation:fdash-rev 1.2s linear infinite;}
@keyframes fdash{to{stroke-dashoffset:-20}}
@keyframes fdash-rev{to{stroke-dashoffset:20}}
.bar-anim{transition:height .5s ease;}
</style>
</head>
<body class="min-h-screen">

<!-- TOPBAR -->
<header class="sticky top-0 z-50 bg-[#0b1120]/90 backdrop-blur border-b border-slate-800 px-4 py-3 flex items-center gap-3">
  <span class="text-lg font-bold tracking-widest text-white">&#9889; ABELGÅRD</span>
  <span class="pill bg-slate-800 text-slate-400 border border-slate-600" id="modePill">IDLE</span>
  <span class="pill bg-red-900 text-red-300 border border-red-700 text-xs" id="stormPill" style="display:none">&#127783; STORM</span>
  <div class="ml-auto mono text-xs text-slate-500" id="updLine">—</div>
  <div class="dot-pulse"></div>
</header>

<main class="max-w-5xl mx-auto px-3 py-4 space-y-4">

  <!-- ROW 1: Energy flow + batteri -->
  <section class="grid grid-cols-1 md:grid-cols-3 gap-3">

    <div class="card card-glow-brand p-4 md:col-span-2">
      <div class="text-xs font-semibold text-slate-500 uppercase tracking-widest mb-4">Energiflyt — live</div>

      <!-- Top flow: Grid ←→ Hus ←→ Sol -->
      <div class="flex items-center justify-around">

        <div class="flex flex-col items-center gap-1 w-20">
          <div class="text-3xl">&#128268;</div>
          <div class="text-xs text-slate-500">Grid</div>
          <div class="mono text-sm font-semibold" id="fGrid">— W</div>
          <div class="text-xs text-slate-500" id="fGridSub"></div>
        </div>

        <svg width="64" height="20" class="shrink-0">
          <line id="lineGrid" x1="2" y1="10" x2="62" y2="10" stroke="#818cf8" stroke-width="2.5" stroke-dasharray="6 4"/>
          <polygon id="arrowGrid" points="62,10 52,5 52,15" fill="#818cf8"/>
        </svg>

        <div class="flex flex-col items-center gap-1 w-20">
          <div class="text-3xl">&#127968;</div>
          <div class="text-xs text-slate-500">Forbruk</div>
          <div class="mono text-sm font-semibold text-slate-200" id="fLoad">— W</div>
        </div>

        <svg width="64" height="20" class="shrink-0">
          <line id="lineSolar" x1="62" y1="10" x2="2" y2="10" stroke="#f59e0b" stroke-width="2.5" stroke-dasharray="6 4"/>
          <polygon id="arrowSolar" points="2,10 12,5 12,15" fill="#f59e0b"/>
        </svg>

        <div class="flex flex-col items-center gap-1 w-20">
          <div class="text-3xl">&#9728;</div>
          <div class="text-xs text-slate-500">Sol</div>
          <div class="mono text-sm font-semibold text-solar" id="fSolar">— W</div>
        </div>

      </div>

      <!-- Bottom flow: Batteri -->
      <div class="flex items-center justify-center gap-3 mt-5 pt-4 border-t border-slate-800">
        <div class="text-xs text-slate-400 mono" id="fBatFlow">Batteri: —</div>
        <svg width="80" height="14" class="shrink-0">
          <line id="lineBat" x1="2" y1="7" x2="78" y2="7" stroke="#22c55e" stroke-width="2" stroke-dasharray="6 4"/>
        </svg>
        <div class="text-xs text-slate-500">&#8597; bat</div>
      </div>
    </div>

    <!-- Batteri -->
    <div class="card card-glow-bat p-4 flex flex-col items-center justify-center gap-3">
      <div class="text-xs font-semibold text-slate-500 uppercase tracking-widest">Batteri</div>
      <div class="relative" style="width:56px;height:94px;">
        <div class="absolute top-0 left-1/2 -translate-x-1/2 w-5 h-2.5 rounded-t bg-slate-600"></div>
        <div class="absolute top-2.5 inset-x-0 bottom-0 border-2 border-slate-600 rounded-b rounded-t-sm overflow-hidden bg-slate-900">
          <div class="absolute bottom-0 inset-x-0 bar-anim" id="batFill" style="height:65%;background:linear-gradient(180deg,#15803d,#22c55e);"></div>
          <div class="absolute inset-0 flex items-center justify-center mono text-xs font-bold text-white drop-shadow" id="batPctLabel">—%</div>
        </div>
      </div>
      <div class="text-center">
        <div class="mono text-3xl font-bold text-bat" id="socVal">—%</div>
        <div class="text-xs text-slate-500 mt-1" id="socTarget">lademål: —%</div>
        <div class="mono text-sm mt-2" id="batWval">—</div>
      </div>
    </div>

  </section>

  <!-- ROW 2: Stat cards -->
  <section class="grid grid-cols-2 sm:grid-cols-4 gap-3">
    <div class="card card-glow-solar p-3">
      <div class="text-xs text-slate-500 uppercase tracking-wider mb-1">Spot nå</div>
      <div class="mono text-2xl font-bold text-solar" id="cSpot">—</div>
      <div class="text-xs text-slate-500 mt-1" id="cBuy">kjøp/salg: —</div>
    </div>
    <div class="card card-glow-bat p-3">
      <div class="text-xs text-slate-500 uppercase tracking-wider mb-1">Sol nå</div>
      <div class="mono text-2xl font-bold text-bat" id="cSolarNow">— kW</div>
      <div class="text-xs text-slate-500 mt-1" id="cSolarFc">i morgen: —</div>
    </div>
    <div class="card card-glow-grid p-3">
      <div class="text-xs text-slate-500 uppercase tracking-wider mb-1">Profitt i dag</div>
      <div class="mono text-2xl font-bold text-grid" id="cProfit">—</div>
      <div class="text-xs text-slate-500 mt-1" id="cProfitTotal">total: —</div>
    </div>
    <div class="card card-glow-brand p-3">
      <div class="text-xs text-slate-500 uppercase tracking-wider mb-1">Arbitrasje-margin</div>
      <div class="mono text-2xl font-bold text-brand" id="cMargin">— ø</div>
      <div class="text-xs text-slate-500 mt-1">spread kjøp/salg</div>
    </div>
  </section>

  <!-- ROW 3: Prisbar + Sol-profil -->
  <section class="grid grid-cols-1 md:grid-cols-2 gap-3">
    <div class="card p-4">
      <div class="text-xs font-semibold text-slate-500 uppercase tracking-widest mb-3">Spotpris neste 24t</div>
      <div class="flex items-end gap-px h-20" id="priceBars"></div>
      <div class="flex mt-1" id="priceLabels"></div>
    </div>
    <div class="card p-4">
      <div class="flex items-center justify-between mb-3">
        <div class="text-xs font-semibold text-slate-500 uppercase tracking-widest">Sol-profil i dag</div>
        <div class="mono text-xs text-solar" id="solarTodayKwh">—</div>
      </div>
      <div class="flex items-end gap-px h-20" id="solarBars"></div>
      <div class="flex mt-1" id="solarLabels"></div>
    </div>
  </section>

  <!-- ROW 4: Handelsplan -->
  <div class="card p-4">
    <div class="text-xs font-semibold text-slate-500 uppercase tracking-widest mb-3">Optimizer — plan neste 24t</div>
    <div class="space-y-0.5" id="planRows">
      <div class="text-slate-600 text-xs">Laster plan...</div>
    </div>
  </div>

  <!-- ROW 5: Handler + Sol-analyse -->
  <section class="grid grid-cols-1 md:grid-cols-2 gap-3">

    <div class="card p-4">
      <div class="text-xs font-semibold text-slate-500 uppercase tracking-widest mb-3">Siste handler</div>
      <div id="tradeRows"><div class="text-slate-600 text-xs">Ingen handler ennå</div></div>
    </div>

    <div class="card p-4">
      <div class="text-xs font-semibold text-slate-500 uppercase tracking-widest mb-3">Sol-analyse (daglige sykluser)</div>
      <div class="overflow-x-auto">
        <table class="w-full text-xs mono">
          <thead><tr class="text-slate-600 border-b border-slate-800">
            <th class="text-left pb-2 font-medium">Tid</th>
            <th class="text-right pb-2 font-medium">Sol kWh</th>
            <th class="text-right pb-2 font-medium">Reserve</th>
            <th class="text-right pb-2 font-medium">Lademål</th>
            <th class="text-right pb-2 font-medium">SOC</th>
          </tr></thead>
          <tbody id="planAnalysisBody">
            <tr><td colspan="5" class="text-slate-600 pt-2">Laster...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </section>

  <!-- ROW 6: Systemkonfig -->
  <div class="card p-4">
    <div class="text-xs font-semibold text-slate-500 uppercase tracking-widest mb-3">Systemkonfig</div>
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
      <div class="bg-slate-900 rounded-lg p-3"><div class="text-slate-500 text-xs mb-1">Min SOC</div><div class="mono font-semibold" id="cfgMinSoc">—</div></div>
      <div class="bg-slate-900 rounded-lg p-3"><div class="text-slate-500 text-xs mb-1">Max SOC</div><div class="mono font-semibold" id="cfgMaxSoc">—</div></div>
      <div class="bg-slate-900 rounded-lg p-3"><div class="text-slate-500 text-xs mb-1">Min spread</div><div class="mono font-semibold" id="cfgSpread">—</div></div>
      <div class="bg-slate-900 rounded-lg p-3"><div class="text-slate-500 text-xs mb-1">Kapasitetsavgift</div><div class="mono font-semibold" id="cfgCap">—</div></div>
    </div>
  </div>

</main>

<script>
const pad2 = n => String(n).padStart(2,'0');
const fmtW = w => { if(w==null) return '—'; const a=Math.abs(w); return a>=1000?(w/1000).toFixed(2)+' kW':Math.round(w)+' W'; };

// Mode pill
const MODE_CFG = {
  charge:        {label:'LADER',       cls:'bg-green-900 text-green-300 border-green-700'},
  discharge:     {label:'UTLADER',     cls:'bg-orange-900 text-orange-300 border-orange-700'},
  idle:          {label:'IDLE',        cls:'bg-slate-800 text-slate-400 border-slate-600'},
  'self-consume':{label:'EGENFORBRUK', cls:'bg-sky-900 text-sky-300 border-sky-700'},
  peak_shave:    {label:'PEAK-SHAVE',  cls:'bg-red-900 text-red-300 border-red-700'},
};
function setModePill(action) {
  const el = document.getElementById('modePill');
  const cfg = MODE_CFG[action] || MODE_CFG.idle;
  el.className = 'pill border ' + cfg.cls;
  el.textContent = cfg.label;
}

// Flow line animation — bruker style direkte da SVG class-arv er upålitelig
function setFlow(id, active, reverse) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.opacity = active ? '1' : '0.12';
  if (active) {
    el.style.strokeDasharray = '6 4';
    el.style.animation = reverse ? 'fdash-rev 1.2s linear infinite' : 'fdash 1.2s linear infinite';
  } else {
    el.style.strokeDasharray = '4 4';
    el.style.animation = 'none';
  }
}

// Price bars
function renderPriceBars(prices) {
  if (!prices?.length) return;
  const c = document.getElementById('priceBars');
  const l = document.getElementById('priceLabels');
  const mx = Math.max(...prices.slice(0,24).map(p=>p.buy_ore));
  const nowH = new Date().getHours();
  c.innerHTML = prices.slice(0,24).map(p => {
    const h = parseInt(p.time.slice(-5,-3));
    const pct = Math.max(5, Math.round(p.buy_ore/mx*100));
    const isCur = h === nowH;
    const col = isCur ? '#0ea5e9' : p.buy_ore > mx*0.75 ? '#ef4444' : p.buy_ore < mx*0.4 ? '#22c55e' : '#818cf8';
    return '<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%">'
      +'<div class="bar-anim w-full rounded-t" style="height:'+pct+'%;background:'+col+(isCur?';box-shadow:0 0 8px '+col:'44')+';border-top:2px solid '+col+'" title="'+Math.round(p.buy_ore)+'ø"></div>'
      +'</div>';
  }).join('');
  l.innerHTML = prices.slice(0,24).map((p,i)=>'<span style="flex:1;text-align:center;font-size:10px;color:#475569;font-family:monospace">'+(i%4===0?p.time.slice(-5,-3):'')+'</span>').join('');
}

// Solar bars
function renderSolarBars(profile) {
  if (!profile?.length) return;
  const c = document.getElementById('solarBars');
  const l = document.getElementById('solarLabels');
  const mx = Math.max(...profile.map(p=>p.kw), 0.1);
  const nowH = new Date().getHours();
  c.innerHTML = profile.map(p => {
    const pct = Math.max(4, Math.round(p.kw/mx*100));
    const isCur = p.hour === nowH;
    const col = '#f59e0b';
    return '<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%">'
      +'<div class="bar-anim w-full rounded-t" style="height:'+pct+'%;background:'+col+(isCur?';box-shadow:0 0 8px '+col:'44')+';border-top:2px solid '+col+'" title="'+p.kw+' kW"></div>'
      +'</div>';
  }).join('');
  l.innerHTML = profile.map((p,i)=>'<span style="flex:1;text-align:center;font-size:10px;color:#475569;font-family:monospace">'+(i%3===0?pad2(p.hour):'')+'</span>').join('');
}

// Plan rows
const PLAN_STYLE = {
  charge:    'border-l-2 border-green-600 bg-green-950/30',
  discharge: 'border-l-2 border-orange-600 bg-orange-950/30',
  idle:      'border-l-2 border-slate-700 bg-slate-900/20',
  peak_shave:'border-l-2 border-red-600 bg-red-950/30',
};
const PLAN_LABEL = {charge:'LADER', discharge:'UTLADER', idle:'IDLE', peak_shave:'PEAK'};
const PLAN_COLOR = {charge:'#22c55e', discharge:'#f97316', idle:'#475569', peak_shave:'#ef4444'};

function renderPlan(plan) {
  const el = document.getElementById('planRows');
  if (!plan?.length) { el.innerHTML = '<div class="text-slate-600 text-xs">Ingen plan</div>'; return; }
  el.innerHTML = plan.slice(0,24).map(p => {
    const st = PLAN_STYLE[p.action] || PLAN_STYLE.idle;
    const lbl = PLAN_LABEL[p.action] || p.action.toUpperCase();
    const col = PLAN_COLOR[p.action] || '#475569';
    const profit = p.profit_nok ? (p.profit_nok>0?'+':'')+p.profit_nok.toFixed(2) : '';
    const pcol = p.profit_nok > 0 ? '#22c55e' : '#ef4444';
    return '<div class="flex items-center gap-3 px-3 py-1.5 rounded '+st+'">'
      +'<span class="mono text-slate-500 text-xs w-12 shrink-0">'+p.time.slice(-5)+'</span>'
      +'<span class="mono text-xs font-semibold w-20 shrink-0" style="color:'+col+'">'+lbl+'</span>'
      +'<span class="text-slate-500 text-xs flex-1 truncate">'+( p.reason||'')+'</span>'
      +'<span class="mono text-xs text-slate-600 shrink-0">'+(p.power_kw?p.power_kw.toFixed(1)+'kW':'')+'</span>'
      +(profit?'<span class="mono text-xs shrink-0" style="color:'+pcol+'">'+profit+'kr</span>':'')
      +'</div>';
  }).join('');
}

// Trade rows
function renderTrades(trades) {
  const el = document.getElementById('tradeRows');
  if (!trades?.length) { el.innerHTML = '<div class="text-slate-600 text-xs">Ingen handler ennå</div>'; return; }
  el.innerHTML = trades.slice(0,10).map(t => {
    const isBuy = t.trade_type === 'buy';
    const col   = isBuy ? '#60a5fa' : '#22c55e';
    const prof  = (t.net_profit_nok||0);
    const pcol  = prof >= 0 ? '#22c55e' : '#ef4444';
    const ts    = t.timestamp ? t.timestamp.slice(5,16).replace('T',' ') : '--';
    return '<div class="flex items-center gap-2 py-1.5 border-b border-slate-800/50 text-xs">'
      +'<div class="w-1.5 h-1.5 rounded-full shrink-0" style="background:'+col+'"></div>'
      +'<span class="mono text-slate-500 w-24 shrink-0">'+ts+'</span>'
      +'<span class="font-semibold w-12 shrink-0" style="color:'+col+'">'+(isBuy?'Kjøp':'Salg')+'</span>'
      +'<span class="mono text-slate-500 flex-1">'+((t.energy_kwh||0).toFixed(2))+' kWh @ '+Math.round((t.price_nok_kwh||0)*100)+'ø</span>'
      +'<span class="mono font-semibold" style="color:'+pcol+'">'+(prof>=0?'+':'')+prof.toFixed(2)+' kr</span>'
      +'</div>';
  }).join('');
}

// Sol-analyse tabell
function renderPlanAnalysis(rows) {
  const tbody = document.getElementById('planAnalysisBody');
  if (!rows?.length) { tbody.innerHTML = '<tr><td colspan="5" class="text-slate-600 pt-2">Ingen data ennå</td></tr>'; return; }
  tbody.innerHTML = rows.slice(0,12).map(r => {
    const ts = r.timestamp ? r.timestamp.slice(5,16).replace('T',' ') : '--';
    return '<tr class="border-b border-slate-800/40">'
      +'<td class="py-1.5 text-slate-400">'+ts+(r.storm_mode?' &#127783;':'')+'</td>'
      +'<td class="py-1.5 text-right text-solar">'+((r.solar_kwh_forecast||0).toFixed(1))+'</td>'
      +'<td class="py-1.5 text-right text-slate-400">'+((r.solar_reserve_pct||0).toFixed(1))+'%</td>'
      +'<td class="py-1.5 text-right text-brand">'+((r.charge_target_soc||0).toFixed(1))+'%</td>'
      +'<td class="py-1.5 text-right text-bat">'+(r.soc_at_cycle!=null?(r.soc_at_cycle).toFixed(1)+'%':'—')+'</td>'
      +'</tr>';
  }).join('');
}

// Main fetch
async function fetchAll() {
  const safe = async (url, def) => { try { const r=await fetch(url); return r.ok?r.json():def; } catch{ return def; } };
  const [live, status, activity, prices, plan, trades, solar, dp] = await Promise.all([
    safe('/api/live',{}), safe('/api/status',{}), safe('/api/activity',{}),
    safe('/api/prices',[]), safe('/api/plan',[]), safe('/api/trades',[]),
    safe('/api/solar',{}), safe('/api/daily_plan',[]),
  ]);

  const soc    = live.soc ?? 0;
  const solarW = live.solar_w ?? 0;
  const gridW  = live.grid_w ?? 0;
  const batW   = live.battery_w ?? 0;
  const loadW  = solarW + gridW - batW;

  // Flow
  document.getElementById('fGrid').textContent  = fmtW(gridW);
  document.getElementById('fGrid').style.color  = gridW>50?'#818cf8':gridW<-50?'#22c55e':'#64748b';
  document.getElementById('fGridSub').textContent = gridW<-50?'↑ eksport':gridW>50?'↓ import':'';
  document.getElementById('fSolar').textContent = fmtW(solarW);
  document.getElementById('fLoad').textContent  = fmtW(Math.max(0,loadW));
  document.getElementById('fBatFlow').textContent = 'Batteri: '+fmtW(batW)+(batW>50?' ↑ lader':batW<-50?' ↓ utlader':'');
  document.getElementById('fBatFlow').style.color = batW<-50?'#f97316':batW>50?'#22c55e':'#94a3b8';

  // Battery
  document.getElementById('socVal').textContent = soc?soc.toFixed(1)+'%':'—%';
  document.getElementById('batPctLabel').textContent = soc?Math.round(soc)+'%':'—';
  document.getElementById('batWval').textContent = fmtW(batW);
  document.getElementById('batWval').style.color = batW<-50?'#f97316':batW>50?'#22c55e':'#64748b';
  const fill = document.getElementById('batFill');
  fill.style.height = Math.max(2,Math.min(100,soc))+'%';
  fill.style.background = soc>60?'linear-gradient(180deg,#15803d,#22c55e)':soc>35?'linear-gradient(180deg,#92400e,#f59e0b)':'linear-gradient(180deg,#7f1d1d,#ef4444)';

  if(dp?.length){
    document.getElementById('socTarget').textContent='lademål: '+dp[0].charge_target_soc.toFixed(1)+'%';
    document.getElementById('stormPill').style.display=dp[0].storm_mode?'':'none';
  }

  // Flow arrows
  setFlow('lineGrid',  Math.abs(gridW)>20, gridW<0);
  setFlow('lineSolar', solarW>20, false);
  setFlow('lineBat',   Math.abs(batW)>20, batW>0);
  // arrowhead Grid direction
  const ag = document.getElementById('arrowGrid');
  if(ag) ag.setAttribute('points', gridW<-50?'2,10 12,5 12,15':'62,10 52,5 52,15');

  // Stat cards
  if(status.price){
    document.getElementById('cSpot').textContent = Math.round(status.price.spot_ore)+' ø';
    document.getElementById('cBuy').textContent  = 'kjøp '+Math.round(status.price.buy_ore)+' / salg '+Math.round(status.price.sell_ore)+' ø';
    const m = status.price.margin_ore;
    document.getElementById('cMargin').textContent = Math.round(m)+' ø';
    document.getElementById('cMargin').style.color = m>110?'#22c55e':m>50?'#f59e0b':'#ef4444';
  }
  if(status.profit){
    const p = status.profit.today_nok;
    document.getElementById('cProfit').textContent = (p>=0?'+':'')+p.toFixed(2)+' kr';
    document.getElementById('cProfit').style.color = p>=0?'#818cf8':'#ef4444';
    document.getElementById('cProfitTotal').textContent = 'total: '+status.profit.total_nok.toFixed(2)+' kr';
  }
  document.getElementById('cSolarNow').textContent = (solarW/1000).toFixed(2)+' kW';
  if(solar.kwh_tomorrow!=null) document.getElementById('cSolarFc').textContent='i morgen: '+solar.kwh_tomorrow+' kWh';

  // Mode
  const action = activity?.current_action?.action || 'idle';
  setModePill(action);

  // Config
  if(status){
    document.getElementById('cfgMinSoc').textContent  = (status.min_soc||20)+'%';
    document.getElementById('cfgMaxSoc').textContent  = (status.max_soc||90)+'%';
    document.getElementById('cfgSpread').textContent  = (status.min_spread_ore||110)+' ø';
    document.getElementById('cfgCap').textContent     = (status.capacity_charge_nok||419)+' kr';
  }

  // Timestamp
  const upd = live.updated?new Date(live.updated):new Date();
  document.getElementById('updLine').textContent = pad2(upd.getHours())+':'+pad2(upd.getMinutes())+':'+pad2(upd.getSeconds());

  renderPriceBars(prices);
  if(solar.today_profile){
    renderSolarBars(solar.today_profile);
    // Beregn kun faktisk produsert hittil (timer <= nå)
    const nowH = new Date().getHours();
    const producedSoFar = solar.today_profile.filter(p=>p.hour<=nowH).reduce((s,p)=>s+p.kw,0);
    document.getElementById('solarTodayKwh').textContent=producedSoFar.toFixed(1)+' kWh hittil / '+solar.kwh_today+' kWh prognose';
  }
  renderPlan(plan);
  renderTrades(trades);
  renderPlanAnalysis(dp);
}

fetchAll();
setInterval(fetchAll, 10000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
