"""Web dashboard for Victron Energy Trader."""
import os
import json
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string

from profit_tracker import ProfitTracker
from price_fetcher import PriceFetcher
from tariff import buy_price_ore, sell_price_ore, CAPACITY_CHARGE_NOK
from config import CONFIG

app = Flask(__name__)
tracker = ProfitTracker()
fetcher = PriceFetcher()

# Cache priser for å unngå for mange API-kall
_price_cache = {"data": [], "fetched": None}
_price_lock = threading.Lock()

def get_prices_cached():
    with _price_lock:
        now = datetime.now(timezone.utc)
        if not _price_cache["fetched"] or (now - _price_cache["fetched"]).seconds > 1800:
            try:
                _price_cache["data"] = fetcher.get_prices(24)
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
    buy_ore = buy_price_ore(spot_ore, datetime.now().hour) if current else 0
    sell_ore = sell_price_ore()

    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "price": {
            "spot_ore": round(spot_ore, 1),
            "buy_ore": round(buy_ore, 1),
            "sell_ore": round(sell_ore, 2),
            "margin_ore": round(sell_ore - buy_ore, 1),
        },
        "profit": {
            "today_nok": round(stats.get("today_profit_nok", 0), 2),
            "total_nok": round(stats.get("total_profit_nok", 0), 2),
            "today_bought_kwh": round(stats.get("today_bought_kwh", 0), 1),
            "today_sold_kwh": round(stats.get("today_sold_kwh", 0), 1),
        },
        "capacity_charge_nok": CAPACITY_CHARGE_NOK,
        "solar_max_kw": CONFIG.solar_max_kw,
    })


@app.route("/api/prices")
def api_prices():
    prices = get_prices_cached()
    return jsonify([{
        "time": p.timestamp.strftime("%H:%M"),
        "spot_ore": round(p.price_ore_kwh / CONFIG.vat, 1),
        "buy_ore": round(buy_price_ore(p.price_ore_kwh / CONFIG.vat, p.timestamp.hour), 1),
        "sell_ore": round(sell_price_ore(), 2),
    } for p in prices])


@app.route("/api/trades")
def api_trades():
    trades = tracker.get_recent_trades(20)
    return jsonify(trades)


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Abelgård Energihandel</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }

  header {
    background: linear-gradient(135deg, #1e3a5f, #0f2027);
    padding: 1.2rem 2rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    border-bottom: 1px solid #1e40af44;
  }
  header h1 { font-size: 1.4rem; font-weight: 700; color: #60a5fa; }
  header span { font-size: 0.85rem; color: #94a3b8; }
  .live-dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e;
    animation: pulse 1.5s infinite; margin-left: auto; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }

  main { padding: 1.5rem 2rem; max-width: 1400px; margin: 0 auto; }

  .grid { display: grid; gap: 1rem; }
  .grid-4 { grid-template-columns: repeat(4, 1fr); }
  .grid-2 { grid-template-columns: repeat(2, 1fr); }
  @media(max-width:900px) { .grid-4,.grid-2 { grid-template-columns: 1fr 1fr; } }
  @media(max-width:500px) { .grid-4,.grid-2 { grid-template-columns: 1fr; } }

  .card {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
  }
  .card-title { font-size: 0.75rem; color: #64748b; text-transform: uppercase; letter-spacing: .05em; margin-bottom: .5rem; }
  .card-value { font-size: 2rem; font-weight: 700; }
  .card-sub { font-size: 0.8rem; color: #64748b; margin-top: .3rem; }

  .green { color: #22c55e; }
  .red { color: #ef4444; }
  .blue { color: #60a5fa; }
  .yellow { color: #facc15; }
  .orange { color: #fb923c; }

  .badge {
    display: inline-block; padding: .2rem .7rem; border-radius: 9999px;
    font-size: 0.75rem; font-weight: 600;
  }
  .badge-green { background: #14532d; color: #22c55e; }
  .badge-red { background: #450a0a; color: #ef4444; }
  .badge-blue { background: #1e3a5f; color: #60a5fa; }
  .badge-yellow { background: #422006; color: #facc15; }

  .chart-card { padding: 1.5rem; }
  .chart-card h2 { font-size: 0.9rem; color: #94a3b8; margin-bottom: 1rem; }
  canvas { max-height: 220px; }

  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; padding: .5rem .8rem; color: #64748b; font-weight: 500; border-bottom: 1px solid #334155; }
  td { padding: .5rem .8rem; border-bottom: 1px solid #1e293b; }
  tr:hover td { background: #263348; }

  #lastUpdate { font-size: 0.75rem; color: #475569; text-align: right; margin-top: .5rem; }

  .status-bar {
    background: #1e293b; border: 1px solid #334155; border-radius: 8px;
    padding: .6rem 1rem; margin-bottom: 1rem;
    display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
    font-size: 0.82rem; color: #94a3b8;
  }
  .status-bar strong { color: #e2e8f0; }
</style>
</head>
<body>
<header>
  <div>⚡</div>
  <div>
    <h1>Abelgård Energihandel</h1>
    <span>Kraftriket Solstrøm · Elvia NO1 · 48 kWh Farco</span>
  </div>
  <div class="live-dot" title="Live oppdatering hvert 10s"></div>
</header>

<main>
  <div class="status-bar" id="statusBar">Laster data...</div>

  <!-- KPI-kort -->
  <div class="grid grid-4" style="margin-bottom:1rem">
    <div class="card">
      <div class="card-title">Spot akkurat nå</div>
      <div class="card-value blue" id="spotOre">—</div>
      <div class="card-sub">øre/kWh eks mva</div>
    </div>
    <div class="card">
      <div class="card-title">Reell kjøpspris</div>
      <div class="card-value" id="buyOre">—</div>
      <div class="card-sub">øre/kWh inkl alt + Norgespris</div>
    </div>
    <div class="card">
      <div class="card-title">Salgspris (plusskunde)</div>
      <div class="card-value green" id="sellOre">—</div>
      <div class="card-sub">øre/kWh (Kraftriket netto)</div>
    </div>
    <div class="card">
      <div class="card-title">Margin</div>
      <div class="card-value" id="marginOre">—</div>
      <div class="card-sub" id="marginStatus">—</div>
    </div>
  </div>

  <!-- Profitt-kort -->
  <div class="grid grid-4" style="margin-bottom:1rem">
    <div class="card">
      <div class="card-title">Dagens profitt</div>
      <div class="card-value green" id="todayProfit">—</div>
      <div class="card-sub">kr hittil i dag</div>
    </div>
    <div class="card">
      <div class="card-title">Total profitt</div>
      <div class="card-value green" id="totalProfit">—</div>
      <div class="card-sub">kr siden oppstart</div>
    </div>
    <div class="card">
      <div class="card-title">Kjøpt i dag</div>
      <div class="card-value blue" id="todayBought">—</div>
      <div class="card-sub">kWh lastet inn</div>
    </div>
    <div class="card">
      <div class="card-title">Solgt i dag</div>
      <div class="card-value orange" id="todaySold">—</div>
      <div class="card-sub">kWh sendt ut</div>
    </div>
  </div>

  <!-- Graf + trades -->
  <div class="grid grid-2">
    <div class="card chart-card">
      <h2>Priser neste 24 timer</h2>
      <canvas id="priceChart"></canvas>
    </div>
    <div class="card">
      <h2 style="font-size:.9rem;color:#94a3b8;margin-bottom:.8rem">Siste handler</h2>
      <table>
        <thead><tr><th>Tid</th><th>Type</th><th>kWh</th><th>Pris</th></tr></thead>
        <tbody id="tradesTable"><tr><td colspan="4" style="color:#475569">Ingen handler ennå</td></tr></tbody>
      </table>
    </div>
  </div>

  <div id="lastUpdate"></div>
</main>

<script>
let priceChart = null;

async function fetchStatus() {
  const res = await fetch('/api/status');
  const d = await res.json();

  document.getElementById('spotOre').textContent = d.price.spot_ore + ' øre';
  document.getElementById('buyOre').textContent = d.price.buy_ore + ' øre';

  const sellEl = document.getElementById('sellOre');
  sellEl.textContent = d.price.sell_ore + ' øre';

  const margin = d.price.margin_ore;
  const marginEl = document.getElementById('marginOre');
  const marginStatus = document.getElementById('marginStatus');
  marginEl.textContent = (margin >= 0 ? '+' : '') + margin + ' øre';
  marginEl.className = 'card-value ' + (margin >= 0 ? 'green' : 'red');

  if (margin >= 0) {
    marginStatus.innerHTML = '<span class="badge badge-green">⚡ Lønnsomt å utlade</span>';
  } else {
    marginStatus.innerHTML = '<span class="badge badge-blue">🔋 Lønnsomt å lade</span>';
  }

  document.getElementById('todayProfit').textContent = d.profit.today_nok.toFixed(2) + ' kr';
  document.getElementById('totalProfit').textContent = d.profit.total_nok.toFixed(2) + ' kr';
  document.getElementById('todayBought').textContent = d.profit.today_bought_kwh + ' kWh';
  document.getElementById('todaySold').textContent = d.profit.today_sold_kwh + ' kWh';

  document.getElementById('statusBar').innerHTML =
    `<strong>Status:</strong> Live &nbsp;|&nbsp;
     <strong>Spot:</strong> ${d.price.spot_ore} øre &nbsp;|&nbsp;
     <strong>Kjøp:</strong> ${d.price.buy_ore} øre &nbsp;|&nbsp;
     <strong>Salg:</strong> ${d.price.sell_ore} øre &nbsp;|&nbsp;
     <strong>Kapasitetsledd:</strong> ${d.capacity_charge_nok} kr/mnd`;

  document.getElementById('lastUpdate').textContent =
    'Oppdatert: ' + new Date().toLocaleTimeString('no-NO');
}

async function fetchPrices() {
  const res = await fetch('/api/prices');
  const prices = await res.json();

  const labels = prices.map(p => p.time);
  const buyData = prices.map(p => p.buy_ore);
  const sellData = prices.map(p => p.sell_ore);

  if (priceChart) {
    priceChart.data.labels = labels;
    priceChart.data.datasets[0].data = buyData;
    priceChart.data.datasets[1].data = sellData;
    priceChart.update('none');
    return;
  }

  const ctx = document.getElementById('priceChart').getContext('2d');
  priceChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Kjøpspris (reell)',
          data: buyData,
          borderColor: '#60a5fa',
          backgroundColor: '#60a5fa22',
          fill: true,
          tension: 0.3,
          pointRadius: 2,
        },
        {
          label: 'Salgspris (68.75 øre)',
          data: sellData,
          borderColor: '#22c55e',
          borderDash: [5, 5],
          borderWidth: 2,
          pointRadius: 0,
          fill: false,
        }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#64748b', maxTicksLimit: 8 }, grid: { color: '#1e293b' } },
        y: {
          ticks: { color: '#64748b', callback: v => v + 'ø' },
          grid: { color: '#1e293b' }
        }
      }
    }
  });
}

async function fetchTrades() {
  const res = await fetch('/api/trades');
  const trades = await res.json();
  const tbody = document.getElementById('tradesTable');
  if (!trades.length) return;

  tbody.innerHTML = trades.map(t => {
    const typeClass = t.trade_type === 'sell' ? 'orange' : t.trade_type === 'peak_shave' ? 'yellow' : 'blue';
    const typeLabel = t.trade_type === 'sell' ? '⚡ Solgt' : t.trade_type === 'peak_shave' ? '🔒 Peak' : '🔋 Kjøpt';
    const price = t.price_nok_kwh > 0 ? (t.price_nok_kwh * 100).toFixed(0) + 'ø' : '—';
    return `<tr>
      <td style="color:#64748b">${t.timestamp ? t.timestamp.substring(11,16) : '—'}</td>
      <td><span class="${typeClass}">${typeLabel}</span></td>
      <td>${t.energy_kwh?.toFixed(1) ?? '—'}</td>
      <td>${price}</td>
    </tr>`;
  }).join('');
}

async function refresh() {
  await Promise.all([fetchStatus(), fetchPrices(), fetchTrades()]);
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
