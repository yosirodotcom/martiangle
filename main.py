import os
import webbrowser
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────
# 1. LOAD DATA (dari CSV cache atau download)
# ─────────────────────────────────────────────
CSV_FILE = "eth_usd_15m.csv"

if os.path.exists(CSV_FILE):
    print(f"Memuat data dari cache: {CSV_FILE}")
    df = pd.read_csv(CSV_FILE, skiprows=[1, 2], index_col=0)
    df.index = pd.to_datetime(df.index).tz_convert('Asia/Jakarta')
else:
    print("Mengunduh data ETH-USD 15m dari Yahoo Finance...")
    start_date = (datetime.now(timezone.utc) - timedelta(days=59)).strftime("%Y-%m-%d")
    df = yf.download("ETH-USD", interval="15m", start=start_date)

    if df.empty:
        print("Gagal mengambil data. Coba lagi nanti.")
        exit()

    df.index = df.index.tz_convert('Asia/Jakarta')
    df.to_csv(CSV_FILE)
    print(f"Data berhasil disimpan ke '{CSV_FILE}' ({len(df)} baris)")

print(f"Total candle dimuat: {len(df)} | Dari: {df.index[0]} s/d {df.index[-1]}")

# ─────────────────────────────────────────────
# 2. PARAMETER BACKTEST
# ─────────────────────────────────────────────
INITIAL_EQUITY = 10000.0
equity          = INITIAL_EQUITY
risk_per_trade  = 0.10   # Risiko 10% dari equity terakhir

# State Machine
state        = 'WAITING'
high_break   = 0.0
low_break    = 0.0
candle_range = 0.0
current_lot  = 0.0
initial_qty  = 0.0

daily_log = []

# ─────────────────────────────────────────────
# 3. LOOP BAR-BY-BAR
# ─────────────────────────────────────────────
for current_time, row in df.iterrows():
    c_high = float(row['High'].iloc[0] if isinstance(row['High'], pd.Series) else row['High'])
    c_low  = float(row['Low'].iloc[0]  if isinstance(row['Low'],  pd.Series) else row['Low'])

    # A. Deteksi Candle Jam 21:30 WIB
    if current_time.hour == 21 and current_time.minute == 30:
        high_break   = c_high
        low_break    = c_low
        candle_range = high_break - low_break

        if candle_range > 0:
            risk_amount = equity * risk_per_trade
            initial_qty = risk_amount / candle_range
            state       = 'TRAP_SET'

    # B. Eksekusi & Martingale
    if state == 'TRAP_SET':
        if c_high > high_break:
            state       = 'LONG'
            current_lot = initial_qty
            daily_log.append({'Waktu': str(current_time), 'Aksi': 'Entry Buy Stop',
                               'Harga': round(high_break, 2), 'Lot': round(current_lot, 4), 'Equity': round(equity, 2)})
        elif c_low < low_break:
            state       = 'SHORT'
            current_lot = initial_qty
            daily_log.append({'Waktu': str(current_time), 'Aksi': 'Entry Sell Stop',
                               'Harga': round(low_break, 2), 'Lot': round(current_lot, 4), 'Equity': round(equity, 2)})

    elif state == 'LONG':
        tp_price = high_break + candle_range
        sl_price = low_break

        if c_high >= tp_price:
            profit  = (tp_price - high_break) * current_lot
            equity += profit
            state   = 'WAITING'
            daily_log.append({'Waktu': str(current_time), 'Aksi': 'TP Long',
                               'Harga': round(tp_price, 2), 'Lot': round(current_lot, 4), 'Equity': round(equity, 2)})
        elif c_low <= sl_price:
            loss    = (high_break - sl_price) * current_lot
            equity -= loss
            state   = 'SHORT'
            current_lot *= 2
            daily_log.append({'Waktu': str(current_time), 'Aksi': 'SL Long -> Reversal Short',
                               'Harga': round(sl_price, 2), 'Lot': round(current_lot, 4), 'Equity': round(equity, 2)})

    elif state == 'SHORT':
        tp_price = low_break - candle_range
        sl_price = high_break

        if c_low <= tp_price:
            profit  = (low_break - tp_price) * current_lot
            equity += profit
            state   = 'WAITING'
            daily_log.append({'Waktu': str(current_time), 'Aksi': 'TP Short',
                               'Harga': round(tp_price, 2), 'Lot': round(current_lot, 4), 'Equity': round(equity, 2)})
        elif c_high >= sl_price:
            loss    = (sl_price - low_break) * current_lot
            equity -= loss
            state   = 'LONG'
            current_lot *= 2
            daily_log.append({'Waktu': str(current_time), 'Aksi': 'SL Short -> Reversal Long',
                               'Harga': round(sl_price, 2), 'Lot': round(current_lot, 4), 'Equity': round(equity, 2)})

# ─────────────────────────────────────────────
# 4. EVALUASI HASIL (Console)
# ─────────────────────────────────────────────
log_df       = pd.DataFrame(daily_log)
total_pnl    = equity - INITIAL_EQUITY
pnl_pct      = (total_pnl / INITIAL_EQUITY) * 100
total_trades = len(log_df)

print("\n=== Ringkasan Backtest ===")
if not log_df.empty:
    print(log_df.tail(10).to_string(index=False))
else:
    print("Tidak ada transaksi yang tereksekusi.")

print(f"\nModal Awal     : ${INITIAL_EQUITY:,.2f}")
print(f"Modal Akhir    : ${equity:,.2f}")
print(f"Total P&L      : ${total_pnl:+,.2f} ({pnl_pct:+.2f}%)")
print(f"Total Transaksi: {total_trades}")

# ─────────────────────────────────────────────
# 5. GENERATE HTML REPORT & BUKA DI BROWSER
# ─────────────────────────────────────────────
print("\nMembuat laporan HTML...")

# Data untuk chart equity
equity_times  = ['Start'] + [row['Waktu'] for row in daily_log]
equity_values = [INITIAL_EQUITY] + [row['Equity'] for row in daily_log]

# Build tabel baris
rows_html = ""
for r in daily_log:
    pnl_class = 'profit' if 'TP' in r['Aksi'] else ('loss' if 'SL' in r['Aksi'] else '')
    rows_html += f"""<tr class="{pnl_class}">
        <td>{r['Waktu']}</td>
        <td>{r['Aksi']}</td>
        <td>${r['Harga']:,}</td>
        <td>{r['Lot']}</td>
        <td>${r['Equity']:,.2f}</td>
    </tr>"""

pnl_color = "#4ade80" if total_pnl >= 0 else "#f87171"
pnl_sign  = "+" if total_pnl >= 0 else ""

html = f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ETH-USD Backtest Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', sans-serif;
    background: #0f1117;
    color: #e2e8f0;
    min-height: 100vh;
    padding: 2rem;
  }}
  h1 {{
    font-size: 1.75rem;
    font-weight: 700;
    background: linear-gradient(90deg, #818cf8, #38bdf8);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 0.25rem;
  }}
  .subtitle {{
    color: #64748b;
    font-size: 0.875rem;
    margin-bottom: 2rem;
  }}
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
  }}
  .stat-card {{
    background: #1e2130;
    border: 1px solid #2d3348;
    border-radius: 12px;
    padding: 1.25rem 1.5rem;
  }}
  .stat-label {{
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #64748b;
    margin-bottom: 0.5rem;
  }}
  .stat-value {{
    font-size: 1.4rem;
    font-weight: 700;
    color: #e2e8f0;
  }}
  .stat-value.pnl {{ color: {pnl_color}; }}
  .chart-card {{
    background: #1e2130;
    border: 1px solid #2d3348;
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 2rem;
  }}
  .chart-title {{
    font-size: 0.85rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #94a3b8;
    margin-bottom: 1rem;
  }}
  .chart-wrap {{ position: relative; height: 320px; }}
  .table-card {{
    background: #1e2130;
    border: 1px solid #2d3348;
    border-radius: 12px;
    padding: 1.5rem;
    overflow-x: auto;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }}
  thead th {{
    background: #0f1117;
    padding: 0.7rem 1rem;
    text-align: left;
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #64748b;
    border-bottom: 1px solid #2d3348;
  }}
  tbody tr {{ border-bottom: 1px solid #1a1f2e; transition: background 0.15s; }}
  tbody tr:hover {{ background: #252a3a; }}
  tbody td {{ padding: 0.65rem 1rem; }}
  tbody tr.profit td:nth-child(2) {{ color: #4ade80; font-weight: 600; }}
  tbody tr.loss   td:nth-child(2) {{ color: #f87171; font-weight: 600; }}
  .empty {{ color: #64748b; text-align: center; padding: 3rem; }}
  .generated-at {{ color: #475569; font-size: 0.75rem; text-align: right; margin-top: 1.5rem; }}
</style>
</head>
<body>
<h1>ETH-USD Backtest Report</h1>
<p class="subtitle">Strategi: Trap Candle 21:30 WIB · Martingale Reversal · Interval 15m</p>

<div class="stats-grid">
  <div class="stat-card">
    <div class="stat-label">Modal Awal</div>
    <div class="stat-value">${INITIAL_EQUITY:,.0f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Modal Akhir</div>
    <div class="stat-value">${equity:,.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Total P&amp;L</div>
    <div class="stat-value pnl">{pnl_sign}${total_pnl:,.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Return</div>
    <div class="stat-value pnl">{pnl_sign}{pnl_pct:.2f}%</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Total Transaksi</div>
    <div class="stat-value">{total_trades}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Periode Data</div>
    <div class="stat-value" style="font-size:0.85rem;">{df.index[0].strftime('%d %b')}&nbsp;–&nbsp;{df.index[-1].strftime('%d %b %Y')}</div>
  </div>
</div>

<div class="chart-card">
  <div class="chart-title">📈 Equity Curve</div>
  <div class="chart-wrap">
    <canvas id="equityChart"></canvas>
  </div>
</div>

<div class="table-card">
  <div class="chart-title" style="margin-bottom:1rem;">📋 Log Transaksi</div>
  {'<table><thead><tr><th>Waktu</th><th>Aksi</th><th>Harga</th><th>Lot</th><th>Equity</th></tr></thead><tbody>' + rows_html + '</tbody></table>' if daily_log else '<p class="empty">Tidak ada transaksi yang tereksekusi.</p>'}
</div>

<p class="generated-at">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

<script>
const labels = {equity_times};
const data   = {equity_values};

const gradient = (ctx) => {{
  const g = ctx.createLinearGradient(0, 0, 0, 300);
  g.addColorStop(0,   'rgba(99,102,241,0.35)');
  g.addColorStop(1,   'rgba(99,102,241,0.00)');
  return g;
}};

new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels,
    datasets: [{{
      label: 'Equity ($)',
      data,
      borderColor: '#818cf8',
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      pointHoverBackgroundColor: '#818cf8',
      fill: true,
      backgroundColor: (ctx) => gradient(ctx.chart.ctx),
      tension: 0.3,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#1e2130',
        borderColor: '#2d3348',
        borderWidth: 1,
        titleColor: '#94a3b8',
        bodyColor: '#e2e8f0',
        callbacks: {{
          label: (ctx) => ` ${{ctx.dataset.label}}: $${{ctx.parsed.y.toLocaleString('en-US', {{minimumFractionDigits:2}})}}`
        }}
      }}
    }},
    scales: {{
      x: {{
        ticks: {{ color: '#475569', maxTicksLimit: 10, maxRotation: 0 }},
        grid:  {{ color: '#1a1f2e' }}
      }},
      y: {{
        ticks: {{ color: '#475569', callback: (v) => '$' + v.toLocaleString() }},
        grid:  {{ color: '#1a1f2e' }}
      }}
    }}
  }}
}});
</script>
</body>
</html>"""

REPORT_FILE = "backtest_report.html"
with open(REPORT_FILE, "w", encoding="utf-8") as f:
    f.write(html)

print(f"Laporan disimpan ke '{REPORT_FILE}'")
webbrowser.open(f"file:///{os.path.abspath(REPORT_FILE)}")
print("Browser dibuka. Selesai!")