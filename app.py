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
def run_backtest(df, signal_hour, signal_minute, risk_pct, initial_equity, min_range=9.0, spread=0.0, leverage=100.0):
    """Generator: yields dict per trade event."""
    equity       = initial_equity
    state        = 'WAITING'
    high_break   = low_break = candle_range = current_lot = initial_qty = 0.0
    current_day  = None
    reversal_done = False

    for current_time, row in df.iterrows():
        # Day Shift Detection (Midnight)
        if current_day is not None and current_time.date() != current_day:
            if state in ['LONG', 'SHORT', 'TRAP_SET']:
                if state in ['LONG', 'SHORT']:
                    exit_price = float(row['Open'].iloc[0] if isinstance(row['Open'], pd.Series) else row['Open'])
                    pnl = (exit_price - entry_price) * current_lot if state == 'LONG' else (entry_price - exit_price) * current_lot
                    equity += pnl
                    yield {'waktu': str(current_time), 'aksi': 'Daily Reset: Close', 'harga': round(exit_price, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
                else:
                    yield {'waktu': str(current_time), 'aksi': 'Daily Reset: Cancel', 'harga': 0, 'lot': 0, 'equity': round(equity, 2)}
                state = 'WAITING'
            reversal_done = False
        current_day = current_time.date()

        c_high = float(row['High'].iloc[0] if isinstance(row['High'], pd.Series) else row['High'])
        c_low  = float(row['Low'].iloc[0]  if isinstance(row['Low'],  pd.Series) else row['Low'])

        if current_time.hour == signal_hour and current_time.minute == signal_minute:
            high_break   = c_high
            low_break    = c_low
            candle_range = high_break - low_break
            if candle_range >= min_range:
                risk_amount = equity * (risk_pct / 100.0)
                initial_qty = risk_amount / candle_range
                state       = 'TRAP_SET'
                reversal_done = False

        if state == 'TRAP_SET':
            if c_high > high_break:
                state = 'LONG'; current_lot = initial_qty
                entry_price = high_break + spread
                yield {'waktu': str(current_time), 'aksi': 'Entry Buy Stop',
                       'harga': round(entry_price, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
            elif c_low < low_break:
                state = 'SHORT'; current_lot = initial_qty
                entry_price = low_break - spread
                yield {'waktu': str(current_time), 'aksi': 'Entry Sell Stop',
                       'harga': round(entry_price, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}

        elif state == 'LONG':
            entry_price = high_break + spread
            tp = high_break + candle_range
            sl = low_break
            
            # Check Margin Call on mid-trade drawdown
            floating_loss = (c_low - entry_price) * current_lot
            margin_used = (entry_price * current_lot) / leverage
            if (equity + floating_loss) < margin_used:
                equity = equity + floating_loss
                if equity < 0: equity = 0
                yield {'waktu': str(current_time), 'aksi': 'MARGIN CALL',
                       'harga': round(c_low, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2),
                       'margin_call': True}
                return

            if c_high >= tp:
                equity += (tp - entry_price) * current_lot; state = 'WAITING'
                yield {'waktu': str(current_time), 'aksi': 'TP Long',
                       'harga': round(tp, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
            elif c_low <= sl:
                equity -= (entry_price - sl) * current_lot
                if equity <= 0:
                    equity = 0
                    yield {'waktu': str(current_time), 'aksi': 'MARGIN CALL',
                           'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': 0.0,
                           'margin_call': True}
                    return
                if not reversal_done:
                    state = 'SHORT'; current_lot *= 2; reversal_done = True
                    yield {'waktu': str(current_time), 'aksi': 'SL Long -> Reversal Short',
                           'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
                else:
                    state = 'WAITING'
                    yield {'waktu': str(current_time), 'aksi': 'SL Long -> Done For Day',
                           'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}

        elif state == 'SHORT':
            entry_price = low_break - spread
            tp = low_break - candle_range
            sl = high_break
            
            # Check Margin Call on mid-trade drawdown
            floating_loss = (entry_price - c_high) * current_lot
            margin_used = (entry_price * current_lot) / leverage
            if (equity + floating_loss) < margin_used:
                equity = equity + floating_loss
                if equity < 0: equity = 0
                yield {'waktu': str(current_time), 'aksi': 'MARGIN CALL',
                       'harga': round(c_high, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2),
                       'margin_call': True}
                return

            if c_low <= tp:
                equity += (entry_price - tp) * current_lot; state = 'WAITING'
                yield {'waktu': str(current_time), 'aksi': 'TP Short',
                       'harga': round(tp, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
            elif c_high >= sl:
                equity -= (sl - entry_price) * current_lot
                if equity <= 0:
                    equity = 0
                    yield {'waktu': str(current_time), 'aksi': 'MARGIN CALL',
                           'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': 0.0,
                           'margin_call': True}
                    return
                if not reversal_done:
                    state = 'LONG'; current_lot *= 2; reversal_done = True
                    yield {'waktu': str(current_time), 'aksi': 'SL Short -> Reversal Long',
                           'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}
                else:
                    state = 'WAITING'
                    yield {'waktu': str(current_time), 'aksi': 'SL Short -> Done For Day',
                           'harga': round(sl, 2), 'lot': round(current_lot, 4), 'equity': round(equity, 2)}

def calculate_backtest_summary(df, hour, minute, risk_pct, initial_eq, min_range, spread, leverage):
    """Fast version of backtest for analysis ranking."""
    equity = initial_eq
    final_equity = initial_eq
    trades = 0
    margin_called = False
    
    peak_equity = initial_eq
    max_dd_pct = 0.0
    total_sequences = 0
    won_sequences = 0
    first_open_wins = 0
    current_sequence_trades = 0

    gen = run_backtest(df, hour, minute, risk_pct, initial_eq, min_range, spread, leverage)
    for event in gen:
        trades += 1
        current_sequence_trades += 1
        
        if event['equity'] > peak_equity:
            peak_equity = event['equity']
            
        if peak_equity > 0:
            dd_pct = (peak_equity - event['equity']) / peak_equity * 100
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                
        final_equity = event['equity']
        
        aksi = event.get('aksi', '')
        if 'Entry' in aksi:
            if current_sequence_trades == 1:
                total_sequences += 1
        elif 'TP' in aksi:
            won_sequences += 1
            if current_sequence_trades == 2: # 1 entry + 1 TP
                first_open_wins += 1
            current_sequence_trades = 0
            
        if event.get('margin_call'):
            margin_called = True
            break
            
    win_rate = (won_sequences / total_sequences * 100) if total_sequences > 0 else 0.0
    first_open_rate = (first_open_wins / total_sequences * 100) if total_sequences > 0 else 0.0

    return {
        'final_equity': final_equity, 
        'trades': trades, 
        'pnl': final_equity - initial_eq, 
        'margin_call': margin_called,
        'max_dd_pct': max_dd_pct,
        'win_rate': win_rate,
        'first_open_rate': first_open_rate
    }

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
    min_range     = float(request.args.get('min_range',   '9'))
    spread        = float(request.args.get('spread',      '0'))
    leverage      = float(request.args.get('leverage',    '100'))
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
        for event in run_backtest(df, signal_hour, signal_minute, risk_pct, initial_eq, min_range, spread, leverage):
            trades += 1
            yield f"data: {json.dumps({'type':'trade',**event})}\n\n"
            if delay_ms > 0: time.sleep(delay_ms / 1000.0)
        yield f"data: {json.dumps({'type':'done','total_trades':trades})}\n\n"

    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/analyze')
def api_analyze():
    symbol     = request.args.get('symbol',   'ETH-USD')
    risk_pct   = float(request.args.get('risk_pct',   '10'))
    initial_eq = float(request.args.get('initial_eq', '10000'))
    min_range  = float(request.args.get('min_range',  '9'))
    spread     = float(request.args.get('spread',     '0'))
    leverage   = float(request.args.get('leverage',   '100'))

    safe_sym = symbol.replace('/', '-')
    intervals_to_test = ['15m', '30m', '1h', '4h', '1d']

    def generate():
        results = []
        datasets = {}
        
        yield f"data: {json.dumps({'type':'progress', 'pct': 0, 'time': 'Init', 'msg': 'Memulai Multi-Timeframe Analysis...'})}\n\n"
        
        for inv in intervals_to_test:
            csv_path = os.path.join(CACHE_DIR, f"{safe_sym}_{inv}.csv")
            if not os.path.exists(csv_path):
                yield f"data: {json.dumps({'type':'progress', 'pct': 0, 'time': inv, 'msg': f'Mengunduh data {inv}...'})}\n\n"
                try:
                    kwargs = dict(interval=inv)
                    if inv in ['15m', '30m']:
                        kwargs['start'] = (datetime.now(timezone.utc) - timedelta(days=59)).strftime('%Y-%m-%d')
                    elif inv in ['1h', '4h']:
                        kwargs['start'] = (datetime.now(timezone.utc) - timedelta(days=729)).strftime('%Y-%m-%d')
                    else:
                        kwargs['period'] = 'max'
                    
                    df_down = yf.download(symbol, **kwargs, progress=False)
                    if not df_down.empty:
                        df_down.index = df_down.index.tz_convert('Asia/Jakarta')
                        df_down.to_csv(csv_path)
                except Exception:
                    pass
            
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path, skiprows=[1, 2], index_col=0)
                    df.index = pd.to_datetime(df.index).tz_convert('Asia/Jakarta')
                    for col in ['High','Low']:
                        if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
                    df = df.dropna(subset=['High','Low'])
                    if not df.empty:
                        unique_slots = sorted(list(set([(t.hour, t.minute) for t in df.index])))
                        datasets[inv] = {'df': df, 'slots': unique_slots}
                except Exception:
                    pass

        if not datasets:
            def err(): yield f"data: {json.dumps({'status':'error','message':'Gagal memuat atau mengunduh data'})}\n\n"
            return err()

        total_slots = sum(len(d['slots']) for d in datasets.values())
        processed = 0
        
        for inv, data in datasets.items():
            df = data['df']
            for (h, m) in data['slots']:
                summary = calculate_backtest_summary(df, h, m, risk_pct, initial_eq, min_range, spread, leverage)
                results.append({
                    'interval': inv,
                    'hour': h, 'minute': m, 'time': f"{h:02}:{m:02}",
                    'final_equity': round(summary['final_equity'], 2),
                    'trades': summary['trades'],
                    'pnl': round(summary['pnl'], 2),
                    'margin_call': summary.get('margin_call', False),
                    'max_dd_pct': round(summary.get('max_dd_pct', 0), 2),
                    'win_rate': round(summary.get('win_rate', 0), 2),
                    'first_open_rate': round(summary.get('first_open_rate', 0), 2)
                })
                processed += 1
                if processed % 5 == 0 or processed == total_slots:
                    msg = f"Menganalisis {inv} {h:02}:{m:02}..."
                    yield f"data: {json.dumps({'type':'progress', 'pct': round((processed)/total_slots * 100), 'time': f'{inv} {h:02}:{m:02}', 'msg': msg})}\n\n"

        # Sort based on P&L descending
        results.sort(key=lambda x: x['pnl'], reverse=True)
        yield f"data: {json.dumps({'type':'result', 'data': results})}\n\n"

    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/optimize')
def api_optimize():
    symbol     = request.args.get('symbol',   'ETH-USD')
    initial_eq = float(request.args.get('initial_eq', '10000'))
    spread     = float(request.args.get('spread',     '0'))
    leverage   = float(request.args.get('leverage',   '100'))
    
    # Comma separated inputs
    opt_intervals = request.args.get('opt_intervals', '15m').split(',')
    opt_risks     = [float(x.strip()) for x in request.args.get('opt_risks', '10').split(',') if x.strip()]
    opt_ranges    = [float(x.strip()) for x in request.args.get('opt_ranges', '9').split(',') if x.strip()]
    
    # Date filters
    start_date    = request.args.get('start_date', '')
    end_date      = request.args.get('end_date', '')

    safe_sym = symbol.replace('/', '-')

    def generate():
        results = []
        datasets = {}
        
        yield f"data: {json.dumps({'type':'progress', 'pct': 0, 'msg': 'Memulai optimasi kombinasi...'})}\n\n"
        
        for inv in opt_intervals:
            inv = inv.strip()
            if not inv: continue
            
            csv_path = os.path.join(CACHE_DIR, f"{safe_sym}_{inv}.csv")
            if not os.path.exists(csv_path):
                yield f"data: {json.dumps({'type':'progress', 'pct': 0, 'msg': f'Mengunduh {inv}...'})}\n\n"
                try:
                    kwargs = dict(interval=inv)
                    if start_date:
                        kwargs['start'] = start_date
                    elif inv in ['15m', '30m']:
                        kwargs['start'] = (datetime.now(timezone.utc) - timedelta(days=59)).strftime('%Y-%m-%d')
                    elif inv in ['1h', '4h']:
                        kwargs['start'] = (datetime.now(timezone.utc) - timedelta(days=729)).strftime('%Y-%m-%d')
                    else:
                        kwargs['period'] = 'max'
                    if end_date: kwargs['end'] = end_date
                    
                    df_down = yf.download(symbol, **kwargs, progress=False)
                    if not df_down.empty:
                        df_down.index = df_down.index.tz_convert('Asia/Jakarta')
                        df_down.to_csv(csv_path)
                except Exception:
                    pass
            
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path, skiprows=[1, 2], index_col=0)
                    df.index = pd.to_datetime(df.index).tz_convert('Asia/Jakarta')
                    if start_date or end_date:
                        try:
                            # Parse dates correctly assuming start/end are string dates (YYYY-MM-DD)
                            s_idx = pd.to_datetime(start_date).tz_localize('Asia/Jakarta') if start_date else df.index[0]
                            e_idx = pd.to_datetime(end_date).tz_localize('Asia/Jakarta') + timedelta(days=1) if end_date else df.index[-1]
                            df = df.loc[s_idx:e_idx]
                        except Exception:
                            pass
                    
                    for col in ['High','Low']:
                        if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
                    df = df.dropna(subset=['High','Low'])
                    if not df.empty:
                        unique_slots = sorted(list(set([(t.hour, t.minute) for t in df.index])))
                        datasets[inv] = {'df': df, 'slots': unique_slots}
                except Exception:
                    pass

        if not datasets:
            def err(): yield f"data: {json.dumps({'status':'error','message':'Gagal memuat atau mengunduh data'})}\n\n"
            return err()

        total_combinations = sum(len(d['slots']) for d in datasets.values()) * len(opt_risks) * len(opt_ranges)
        processed = 0
        
        for inv, data in datasets.items():
            df = data['df']
            for (h, m) in data['slots']:
                for risk in opt_risks:
                    for m_range in opt_ranges:
                        summary = calculate_backtest_summary(df, h, m, risk, initial_eq, m_range, spread, leverage)
                        results.append({
                            'interval': inv,
                            'hour': h, 'minute': m, 'time': f"{h:02}:{m:02}",
                            'risk': risk,
                            'min_range': m_range,
                            'final_equity': round(summary['final_equity'], 2),
                            'trades': summary['trades'],
                            'pnl': round(summary['pnl'], 2),
                            'margin_call': summary.get('margin_call', False),
                            'max_dd_pct': round(summary.get('max_dd_pct', 0), 2),
                            'win_rate': round(summary.get('win_rate', 0), 2),
                            'first_open_rate': round(summary.get('first_open_rate', 0), 2)
                        })
                        processed += 1
                        if processed % 15 == 0 or processed == total_combinations:
                            msg = f"Optimasi: {processed}/{total_combinations}"
                            yield f"data: {json.dumps({'type':'progress', 'pct': round((processed)/total_combinations * 100), 'msg': msg})}\n\n"

        # Hierarchical Sort (Updated): 
        # 1. Not Margin Call (False/0 first)
        # 2. Highest Net Profit (Descending: -pnl)
        # 3. Lowest Max Drawdown (Ascending)
        # 4. Highest Win Rate (Descending)
        results.sort(key=lambda x: (1 if x['margin_call'] else 0, -x['pnl'], x['max_dd_pct'], -x['win_rate']))
        top_100 = results[:100]
        
        # Filter for failures (Margin Call)
        failures = [r for r in results if r.get('margin_call') or r.get('final_equity', 0) <= 0]
        bottom_10 = failures[:10]
        
        yield f"data: {json.dumps({'type':'result', 'top': top_100, 'bottom': bottom_10})}\n\n"

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

/* Tabs */
.tab-headers { display:flex; border-bottom:1px solid var(--border); margin-bottom:.5rem; }
.tab-btn { flex:1; background:transparent; border:none; color:var(--muted); padding:.75rem 0; font-size:.72rem; font-weight:600; text-transform:uppercase; letter-spacing:.08em; cursor:pointer; transition:all .15s; border-bottom:2px solid transparent; font-family:inherit; }
.tab-btn:hover { color:var(--text); background:rgba(255,255,255,0.03); }
.tab-btn.active { color:var(--accent); border-bottom-color:var(--accent); }
.tab-pane { display:none; flex-direction:column; flex:1; gap:.5rem; overflow-y:auto; padding:0 1.25rem 1.1rem 1.25rem; }
.tab-pane.active { display:flex; }
.tab-pane .sidebar-section { padding:0; border-bottom:none; margin-bottom:.5rem; }

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
/* Dual Range Slider Styles */
.range-slider-container { position: relative; width: 100%; height: 36px; margin-top: 5px; }
.range-slider-track { position: absolute; top: 15px; width: 100%; height: 4px; background: #1e2538; border-radius: 4px; z-index: 1; }
.range-slider-shroud { position: absolute; top: 15px; height: 4px; background: var(--accent); border-radius: 4px; z-index: 2; transition: left 0.1s, width 0.1s; }
.range-input-wrap { position: relative; width: 100%; }
.range-input-wrap input[type="range"] {
  position: absolute; width: 100%; top: 5px; background: none; pointer-events: none; -webkit-appearance: none; z-index: 3; margin: 0;
}
.range-input-wrap input[type="range"]::-webkit-slider-thumb {
  height: 18px; width: 18px; border-radius: 50%; background: #fff; border: 2px solid var(--accent); 
  cursor: pointer; pointer-events: auto; -webkit-appearance: none; box-shadow: 0 0 5px rgba(0,0,0,0.5);
}
.range-input-wrap input[type="range"]::-moz-range-thumb {
  height: 18px; width: 18px; border-radius: 50%; background: #fff; border: 2px solid var(--accent); 
  cursor: pointer; pointer-events: auto; box-shadow: 0 0 5px rgba(0,0,0,0.5);
}
.range-values { display: flex; justify-content: space-between; font-size: 0.7rem; color: var(--muted); margin-bottom: 2px; }
.range-values b { color: var(--accent); font-weight: 700; }
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
  </div>
  
  <div class="tab-headers" style="padding: 0 1.25rem;">
    <button class="tab-btn active" data-target="tabAnalisis">Analisis</button>
    <button class="tab-btn" data-target="tabBacktest">Backtest</button>
    <button class="tab-btn" data-target="tabOptimasi">Optimasi</button>
  </div>

  <div id="tabAnalisis" class="tab-pane active" style="flex:1;">
    <div class="sidebar-section" style="padding-top:1rem;border-bottom:1px solid var(--border);">
      <h2>Parameter Analisis</h2>
      <div class="field row2">
        <div><label>Min Range</label><input type="number" id="anaMinRange" value="10" min="0" step="0.1"></div>
        <div><label>Spread</label><input type="number" id="anaSpread" value="2.0" min="0" step="0.01"></div>
      </div>
      <div class="field row2">
        <div><label>Risk / Trade (%)</label><input type="number" id="anaRisk" value="10" min="1" max="100"></div>
        <div><label>Leverage (x)</label><input type="number" id="anaLeverage" value="100" min="1"></div>
      </div>
      <div class="field"><label>Modal Awal ($)</label><input type="number" id="anaInitEq" value="10000" min="100"></div>
    </div>
    <div class="sidebar-section" style="flex:1;display:flex;flex-direction:column;justify-content:flex-start;padding-top:1rem;border-bottom:none;">
      <div class="data-info" style="margin-bottom: 1rem; margin-top: 0;">Mencari probabilitas HH:MM terbaik di Timeframe 15m - 1d.</div>
      <button class="btn btn-accent" id="btnAnalyze" disabled>🔍 Analisis Sinyal</button>
    </div>
  </div>

  <div id="tabBacktest" class="tab-pane" style="flex:1;">
    <div class="sidebar-section" style="padding-top:1rem;border-bottom:1px solid var(--border);">
      <h2>Parameter Backtest</h2>
      <div class="field row2">
        <div><label>Jam Sinyal</label><input type="number" id="btHour" value="21" min="0" max="23"></div>
        <div><label>Menit</label><input type="number" id="btMin" value="30" min="0" max="59" step="15"></div>
      </div>
      <div class="field row2">
        <div><label>Min Range</label><input type="number" id="btMinRange" value="9" min="0" step="0.1"></div>
        <div><label>Spread</label><input type="number" id="btSpread" value="2.0" min="0" step="0.01"></div>
      </div>
      <div class="field row2">
        <div><label>Risk / Trade (%)</label><input type="number" id="btRisk" value="10" min="1" max="100"></div>
        <div><label>Leverage (x)</label><input type="number" id="btLeverage" value="100" min="1"></div>
      </div>
      <div class="field"><label>Modal Awal ($)</label><input type="number" id="btInitEq" value="10000" min="100"></div>
    </div>
    <div class="sidebar-section" style="border-bottom:none;">
      <h2>Kecepatan Animasi</h2>
      <div class="speed-wrap">
        <span style="font-size:.7rem;color:var(--muted)">Lambat</span>
        <input type="range" id="speedSlider" min="0" max="5" value="3" step="1">
        <span style="font-size:.7rem;color:var(--muted)">Cepat</span>
      </div>
      <div style="text-align:center;margin-top:.4rem"><span class="speed-val" id="speedLabel">200ms</span></div>
    </div>
    <div class="sidebar-section" style="flex:1;display:flex;flex-direction:column;gap:.5rem;justify-content:flex-end;margin-bottom:0">
      <button class="btn btn-primary" id="btnStart" disabled>▶ Mulai Backtest</button>
      <button class="btn btn-danger" id="btnStop" style="display:none">⏹ Stop</button>
    </div>
  </div>

  <div id="tabOptimasi" class="tab-pane" style="flex:1;">
    <div class="sidebar-section" style="padding-top:1rem;border-bottom:1px solid var(--border);">
      <h2>Parameter Optimasi</h2>
      <div class="data-info" style="margin-top:0;margin-bottom:10px;">Gunakan slider untuk memilih rentang pengetesan</div>
      
      <div class="field">
        <label>Min Range (Area Pencarian)</label>
        <div class="range-values"><span>Min: <b id="optM1V">5</b></span><span>Max: <b id="optM2V">15</b></span></div>
        <div class="range-slider-container">
          <div class="range-slider-track"></div>
          <div id="mShroud" class="range-slider-shroud" style="left:10%; width:20%"></div>
          <div class="range-input-wrap">
            <input type="range" id="optMin1" min="1" max="50" value="5" oninput="syncRange('m')">
            <input type="range" id="optMin2" min="1" max="50" value="15" oninput="syncRange('m')">
          </div>
        </div>
      </div>
      <div class="field">
        <label>Risk / Trade % (Area Pencarian)</label>
        <div class="range-values"><span>Min: <b id="optR1V">5</b>%</span><span>Max: <b id="optR2V">10</b>%</span></div>
        <div class="range-slider-container">
          <div class="range-slider-track"></div>
          <div id="rShroud" class="range-slider-shroud" style="left:10%; width:10%"></div>
          <div class="range-input-wrap">
            <input type="range" id="optRisk1" min="1" max="50" value="5" oninput="syncRange('r')">
            <input type="range" id="optRisk2" min="1" max="50" value="10" oninput="syncRange('r')">
          </div>
        </div>
      </div>
      <div class="field">
        <label>Interval yang Dites</label>
        <div style="display:flex; gap:10px; margin-top:5px; flex-wrap:wrap;">
           <label><input type="checkbox" class="opt-inv" value="15m" checked> 15m</label>
           <label><input type="checkbox" class="opt-inv" value="30m"> 30m</label>
           <label><input type="checkbox" class="opt-inv" value="1h" checked> 1h</label>
           <label><input type="checkbox" class="opt-inv" value="4h"> 4h</label>
           <label><input type="checkbox" class="opt-inv" value="1d"> 1d</label>
        </div>
      </div>
      <div class="field row2" style="margin-top:10px;">
        <div><label>Spread</label><input type="number" id="optSpread" value="2.0" min="0" step="0.1"></div>
        <div><label>Leverage</label><input type="number" id="optLeverage" value="100" min="1"></div>
      </div>
      <div class="field"><label>Modal Awal ($)</label><input type="number" id="optInitEq" value="10000" min="100"></div>
    </div>
    <div class="sidebar-section" style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;border-bottom:none;">
      <button class="btn btn-accent" id="btnOptimize" disabled>⚡ Mulai Optimasi</button>
    </div>
  </div>
</aside>
<main>
  <div id="optimizeOverlay">
    <div class="analysis-card">
      <div class="analysis-title">⚡ Brute-Force Optimasi <span id="optRunningBadge" class="badge" style="background:var(--accent);color:#fff">Memproses...</span></div>
      <div class="ana-prog-wrap"><div id="optProgFill" class="ana-prog-fill" style="width:0%"></div></div>
      <div class="ana-status-row"><span id="optStatusTxt">Menyiapkan permutasi...</span><span id="optPctTxt">0%</span></div>
      <div class="ana-scroll">
        <table class="ana-table">
          <thead><tr><th>Rank</th><th>Setting (Inv | Waktu | Range | Risk)</th><th>Net Profit</th><th>Equity</th><th>Max DD</th><th>Win Rate</th><th>Trade</th></tr></thead>
          <tbody id="optBody"></tbody>
        </table>
      </div>
      <button class="btn btn-secondary" style="margin-top:1rem;background:transparent" onclick="hideOptimize()">Tutup</button>
    </div>
  </div>
  <div id="analysisOverlay">
    <div class="analysis-card">
      <div class="analysis-title">🔍 Analisis Sinergi Candle <span id="anaRunningBadge" class="badge" style="background:var(--accent);color:#fff">Memproses...</span></div>
      <div class="ana-prog-wrap"><div id="anaProgFill" class="ana-prog-fill" style="width:0%"></div></div>
      <div class="ana-status-row"><span id="anaStatusTxt">Menghitung ranking profit...</span><span id="anaPctTxt">0%</span></div>
      <div class="ana-scroll">
        <table class="ana-table">
          <thead><tr><th>Rank</th><th>Interval</th><th>Waktu</th><th>Net Profit</th><th>Equity</th><th>Max DD</th><th>Win Rate</th><th>1st Win</th><th>Trade</th><th></th></tr></thead>
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

function syncRange(prefix) {
  const i1 = document.getElementById(prefix === 'm' ? 'optMin1' : 'optRisk1');
  const i2 = document.getElementById(prefix === 'm' ? 'optMin2' : 'optRisk2');
  const label1 = document.getElementById(prefix === 'm' ? 'optM1V' : 'optR1V');
  const label2 = document.getElementById(prefix === 'm' ? 'optM2V' : 'optR2V');
  const shroud = document.getElementById(prefix === 'm' ? 'mShroud' : 'rShroud');
  
  let val1 = parseInt(i1.value), val2 = parseInt(i2.value);
  let min = Math.min(val1, val2), max = Math.max(val1, val2);
  
  label1.textContent = min; label2.textContent = max;
  
  const total = parseInt(i1.max);
  shroud.style.left = (min / total * 100) + '%';
  shroud.style.width = ((max - min) / total * 100) + '%';
}
// Initial sync
syncRange('m'); syncRange('r');

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
  document.getElementById('btnStart').disabled=false; document.getElementById('btnAnalyze').disabled=false; document.getElementById('btnOptimize').disabled=false;
    }
  } catch(e) { info.innerHTML = `<span style="color:var(--red)">Error: ${e.message}</span>`; }
  finally { document.getElementById('btnDownload').disabled=false; document.getElementById('btnRefresh').disabled=false; }
}
document.getElementById('btnDownload').addEventListener('click', ()=>doDownload(false));
document.getElementById('btnRefresh').addEventListener('click', ()=>doDownload(true));

function useTime(h, m){
  document.getElementById('btHour').value=h; document.getElementById('btMin').value=m;
  hideAnalyze(); hideOptimize(); alert(`Waktu ditetapkan ke ${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')} untuk Backtest.`);
  
  // Switch to backtest tab automatically when a time is selected
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(img => img.classList.remove('active'));
  document.querySelector('.tab-btn[data-target="tabBacktest"]').classList.add('active');
  document.getElementById('tabBacktest').classList.add('active');
}
document.getElementById('btnAnalyze').addEventListener('click', showAnalyze);

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.target).classList.add('active');
  });
});

initChart(parseFloat(document.getElementById('btInitEq').value));

let evtSource = null;
function stopBacktest() { if(evtSource){evtSource.close();evtSource=null;} setStatus('','Idle'); document.getElementById('btnStop').style.display='none'; document.getElementById('btnStart').disabled=false; }
function startBacktest() {
  if (evtSource) evtSource.close();
  const initEq = parseFloat(document.getElementById('btInitEq').value);
  logBody.innerHTML = ''; logCount = 0; updateStats(initEq, initEq, 0); initChart(initEq);
  document.getElementById('btnStart').disabled=true; document.getElementById('btnStop').style.display='block'; setStatus('running','Running...');
  const params = new URLSearchParams({ 
    symbol: document.getElementById('symbol').value, 
    interval: document.getElementById('interval').value, 
    signal_hour: document.getElementById('btHour').value, 
    signal_minute: document.getElementById('btMin').value, 
    risk_pct: document.getElementById('btRisk').value, 
    initial_eq: initEq, 
    min_range: document.getElementById('btMinRange').value,
    spread: document.getElementById('btSpread').value,
    leverage: document.getElementById('btLeverage').value,
    delay_ms: SPEED_MAP[+slider.value] 
  });
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
document.getElementById('btInitEq').addEventListener('input', (e) => {
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
  
  const params = new URLSearchParams({ 
    symbol: document.getElementById('symbol').value, 
    risk_pct: document.getElementById('anaRisk').value, 
    initial_eq: document.getElementById('anaInitEq').value,
    min_range: document.getElementById('anaMinRange').value,
    spread: document.getElementById('anaSpread').value,
    leverage: document.getElementById('anaLeverage').value 
  });
  anaSource = new EventSource('/api/analyze?'+params.toString());
  anaSource.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if(d.type==='progress'){
      document.getElementById('anaProgFill').style.width=d.pct+'%';
      document.getElementById('anaPctTxt').textContent=d.pct+'%';
      if(d.msg) document.getElementById('anaStatusTxt').textContent=d.msg;
      else document.getElementById('anaStatusTxt').textContent=`Mencoba waktu ${d.time}...`;
    } else if(d.type==='result'){
      anaSource.close(); anaSource=null;
      document.getElementById('anaRunningBadge').style.display='none';
      document.getElementById('anaStatusTxt').textContent='Analisis Selesai!';
      const body = document.getElementById('anaBody');
      d.data.forEach((r, i) => {
        const tr = document.createElement('tr');
        let statusHtml = `$${fmt(r.final_equity)}`;
        if (r.margin_call || r.final_equity <= 0) {
            statusHtml = `<span class="badge" style="background:rgba(248,113,113,0.15);color:var(--red);border:1px solid rgba(248,113,113,0.3)">MARGIN CALL</span>`;
        }
        let intervalBadge = `<span class="badge" style="background:rgba(255,255,255,0.1);color:var(--accent);border:1px solid rgba(255,255,255,0.2)">${r.interval}</span>`;
        tr.innerHTML=`<td>#${i+1}</td><td>${intervalBadge}</td><td><b>${r.time}</b></td><td class="${r.pnl>=0?'green':'red'}">$${fmt(r.pnl)}</td><td>${statusHtml}</td><td style="color:var(--red)">-${r.max_dd_pct}%</td><td style="color:var(--green)">${r.win_rate}%</td><td style="color:#60A5FA">${r.first_open_rate}%</td><td>${r.trades}</td><td><button class="btn-use" onclick="document.getElementById('interval').value='${r.interval}';useTime(${r.hour},${r.minute})">Gunakan</button></td>`;
        body.appendChild(tr);
      });
    }
  };
}
document.getElementById('btnAnalyze').addEventListener('click', showAnalyze);

// ── Optimize ──
let optSource = null;
function hideOptimize(){ document.getElementById('optimizeOverlay').style.display='none'; if(optSource){optSource.close();optSource=null;} }
function showOptimize(){
  document.getElementById('optimizeOverlay').style.display='flex';
  document.getElementById('optBody').innerHTML=''; document.getElementById('optProgFill').style.width='0%';
  document.getElementById('optPctTxt').textContent='0%'; document.getElementById('optStatusTxt').textContent='Memulai Optimasi...';
  document.getElementById('optRunningBadge').style.display='inline-block';
  
  let checkedInvs = [];
  document.querySelectorAll('.opt-inv:checked').forEach(c => checkedInvs.push(c.value));
  
  let r1=parseInt(document.getElementById('optRisk1').value), r2=parseInt(document.getElementById('optRisk2').value);
  let arrR=[]; for(let i=Math.min(r1,r2); i<=Math.max(r1,r2); i++) arrR.push(i);
  let m1=parseInt(document.getElementById('optMin1').value), m2=parseInt(document.getElementById('optMin2').value);
  let arrM=[]; for(let i=Math.min(m1,m2); i<=Math.max(m1,m2); i++) arrM.push(i);
  
  const params = new URLSearchParams({ 
    symbol: document.getElementById('symbol').value, 
    initial_eq: document.getElementById('optInitEq').value,
    spread: document.getElementById('optSpread').value,
    leverage: document.getElementById('optLeverage').value,
    opt_intervals: checkedInvs.join(','),
    opt_risks: arrR.join(','),
    opt_ranges: arrM.join(','),
    start_date: document.getElementById('startDate').value,
    end_date: document.getElementById('endDate').value
  });
  
  optSource = new EventSource('/api/optimize?'+params.toString());
  optSource.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if(d.type==='progress'){
      document.getElementById('optProgFill').style.width=d.pct+'%';
      document.getElementById('optPctTxt').textContent=d.pct+'%';
      if(d.msg) document.getElementById('optStatusTxt').textContent=d.msg;
    } else if(d.type==='result'){
      optSource.close(); optSource=null;
      document.getElementById('optRunningBadge').style.display='none';
      document.getElementById('optStatusTxt').textContent='Optimasi Selesai!';
      const body = document.getElementById('optBody');
      body.innerHTML = '';
      
      const renderRows = (arr, title) => {
        if(!arr || arr.length === 0) return;
        const hTr = document.createElement('tr');
        hTr.innerHTML = `<td colspan="8" style="background:rgba(99,102,241,0.1); font-weight:700; color:var(--accent); text-align:center; padding:0.6rem; font-size:0.75rem">${title}</td>`;
        body.appendChild(hTr);
        
        arr.forEach((r, i) => {
          const tr = document.createElement('tr');
          let statusHtml = `$${fmt(r.final_equity)}`;
          if (r.margin_call || r.final_equity <= 0) {
              statusHtml = `<span class="badge" style="background:rgba(248,113,113,0.15);color:var(--red);border:1px solid rgba(248,113,113,0.3)">MARGIN CALL</span>`;
          }
          let comboBadge = `<span class="badge" style="background:rgba(255,255,255,0.1);color:var(--accent);border:1px solid rgba(255,255,255,0.2)">${r.interval} | ${r.time} | R:${r.min_range} | %:${r.risk}</span>`;
          
          let btnHtml = r.margin_call ? '' : `<button class="btn" style="padding:4px 8px;font-size:0.65rem;background:var(--accent);color:white;width:auto;margin-top:0" onclick='applyOptimize(${JSON.stringify(r)})'>Gunakan</button>`;
          
          tr.innerHTML=`<td>#${i+1}</td><td>${comboBadge}</td><td class="${r.pnl>=0?'green':'red'}">$${fmt(r.pnl)}</td><td>${statusHtml}</td><td style="color:var(--red)">-${r.max_dd_pct}%</td><td style="color:var(--green)">${r.win_rate}%</td><td>${r.trades}</td><td>${btnHtml}</td>`;
          body.appendChild(tr);
        });
      };
      
      renderRows(d.top, "💎 TOP 100 PROFITABLE COMBINATIONS");
      renderRows(d.bottom, "⚠️ 10 PERTAMA MARGIN CALL");
    }
  };
}
document.getElementById('btnOptimize').addEventListener('click', showOptimize);

// Ensure first tab is correctly initialized on load
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.target).classList.add('active');
  });
});
function applyOptimize(r) {
  const [h, m] = r.time.split(':');
  document.getElementById('interval').value = r.interval;
  document.getElementById('btHour').value = parseInt(h);
  document.getElementById('btMin').value = parseInt(m);
  document.getElementById('btMinRange').value = r.min_range;
  document.getElementById('btRisk').value = r.risk;
  document.getElementById('btSpread').value = document.getElementById('optSpread').value;
  document.getElementById('btLeverage').value = document.getElementById('optLeverage').value;
  document.getElementById('btInitEq').value = document.getElementById('optInitEq').value;
  
  // Trigger UI state updates
  document.getElementById('btInitEq').dispatchEvent(new Event('input'));
  
  hideOptimize();
  // Switch to backtest tab
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  const target = document.querySelector('.tab-btn[data-target="tabBacktest"]');
  if(target) target.classList.add('active');
  document.getElementById('tabBacktest').classList.add('active');
  
  alert(`Setting Optimasi Diterapkan!\nInterval: ${r.interval}, Jam: ${r.time}, Min Range: ${r.min_range}, Risk: ${r.risk}%`);
}
</script>
</body>
</html>"""

@app.route('/')
def index(): return HTML

def open_browser(): time.sleep(1.2); webbrowser.open('http://localhost:5000')
if __name__ == '__main__':
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, port=5000, threaded=True)
