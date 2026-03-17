import os, json, time, webbrowser, threading
from datetime import datetime, timedelta, timezone
from flask import Flask, request, Response, jsonify
import yfinance as yf
import pandas as pd

app = Flask(__name__)
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────────────────────────
def run_backtest(df, signal_hour, signal_minute, risk_pct, initial_equity):
    """Generator: yields dict per trade event."""
    equity      = initial_equity
    state       = 'WAITING'
    high_break  = low_break = candle_range = current_lot = initial_qty = 0.0

    for current_time, row in df.iterrows():
        c_high = float(row['High'].iloc[0] if isinstance(row['High'], pd.Series) else row['High'])
        c_low  = float(row['Low'].iloc[0]  if isinstance(row['Low'],  pd.Series) else row['Low'])

        if current_time.hour == signal_hour and current_time.minute == signal_minute:
            high_break   = c_high
            low_break    = c_low
            candle_range = high_break - low_break
            if candle_range > 0:
                risk_amount = equity * (risk_pct / 100.0)
                initial_qty = risk_amount / candle_range
                state       = 'TRAP_SET'

        if state == 'TRAP_SET':
            if c_high > high_break:
                state = 'LONG'; current_lot = initial_qty
                yield {'waktu': str(current_time), 'aksi': 'Entry Buy Stop',
                       'harga': round(high_break, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
            elif c_low < low_break:
                state = 'SHORT'; current_lot = initial_qty
                yield {'waktu': str(current_time), 'aksi': 'Entry Sell Stop',
                       'harga': round(low_break, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}

        elif state == 'LONG':
            tp = high_break + candle_range; sl = low_break
            if c_high >= tp:
                equity += (tp - high_break) * current_lot; state = 'WAITING'
                yield {'waktu': str(current_time), 'aksi': 'TP Long',
                       'harga': round(tp, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
            elif c_low <= sl:
                equity -= (high_break - sl) * current_lot
                if equity <= 0:
                    equity = 0
                    yield {'waktu': str(current_time), 'aksi': 'MARGIN CALL',
                           'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': 0.0,
                           'margin_call': True}
                    return
                state = 'SHORT'; current_lot *= 2
                yield {'waktu': str(current_time), 'aksi': 'SL Long -> Reversal Short',
                       'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}

        elif state == 'SHORT':
            tp = low_break - candle_range; sl = high_break
            if c_low <= tp:
                equity += (low_break - tp) * current_lot; state = 'WAITING'
                yield {'waktu': str(current_time), 'aksi': 'TP Short',
                       'harga': round(tp, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
            elif c_high >= sl:
                equity -= (sl - low_break) * current_lot
                if equity <= 0:
                    equity = 0
                    yield {'waktu': str(current_time), 'aksi': 'MARGIN CALL',
                           'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': 0.0,
                           'margin_call': True}
                    return
                state = 'LONG'; current_lot *= 2
                yield {'waktu': str(current_time), 'aksi': 'SL Short -> Reversal Long',
                       'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}

def calculate_backtest_summary(df, hour, minute, risk_pct, initial_eq):
    """Fast version of backtest for analysis ranking."""
    equity = initial_eq
    final_equity = initial_eq
    trades = 0
    gen = run_backtest(df, hour, minute, risk_pct, initial_eq)
    for event in gen:
        trades += 1
        final_equity = event['equity']
        if event.get('margin_call'): break
    return {'final_equity': final_equity, 'trades': trades, 'pnl': final_equity - initial_eq}

# ─────────────────────────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────────────────────────
@app.route('/api/download', methods=['POST'])
def api_download():
    d           = request.get_json()
    symbol      = d.get('symbol',     'ETH-USD')
    interval    = d.get('interval',   '15m')
    start       = d.get('start',      '')
    end         = d.get('end',        '')
    cache_only  = d.get('cache_only', False)

    safe_sym = symbol.replace('/', '-')
    csv_path = os.path.join(CACHE_DIR, f"{safe_sym}_{interval}.csv")

    if cache_only:
        if not os.path.exists(csv_path):
            return jsonify(status='error', message='Cache tidak ditemukan.'), 404
        df = pd.read_csv(csv_path, skiprows=[1, 2], index_col=0)
        df.index = pd.to_datetime(df.index).tz_convert('Asia/Jakarta')
        return jsonify(status='cached', rows=len(df), frm=str(df.index[0]), to=str(df.index[-1]))

    try:
        if os.path.exists(csv_path): os.remove(csv_path); print(f"Cache dihapus: {csv_path}")
        kwargs = dict(interval=interval)
        if start: kwargs['start'] = start
        if end:   kwargs['end']   = end
        if not start: kwargs['start'] = (datetime.now(timezone.utc) - timedelta(days=59)).strftime('%Y-%m-%d')

        df = yf.download(symbol, **kwargs, progress=False)
        if df.empty: return jsonify(status='error', message='Data kosong / pair tidak ditemukan'), 400
        
        df.index = df.index.tz_convert('Asia/Jakarta')
        df.to_csv(csv_path)
        return jsonify(status='ok', rows=len(df), frm=str(df.index[0]), to=str(df.index[-1]))
    except Exception as e:
        return jsonify(status='error', message=str(e)), 500


@app.route('/api/backtest')
def api_backtest():
    symbol        = request.args.get('symbol',        'ETH-USD')
    interval      = request.args.get('interval',      '15m')
    signal_hour   = int(request.args.get('signal_hour',   '21'))
    signal_minute = int(request.args.get('signal_minute', '30'))
    risk_pct      = float(request.args.get('risk_pct',    '10'))
    initial_eq    = float(request.args.get('initial_eq',  '10000'))
    delay_ms      = int(request.args.get('delay_ms',      '200'))

    safe_sym = symbol.replace('/', '-')
    csv_path = os.path.join(CACHE_DIR, f"{safe_sym}_{interval}.csv")
    if not os.path.exists(csv_path):
        def err(): yield f"data: {json.dumps({'type':'error','message':'Data belum didownload'})}\n\n"
        return Response(err(), mimetype='text/event-stream')

    df = pd.read_csv(csv_path, skiprows=[1, 2], index_col=0)
    df.index = pd.to_datetime(df.index).tz_convert('Asia/Jakarta')
    for col in ['Open','High','Low','Close']:
        if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['High','Low'])

    def generate():
        trades = 0
        for event in run_backtest(df, signal_hour, signal_minute, risk_pct, initial_eq):
            trades += 1
            yield f"data: {json.dumps({'type':'trade',**event})}\n\n"
            if delay_ms > 0: time.sleep(delay_ms / 1000.0)
        yield f"data: {json.dumps({'type':'done','total_trades':trades})}\n\n"

    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/analyze')
def api_analyze():
    symbol     = request.args.get('symbol',   'ETH-USD')
    interval   = request.args.get('interval', '15m')
    risk_pct   = float(request.args.get('risk_pct',   '10'))
    initial_eq = float(request.args.get('initial_eq', '10000'))

    safe_sym = symbol.replace('/', '-')
    csv_path = os.path.join(CACHE_DIR, f"{safe_sym}_{interval}.csv")
    if not os.path.exists(csv_path):
        def err(): yield f"data: {json.dumps({'status':'error','message':'Data JSON tidak ditemukan'})}\n\n"
        return Response(err(), mimetype='text/event-stream')

    df = pd.read_csv(csv_path, skiprows=[1, 2], index_col=0)
    df.index = pd.to_datetime(df.index).tz_convert('Asia/Jakarta')
    for col in ['High','Low']:
        if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['High','Low'])

    # Cari semua kombinasi HH:MM yang unik di dataset
    unique_slots = sorted(list(set([(t.hour, t.minute) for t in df.index])))

    def generate():
        results = []
        total = len(unique_slots)
        for i, (h, m) in enumerate(unique_slots):
            summary = calculate_backtest_summary(df, h, m, risk_pct, initial_eq)
            results.append({
                'hour': h, 'minute': m, 'time': f"{h:02}:{m:02}",
                'final_equity': round(summary['final_equity'], 2),
                'trades': summary['trades'],
                'pnl': round(summary['pnl'], 2)
            })
            if i % 5 == 0 or i == total - 1:
                yield f"data: {json.dumps({'type':'progress', 'pct': round((i+1)/total * 100), 'time': f'{h:02}:{m:02}'})}\n\n"

        # Sort based on P&L descending
        results.sort(key=lambda x: x['pnl'], reverse=True)
        yield f"data: {json.dumps({'type':'result', 'data': results})}\n\n"

    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

# ─────────────────────────────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0f17;--panel:#151824;--border:#1f2538;--accent:#6366f1;
  --accent2:#38bdf8;--green:#4ade80;--red:#f87171;--muted:#475569;--text:#e2e8f0;
}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column}
header{padding:1rem 1.5rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:1rem;background:var(--panel)}
header h1{font-size:1.1rem;font-weight:700;background:linear-gradient(90deg,#818cf8,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.badge{font-size:.65rem;font-weight:600;padding:.25rem .55rem;border-radius:99px;background:#1e2538;color:#64748b;text-transform:uppercase;letter-spacing:.06em}
.layout{display:flex;flex:1;overflow:hidden;height:calc(100vh - 57px)}
aside{width:280px;min-width:280px;border-right:1px solid var(--border);background:var(--panel);display:flex;flex-direction:column;overflow-y:auto}
.sidebar-section{padding:1.1rem 1.25rem;border-bottom:1px solid var(--border)}
.sidebar-section h2{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:.85rem}
.field{margin-bottom:.75rem}
label{display:block;font-size:.72rem;font-weight:500;color:#94a3b8;margin-bottom:.3rem}
input,select{width:100%;background:#0d0f17;border:1px solid var(--border);color:var(--text);border-radius:7px;padding:.45rem .65rem;font-size:.8rem;font-family:inherit;outline:none;transition:border .15s}
input:focus,select:focus{border-color:var(--accent)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
.btn{width:100%;padding:.6rem;border-radius:8px;font-size:.8rem;font-weight:600;border:none;cursor:pointer;transition:all .18s;font-family:inherit;display:flex;align-items:center;justify-content:center;gap:.4rem}
.btn-secondary{background:#1e2538;color:#94a3b8;margin-bottom:.5rem}
.btn-secondary:hover{background:#252d45;color:var(--text)}
.btn-primary{background:linear-gradient(135deg,#4f52d3,#6366f1);color:#fff}
.btn-primary:hover{opacity:.9;transform:translateY(-1px)}
.btn-primary:disabled{opacity:.4;cursor:not-allowed;transform:none}
.btn-danger{background:#7f1d1d;color:#fca5a5}
.btn-danger:hover{background:#991b1b}
.btn-accent{background:rgba(99,102,241,0.15);color:var(--accent2);border:1px solid rgba(99,102,241,0.3)}
.btn-accent:hover{background:rgba(99,102,241,0.25)}
.speed-wrap{display:flex;align-items:center;gap:.6rem}
input[type=range]{flex:1;accent-color:var(--accent)}
.speed-val{font-size:.78rem;font-weight:600;color:var(--accent);min-width:28px;text-align:right}
.data-info{background:#0d0f17;border:1px solid var(--border);border-radius:8px;padding:.6rem .8rem;font-size:.72rem;color:var(--muted);margin-top:.5rem;line-height:1.6}
.data-info span{color:var(--text);font-weight:500}
main{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:0;border-bottom:1px solid var(--border)}
.stat{padding:.9rem 1.1rem;border-right:1px solid var(--border)}
.stat:last-child{border-right:none}
.stat-label{font-size:.62rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:.3rem}
.stat-value{font-size:1.15rem;font-weight:700}
.stat-value.green{color:var(--green)}
.stat-value.red{color:var(--red)}
.chart-area{padding:1rem 1.25rem;flex:1;display:flex;flex-direction:column;min-height:0}
.chart-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:.75rem}
.chart-title{font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
#statusBadge{font-size:.65rem;padding:.2rem .55rem;border-radius:99px;background:#1e2538;color:#64748b;font-weight:600}
#statusBadge.running{background:#1e3a1e;color:var(--green);animation:pulse 1.2s infinite}
#statusBadge.done{background:#1a2e1a;color:var(--green)}
#statusBadge.margin-call{background:#3a1e1e;color:var(--red)}
#statusBadge.error{background:#3a1e1e;color:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.chart-wrap{flex:1;min-height:0;position:relative}
canvas{display:block}
.log-area{height:210px;border-top:1px solid var(--border);display:flex;flex-direction:column}
.log-header{padding:.55rem 1.25rem;border-bottom:1px solid var(--border);font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);display:flex;align-items:center;justify-content:space-between}
.log-scroll{flex:1;overflow-y:auto;font-size:.73rem;font-family:'Courier New',monospace}
.log-scroll::-webkit-scrollbar{width:4px}
.log-scroll::-webkit-scrollbar-thumb{background:#1f2538;border-radius:4px}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:0;background:#0d0f17;padding:.4rem .85rem;text-align:left;font-size:.62rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);border-bottom:1px solid var(--border)}
tbody tr{border-bottom:1px solid #12151f;transition:background .1s}
tbody tr:hover{background:#151824}
tbody td{padding:.35rem .85rem;color:#cbd5e1}
.aksi-tp{color:var(--green);font-weight:600}
.aksi-sl{color:var(--red);font-weight:600}
.aksi-mc{color:#ef4444;font-weight:800;background:rgba(239,68,68,0.1)}
.aksi-entry{color:#93c5fd}
/* Analysis Overlay */
#analysisOverlay{position:absolute;inset:0;background:var(--bg);z-index:50;display:none;flex-direction:column;padding:2rem}
.analysis-card{background:var(--panel);border:1px solid var(--border);border-radius:12px;width:100%;max-width:800px;margin:0 auto;padding:1.5rem;display:flex;flex-direction:column;flex:1;overflow:hidden}
.analysis-title{font-size:1.2rem;font-weight:700;margin-bottom:1rem;display:flex;align-items:center;gap:1rem}
.ana-prog-wrap{background:#0d0f17;border-radius:10px;height:12px;overflow:hidden;margin-bottom:1rem;border:1px solid var(--border)}
.ana-prog-fill{height:100%;background:linear-gradient(90deg,#6366f1,#38bdf8);transition:width .2s}
.ana-status-row{display:flex;justify-content:space-between;font-size:.8rem;color:var(--muted);margin-bottom:1.5rem}
.ana-scroll{flex:1;overflow-y:auto;border:1px solid var(--border);border-radius:8px;background:#0d0f17}
.ana-table tr{transition:background .05s}
.ana-table td{padding:.6rem 1rem;font-size:.85rem}
.ana-table b{color:var(--green)}
.ana-table .btn-use{padding:.2rem .5rem;font-size:.7rem;background:var(--accent);color:#fff;border-radius:4px;border:none;cursor:pointer}
.ana-table .btn-use:hover{opacity:.8}
</style>
</head>
<body>
<header><h1>⚡ Backtest Dashboard</h1><span class="badge">Martiangle Strategy</span></header>
<div class="layout">
<aside>
  <div class="sidebar-section">
    <h2>Pair & Timeframe</h2>
    <div class="field"><label>Symbol</label>
      <select id="symbol">
        <optgroup label="Crypto">
          <option value="ETH-USD" selected>ETH-USD</option><option value="BTC-USD">BTC-USD</option>
          <option value="BNB-USD">BNB-USD</option><option value="SOL-USD">SOL-USD</option>
          <option value="XRP-USD">XRP-USD</option><option value="ADA-USD">ADA-USD</option>
          <option value="AVAX-USD">AVAX-USD</option><option value="DOGE-USD">DOGE-USD</option>
          <option value="MATIC-USD">MATIC-USD</option>
        </optgroup>
        <optgroup label="Forex">
          <option value="EURUSD=X">EUR/USD</option><option value="GBPUSD=X">GBP/USD</option>
          <option value="USDJPY=X">USD/JPY</option><option value="AUDUSD=X">AUD/USD</option>
          <option value="USDCAD=X">USD/CAD</option><option value="USDCHF=X">USD/CHF</option>
          <option value="NZDUSD=X">NZD/USD</option>
        </optgroup>
      </select>
    </div>
    <div class="field"><label>Interval</label>
      <select id="interval">
        <option value="15m" selected>15 Menit</option><option value="30m">30 Menit</option>
        <option value="1h">1 Jam</option><option value="4h">4 Jam</option><option value="1d">1 Hari</option>
      </select>
    </div>
    <div class="field row2">
      <div><label>Mulai</label><input type="date" id="startDate"></div>
      <div><label>Selesai</label><input type="date" id="endDate"></div>
    </div>
    <button class="btn btn-secondary" id="btnDownload">⬇ Download Data</button>
    <button class="btn btn-secondary" id="btnRefresh">📂 Load Cache</button>
    <div class="data-info" id="dataInfo">Belum ada data dimuat.</div>
    <button class="btn btn-accent" id="btnAnalyze" style="margin-top:1rem" disabled>🔍 Analisis Sinyal</button>
  </div>
  <div class="sidebar-section">
    <h2>Parameter Strategi</h2>
    <div class="field row2">
      <div><label>Jam Sinyal</label><input type="number" id="sigHour" value="21" min="0" max="23"></div>
      <div><label>Menit</label><input type="number" id="sigMin" value="30" min="0" max="59" step="15"></div>
    </div>
    <div class="field"><label>Risk per Trade (%)</label><input type="number" id="riskPct" value="10" min="1" max="100"></div>
    <div class="field"><label>Modal Awal ($)</label><input type="number" id="initEq" value="10000" min="100"></div>
  </div>
  <div class="sidebar-section">
    <h2>Kecepatan Animasi</h2>
    <div class="speed-wrap">
      <span style="font-size:.7rem;color:var(--muted)">Lambat</span>
      <input type="range" id="speedSlider" min="0" max="5" value="3" step="1">
      <span style="font-size:.7rem;color:var(--muted)">Cepat</span>
    </div>
    <div style="text-align:center;margin-top:.4rem"><span class="speed-val" id="speedLabel">200ms</span></div>
  </div>
  <div class="sidebar-section" style="flex:1;display:flex;flex-direction:column;gap:.5rem;justify-content:flex-end">
    <button class="btn btn-primary" id="btnStart" disabled>▶ Mulai Backtest</button>
    <button class="btn btn-danger" id="btnStop" style="display:none">⏹ Stop</button>
  </div>
</aside>
<main>
  <div id="analysisOverlay">
    <div class="analysis-card">
      <div class="analysis-title">🔍 Analisis Sinergi Candle <span id="anaRunningBadge" class="badge" style="background:var(--accent);color:#fff">Memproses...</span></div>
      <div class="ana-prog-wrap"><div id="anaProgFill" class="ana-prog-fill" style="width:0%"></div></div>
      <div class="ana-status-row"><span id="anaStatusTxt">Menghitung ranking profit...</span><span id="anaPctTxt">0%</span></div>
      <div class="ana-scroll">
        <table class="ana-table">
          <thead><tr><th>Rank</th><th>Waktu</th><th>Net Profit</th><th>Equity</th><th>Trade</th><th></th></tr></thead>
          <tbody id="anaBody"></tbody>
        </table>
      </div>
      <button class="btn btn-secondary" style="margin-top:1rem;background:transparent" onclick="hideAnalyze()">Tutup</button>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><div class="stat-label">Modal Awal</div><div class="stat-value" id="sInitial">$10,000</div></div>
    <div class="stat"><div class="stat-label">Modal Akhir</div><div class="stat-value" id="sFinal">—</div></div>
    <div class="stat"><div class="stat-label">Total P&L</div><div class="stat-value" id="sPnl">—</div></div>
    <div class="stat"><div class="stat-label">Return</div><div class="stat-value" id="sReturn">—</div></div>
    <div class="stat"><div class="stat-label">Transaksi</div><div class="stat-value" id="sTrades">0</div></div>
  </div>
  <div class="chart-area">
    <div class="chart-header"><span class="chart-title">📈 Equity Curve</span><span id="statusBadge">Idle</span></div>
    <div class="chart-wrap"><canvas id="eqChart"></canvas></div>
  </div>
  <div class="log-area">
    <div class="log-header"><span>📋 Log Transaksi</span><span id="logCount" style="color:var(--muted)">0 events</span></div>
    <div class="log-scroll">
      <table>
        <thead><tr><th>Waktu</th><th>Aksi</th><th>Harga</th><th>Lot</th><th>Equity</th></tr></thead>
        <tbody id="logBody"></tbody>
      </table>
    </div>
  </div>
</main>
</div>
<script>
const SPEED_MAP = [2000, 800, 400, 200, 80, 0];
const SPEED_LABELS = ['2000ms','800ms','400ms','200ms','80ms','Instant'];
const slider = document.getElementById('speedSlider');
slider.addEventListener('input', () => { document.getElementById('speedLabel').textContent = SPEED_LABELS[+slider.value]; });

const today = new Date();
const d59ago = new Date(); d59ago.setDate(today.getDate() - 59);
document.getElementById('endDate').value = today.toISOString().slice(0,10);
document.getElementById('startDate').value = d59ago.toISOString().slice(0,10);

let chart, chartLabels=[], chartData=[];
function initChart(initialEq) {
  if (chart) chart.destroy(); chartLabels=['Start']; chartData=[initialEq];
  const ctx = document.getElementById('eqChart').getContext('2d');
  const grad = ctx.createLinearGradient(0,0,0,280);
  grad.addColorStop(0,'rgba(99,102,241,0.3)'); grad.addColorStop(1,'rgba(99,102,241,0.0)');
  chart = new Chart(ctx, {
    type:'line', data:{ labels: chartLabels, datasets:[{ label:'Equity', data: chartData, borderColor:'#818cf8', borderWidth:2, pointRadius:0, pointHoverRadius:4, fill:true, backgroundColor:grad, tension:0.35 }] },
    options:{ responsive:true, maintainAspectRatio:false, animation:{duration:300}, interaction:{mode:'index',intersect:false}, plugins:{ legend:{display:false}, tooltip:{ backgroundColor:'#151824',borderColor:'#1f2538',borderWidth:1,titleColor:'#94a3b8',bodyColor:'#e2e8f0', callbacks:{label:c=>` Equity: $${c.parsed.y.toLocaleString('en-US',{minimumFractionDigits:2})}`} } }, scales:{ x:{ticks:{color:'#475569',maxTicksLimit:8},grid:{color:'#12151f'}}, y:{ticks:{color:'#475569',callback:v=>'$'+v.toLocaleString()},grid:{color:'#12151f'}} } }
  });
}
function addChartPoint(label, value) { chartLabels.push(label); chartData.push(value); chart.update('none'); }
function fmt(n){return n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
function updateStats(eq, initEq, trades) {
  const pnl=eq-initEq, ret=(pnl/initEq)*100;
  document.getElementById('sFinal').textContent = '$'+fmt(eq);
  document.getElementById('sPnl').textContent = (pnl>=0?'+':'')+ '$'+fmt(pnl);
  document.getElementById('sReturn').textContent = (ret>=0?'+':'')+ret.toFixed(2)+'%';
  document.getElementById('sTrades').textContent = trades;
  document.getElementById('sPnl').className = 'stat-value '+(pnl>=0?'green':'red');
  document.getElementById('sReturn').className = 'stat-value '+(ret>=0?'green':'red');
}

let logCount=0; const logBody=document.getElementById('logBody');
function addLogRow(e) {
  logCount++; document.getElementById('logCount').textContent = logCount+' events';
  const tr = document.createElement('tr');
  let cls = e.margin_call ? 'aksi-mc' : e.aksi.startsWith('TP') ? 'aksi-tp' : e.aksi.startsWith('SL') ? 'aksi-sl' : 'aksi-entry';
  tr.innerHTML = `<td>${e.waktu.slice(0,19)}</td><td class="${cls}">${e.aksi}</td><td>$${e.harga.toLocaleString()}</td><td>${e.lot}</td><td>$${fmt(e.equity)}</td>`;
  logBody.prepend(tr);
}
function setStatus(s,txt){ const b=document.getElementById('statusBadge'); b.className=s; b.textContent=txt; }

async function doDownload(cacheOnly=false) {
  const symbol=document.getElementById('symbol').value; const interval=document.getElementById('interval').value;
  const start=document.getElementById('startDate').value; const end=document.getElementById('endDate').value;
  const info=document.getElementById('dataInfo');
  info.innerHTML = cacheOnly ? '<span>Memuat cache...</span>' : '<span>Mengunduh...</span>';
  document.getElementById('btnStart').disabled=true; document.getElementById('btnDownload').disabled=true; document.getElementById('btnRefresh').disabled=true; document.getElementById('btnAnalyze').disabled=true;
  try {
    const r = await fetch('/api/download', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({symbol,interval,start,end,cache_only:cacheOnly}) });
    const j = await r.json();
    if (j.status === 'error') { info.innerHTML = `<span style="color:var(--red)">${j.message}</span>`; }
    else {
      info.innerHTML = `${j.status==='cached'?'📂 Cache':'✅ Baru'}: <span>${j.rows.toLocaleString()} baris</span><br>S/d: <span>${j.to.slice(0,16)}</span>`;
      document.getElementById('btnStart').disabled=false; document.getElementById('btnAnalyze').disabled=false;
    }
  } catch(e) { info.innerHTML = `<span style="color:var(--red)">Error: ${e.message}</span>`; }
  finally { document.getElementById('btnDownload').disabled=false; document.getElementById('btnRefresh').disabled=false; }
}
document.getElementById('btnDownload').addEventListener('click', ()=>doDownload(false));
document.getElementById('btnRefresh').addEventListener('click', ()=>doDownload(true));

let evtSource = null;
function stopBacktest() { if(evtSource){evtSource.close();evtSource=null;} setStatus('','Idle'); document.getElementById('btnStop').style.display='none'; document.getElementById('btnStart').disabled=false; }
function startBacktest() {
  if (evtSource) evtSource.close();
  const initEq = parseFloat(document.getElementById('initEq').value);
  logBody.innerHTML = ''; logCount = 0; updateStats(initEq, initEq, 0); initChart(initEq);
  document.getElementById('btnStart').disabled=true; document.getElementById('btnStop').style.display='block'; setStatus('running','Running...');
  const params = new URLSearchParams({ symbol:document.getElementById('symbol').value, interval:document.getElementById('interval').value, signal_hour:document.getElementById('sigHour').value, signal_minute:document.getElementById('sigMin').value, risk_pct:document.getElementById('riskPct').value, initial_eq:initEq, delay_ms:SPEED_MAP[+slider.value] });
  evtSource = new EventSource('/api/backtest?'+params.toString());
  evtSource.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if (d.type === 'trade') {
      addChartPoint(d.waktu.slice(0,16), d.equity); addLogRow(d); updateStats(d.equity, initEq, logCount);
      if (d.margin_call) { evtSource.close(); setStatus('margin-call','MARGIN CALL ☠'); document.getElementById('btnStop').style.display='none'; document.getElementById('btnStart').disabled=false; alert('MARGIN CALL! Modal telah habis.'); }
    } else if (d.type === 'done') { evtSource.close(); setStatus('done','Selesai ✓'); document.getElementById('btnStop').style.display='none'; document.getElementById('btnStart').disabled=false; }
    else if (d.type === 'error') { stopBacktest(); alert(d.message); }
  };
  evtSource.onerror = stopBacktest;
}
document.getElementById('btnStart').addEventListener('click', startBacktest);
document.getElementById('btnStop').addEventListener('click', stopBacktest);
document.getElementById('initEq').addEventListener('input', (e) => {
  const v = parseFloat(e.target.value)||0; document.getElementById('sInitial').textContent = '$'+v.toLocaleString(); initChart(v);
});

// ── Analysis ──
let anaSource = null;
function hideAnalyze(){ document.getElementById('analysisOverlay').style.display='none'; if(anaSource){anaSource.close();anaSource=null;} }
function showAnalyze(){
  document.getElementById('analysisOverlay').style.display='flex';
  document.getElementById('anaBody').innerHTML=''; document.getElementById('anaProgFill').style.width='0%';
  document.getElementById('anaPctTxt').textContent='0%'; document.getElementById('anaStatusTxt').textContent='Menyiapkan analisis...';
  document.getElementById('anaRunningBadge').style.display='inline-block';
  
  const params = new URLSearchParams({ symbol:document.getElementById('symbol').value, interval:document.getElementById('interval').value, risk_pct:document.getElementById('riskPct').value, initial_eq:document.getElementById('initEq').value });
  anaSource = new EventSource('/api/analyze?'+params.toString());
  anaSource.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if(d.type==='progress'){
      document.getElementById('anaProgFill').style.width=d.pct+'%';
      document.getElementById('anaPctTxt').textContent=d.pct+'%';
      document.getElementById('anaStatusTxt').textContent=`Mencoba waktu ${d.time}...`;
    } else if(d.type==='result'){
      anaSource.close(); anaSource=null;
      document.getElementById('anaRunningBadge').style.display='none';
      document.getElementById('anaStatusTxt').textContent='Analisis Selesai!';
      const body = document.getElementById('anaBody');
      d.data.forEach((r, i) => {
        const tr = document.createElement('tr');
        tr.innerHTML=`<td>#${i+1}</td><td><b>${r.time}</b></td><td class="${r.pnl>=0?'green':'red'}">$${fmt(r.pnl)}</td><td>$${fmt(r.final_equity)}</td><td>${r.trades}</td><td><button class="btn-use" onclick="useTime(${r.hour},${r.minute})">Gunakan</button></td>`;
        body.appendChild(tr);
      });
    }
  };
}
function useTime(h, m){
  document.getElementById('sigHour').value=h; document.getElementById('sigMin').value=m;
  hideAnalyze(); alert(`Waktu ditetapkan ke ${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`);
}
document.getElementById('btnAnalyze').addEventListener('click', showAnalyze);

initChart(parseFloat(document.getElementById('initEq').value));
</script>
</body>
</html>"""

@app.route('/')
def index(): return HTML

def open_browser(): time.sleep(1.2); webbrowser.open('http://localhost:5000')
if __name__ == '__main__':
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, port=5000, threaded=True)
