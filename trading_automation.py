import yaml, pytz, json, os, csv, decimal, time, threading, math, random
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import Update, ReplyKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    Application,
    CallbackContext,
    ContextTypes,
    BaseHandler,
    filters,
    JobQueue,
)
from dotenv import load_dotenv
load_dotenv()

import os

API_KEY = os.environ['BINANCE_KEY']
API_SECRET = os.environ['BINANCE_SECRET']
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = int(os.environ['TELEGRAM_CHAT_ID'])

import apscheduler.util

def patched_get_localzone():
    return pytz.UTC

apscheduler.util.get_localzone = patched_get_localzone

BASE_ASSET = 'USDC'
DUST_LIMIT = 0.4

MIN_MARKETCAP = 2_000_000  
MIN_VOLUME = 250_000     
MIN_VOLATILITY = 0.0002

MIN_1M = 0.002   # 0.2% in 1m
MIN_5M = 0.005   # 0.5% in 5m
MIN_15M = 0.01   # 1% in 15m

MAX_POSITIONS = 20
MIN_PROFIT = 1.0       # %
TRAIL_STOP = 0.6       # %
MAX_HOLD_TIME = 900    # seconds
INVEST_AMOUNT = 10     # USD per coin
TRADE_LOG_FILE = "trades_detailed.csv"
YAML_SYMBOLS_FILE = "symbols.yaml"
BOT_STATE_FILE = "bot_state.json"

client = Client(API_KEY, API_SECRET)
# --- Time Sync Patch: ---
try:
    client.get_server_time()
    print("[INFO] Synced time with Binance server.")
except Exception as e:
    print(f"[ERROR] Could not sync time with Binance server: {e}")

positions = {}        # single global positions dict
balance = {'usd': 0.0}
trade_log = []

import time

def parse_trade_time(val, default=None):
    """Parse trade_time as float (Unix timestamp) or from string like 'YYYY-MM-DD HH:MM:SS'."""
    if val is None:
        return default if default is not None else time.time()
    try:
        return float(val)
    except Exception:
        try:
            return time.mktime(datetime.strptime(val, "%Y-%m-%d %H:%M:%S").timetuple())
        except Exception:
            return default if default is not None else time.time()


async def send_with_keyboard(update: Update, text, parse_mode=None, reply_markup=None):
    if reply_markup is None:
        reply_markup = ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

def load_trade_history():
    log = []
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE, "r") as f:
                reader = csv.DictReader(f)
                log = list(reader)
        except Exception as e:
            print(f"[LOAD TRADE ERROR] {e}")
    return log

def rebuild_cost_basis(trade_log):
    positions_tmp = {}
    for tr in trade_log:
        symbol = tr.get('Symbol')
        qty = float(tr.get('Qty', 0))
        entry = float(tr.get('Entry', 0))
        action = 'buy' if float(tr.get('Entry', 0)) > 0 else 'sell'
        action = tr.get('action', '').lower() if 'action' in tr else (action)
        tstamp = parse_trade_time(tr.get('Time'), time.time())
        if symbol not in positions_tmp:
            positions_tmp[symbol] = {'qty': 0.0, 'cost': 0.0, 'trade_time': tstamp}
        if action == 'buy':
            positions_tmp[symbol]['qty'] += qty
            positions_tmp[symbol]['cost'] += qty * entry
            positions_tmp[symbol]['trade_time'] = tstamp
        elif action == 'sell':
            orig_qty = positions_tmp[symbol]['qty']
            if orig_qty > 0 and qty > 0:
                avg_entry = positions_tmp[symbol]['cost'] / orig_qty
                positions_tmp[symbol]['qty'] -= qty
                positions_tmp[symbol]['cost'] -= qty * avg_entry
                positions_tmp[symbol]['trade_time'] = tstamp
                if positions_tmp[symbol]['qty'] < 1e-8:
                    positions_tmp[symbol]['qty'] = 0
                    positions_tmp[symbol]['cost'] = 0
    cost_basis = {}
    for symbol, v in positions_tmp.items():
        if v['qty'] > 0:
            avg_entry = v['cost'] / v['qty'] if v['qty'] > 0 else 0.0
            cost_basis[symbol] = {
                'qty': v['qty'],
                'entry': avg_entry,
                'trade_time': v['trade_time'],  # now always float
            }
    return cost_basis


def reconcile_positions_with_binance(client, positions, quote_asset="USDC"):
    """Update local 'positions' to match true Binance balances for all open positions."""
    try:
        account = client.get_account()
        assets = {b['asset']: float(b['free']) for b in account['balances'] if float(b['free']) > 0}
        for asset, qty in assets.items():
            if asset == quote_asset:
                continue
            symbol = asset + quote_asset
            if symbol in positions:
                positions[symbol]['qty'] = qty
            else:
                positions[symbol] = {'qty': qty, 'entry': 0.0, 'trade_time': 0}
        for symbol in list(positions):
            base = symbol.replace(quote_asset, "")
            if base not in assets or assets[base] == 0:
                positions.pop(symbol)
    except Exception as e:
        print(f"[SYNC ERROR] Failed to reconcile with Binance: {e}")

def fetch_usdc_balance():
    """Update the global USDC balance from Binance live."""
    try:
        asset_info = client.get_asset_balance(asset="USDC")
        free = float(asset_info['free'])
        balance['usd'] = free
        print(f"[DEBUG] Live USDC balance: {free}")
    except Exception as e:
        print(f"[ERROR] Fetching USDC balance: {e}")
        balance['usd'] = 0

def get_latest_price(symbol):
    try:
        return float(client.get_symbol_ticker(symbol=symbol)["price"])
    except Exception as e:
        print(f"[PRICE ERROR] {symbol}: {e}")
        return None

def min_notional_for(symbol):
    try:
        info = client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'MIN_NOTIONAL':
                return float(f['notional'])
        return 10.0
    except Exception:
        return 10.0

def lot_step_size_for(symbol):
    try:
        info = client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size = float(f['stepSize'])
                min_qty = float(f['minQty'])
                return step_size, min_qty
    except Exception:
        pass
    return 0.000001, 0.000001

def round_qty(symbol, qty):
    """Binance-compliant rounding for quantity."""
    info = client.get_symbol_info(symbol)
    step_size = None
    for f in info['filters']:
        if f['filterType'] == 'LOT_SIZE':
            step_size = float(f['stepSize'])
            break
    if step_size is None:
        return qty
    d_qty = decimal.Decimal(str(qty))
    d_step = decimal.Decimal(str(step_size))
    rounded_qty = float((d_qty // d_step) * d_step)
    if rounded_qty < step_size:
        return 0
    return rounded_qty

def get_sellable_positions():
    """Return {symbol: pos} for sellable positions (passes step and notional filters)."""
    out = {}
    for symbol, pos in positions.items():
        qty = pos.get("qty", 0)
        sell_qty = round_qty(symbol, qty)
        current_price = get_latest_price(symbol)
        value = current_price * sell_qty
        if sell_qty == 0 or qty == 0:
            # print(f"[SKIP] {symbol}: Qty after rounding is 0. Skipping sell for now.")
            continue
        if value < DUST_LIMIT:
            print(f"[SKIP] {symbol}: Value after rounding is ${value:.2f} (below DUST_LIMIT ${DUST_LIMIT}), skipping sell.")
            continue
        try:
            min_notional = min_notional_for(symbol)
            current_price = get_latest_price(symbol)
            if current_price is None:
                print(f"[SKIP] {symbol}: Could not fetch price (None), skipping auto-sell logic.")
                continue
            if sell_qty == 0:
                continue
            if sell_qty * current_price < min_notional:
                continue
            out[symbol] = pos
        except Exception:
            continue
    return out

def get_portfolio_lines(positions, get_latest_price, dust_limit=1.0):
    lines = []
    for symbol, pos in positions.items():
        qty = pos['qty']
        entry = pos['entry']
        try:
            current_price = get_latest_price(symbol)
            if current_price is None:
                print(f"[SKIP] {symbol}: Could not fetch price (None), skipping auto-sell logic.")
                continue
        except Exception:
            current_price = entry
        value = qty * current_price
        if value < dust_limit:
            continue
        pnl_pct = ((current_price - entry) / entry * 100) if entry else 0
        lines.append((symbol, qty, entry, current_price, value, pnl_pct))
    return lines

def display_portfolio(positions, get_latest_price, dust_limit=1.0):
    print(f"\nCurrent Portfolio (positions over {dust_limit}€):")
    lines = get_portfolio_lines(positions, get_latest_price, dust_limit)
    if not lines:
        print(f"  (No positions over {dust_limit}€)")
        return
    for symbol, qty, entry, price, value, pnl_pct in lines:
        print(f"  {symbol:<12} qty={qty:.6f} entry={entry:.4f} now={price:.4f} value={value:.2f}€ PnL={pnl_pct:+.2f}%")

def format_investments_message(positions, get_latest_price, dust_limit=1.0):
    lines = get_portfolio_lines(positions, get_latest_price, dust_limit)
    if not lines:
        return f"(No investments over {dust_limit}€)"
    msg = "Current Investments:"
    for symbol, qty, entry, price, value, pnl_pct in lines:
        msg += (
            f"\n\n{symbol}: Qty {qty:.4f} @ {entry:.5f} → {price:.5f} | Value ${value:.2f} | PnL {pnl_pct:+.2f}%"
        )
    return msg

async def telegram_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        await send_with_keyboard(update, "Access Denied.")
        return

    text = update.message.text
    sync_positions_with_binance(client, positions)

    if text == "📊 Balance":
        fetch_usdc_balance()

        usdc = balance['usd']
        total_invested = 0.0
        invested_details = []

        for s, p in positions.items():
            price = get_latest_price(s)
            if price is None:
                continue
            qty = float(p['qty'])
            value = price * qty
            # Only show if value is above DUST_LIMIT
            if value > DUST_LIMIT:
                total_invested += value
                invested_details.append(f"{s}: {qty:.6f} @ ${price:.2f} = ${value:.2f}")

        msg = (
            f"USDC Balance: **${usdc:.2f}**\n"
            f"Total Invested: **${total_invested:.2f}**\n"
            f"Portfolio Value: **${usdc + total_invested:.2f} USDC**"
        )

        if invested_details:
            msg += "\n\n*Investments:*"
            for line in invested_details:
                msg += f"\n- {line}"

        await send_with_keyboard(update, msg, parse_mode='Markdown')


    
    elif text == "💼 Investments":
        msg = format_investments_message(positions, get_latest_price, DUST_LIMIT)
        await send_with_keyboard(update, msg)
    
    elif text == "⏸ Pause Trading":
        set_paused(True)
        await send_with_keyboard(update, "⏸ Trading is now *paused*. Bot will not auto-invest or auto-sell until resumed.", parse_mode='Markdown')

    elif text == "▶️ Resume Trading":
        set_paused(False)
        await send_with_keyboard(update, "▶️ Trading is *resumed*. Bot will continue auto-investing and auto-selling.", parse_mode='Markdown')

    elif text == "📝 Trade Log":
        log = trade_log
        if not log:
            await send_with_keyboard(update, "No trades yet.")
        else:
            msg = (
                "Time                 Symbol       Entry      Exit       Qty        PnL($)\n"
                "-----------------------------------------------------------------------\n"
            )
            for tr in log[-10:]:
                try:
                    entry = float(tr.get('Entry', 0))
                    exit_ = float(tr.get('Exit', 0))
                    qty = float(tr.get('Qty', 0))
                    pnl = float(tr.get('PnL $', 0))
                    msg += (
                        f"{tr['Time'][:16]:<19} "
                        f"{tr['Symbol']:<11} "
                        f"{entry:<9.4f} "
                        f"{exit_:<9.4f} "
                        f"{qty:<9.5f} "
                        f"{pnl:<8.2f}\n"
                    )
                except (ValueError, KeyError) as e:
                    print(f"[WARN] Bad trade log row: {tr} ({e})")
                    continue
            await send_with_keyboard(update, f"```{msg}```", parse_mode='Markdown')
    else:
        await send_with_keyboard(update, "Unknown action.")

def sync_positions_with_binance(client, positions, quote_asset="USDC"):
    """Keeps local positions up-to-date with live Binance balances."""
    try:
        account = client.get_account()
        assets = {b['asset']: float(b['free']) for b in account['balances'] if float(b['free']) > 0}
        updated = set()
        for asset, qty in assets.items():
            if asset == quote_asset:
                continue
            symbol = asset + quote_asset
            if symbol in positions:
                positions[symbol]['qty'] = qty
                updated.add(symbol)
            elif qty > 0:
                positions[symbol] = {'qty': qty, 'entry': 0.0, 'trade_time': 0}
                updated.add(symbol)
        for symbol in list(positions):
            base = symbol.replace(quote_asset, "")
            if symbol not in updated and base in assets:
                if assets[base] == 0:
                    positions.pop(symbol, None)
            elif symbol not in updated and base not in assets:
                positions.pop(symbol, None)
    except Exception as e:
        print(f"[SYNC ERROR] Failed to sync positions with Binance: {e}")

def set_paused(paused: bool):
    state = get_bot_state()
    state["paused"] = paused
    save_bot_state(state)

def is_paused():
    state = get_bot_state()
    return state.get("paused", False)

main_keyboard = [
    ["📊 Balance", "💼 Investments"],
    ["⏸ Pause Trading", "▶️ Resume Trading"],
    ["📝 Trade Log"]
]

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_with_keyboard(
        update,
        "Welcome! Use the buttons below\n",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    )

def telegram_main():
    application = ApplicationBuilder() \
        .token(TELEGRAM_TOKEN) \
        .build()

    application.add_handler(CommandHandler('start', start_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_handle_message))

    application.run_polling()


def quote_precision_for(symbol):
    try:
        info = client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step = str(f['stepSize'])
                if '.' in step:
                    return len(step.split('.')[1].rstrip('0'))
        return 2
    except Exception:
        return 2

def buy(symbol, amount=None):
    print(f"[DEBUG] Actual USDC balance before buy: {balance['usd']}")
    try:
        precision = quote_precision_for(symbol)
        trade_amount = round(amount, precision)
        min_notional = min_notional_for(symbol)
        print(f"[DEBUG] Attempting to buy {symbol}: trade_amount=${trade_amount}, min_notional=${min_notional}")
        if trade_amount < min_notional:
            print(f"[SKIP] {symbol}: Trade amount (${trade_amount}) < MIN_NOTIONAL (${min_notional})")
            return None
        # Let Binance handle rounding/fees
        order = client.order_market_buy(symbol=symbol, quoteOrderQty=trade_amount)
        price = float(order['fills'][0]['price'])
        qty = float(order['executedQty'])
        qty = round_qty(symbol, qty)
        print(f"[INFO] Bought {symbol}: qty={qty}, price={price}")
        balance['usd'] -= trade_amount
        positions[symbol] = {
            'entry': price,
            'qty': qty,
            'timestamp': time.time(),
            'trade_time': time.time()
        }
        print(f"[DEBUG] Actual USDC balance after buy attempt: {balance['usd']}")
        return positions[symbol]
    except BinanceAPIException as e:
        print(f"[BUY ERROR] {symbol}: {e}")
        return None

def sell(symbol, qty):
    try:
        sell_qty = round_qty(symbol, qty)
        current_price = get_latest_price(symbol)
        value = current_price * sell_qty
        if sell_qty == 0 or qty == 0:
            # print(f"[SKIP] {symbol}: Qty after rounding is 0. Skipping sell for now.")
            return None, 0, 0
        if value < DUST_LIMIT:
            print(f"[SKIP] {symbol}: Value after rounding is ${value:.2f} (below DUST_LIMIT ${DUST_LIMIT}), skipping sell.")
            return None, 0, 0
        
        order = client.order_market_sell(symbol=symbol, quantity=sell_qty)
        price = float(order['fills'][0]['price'])
        fee = sum(float(f['commission']) for f in order['fills']) if "fills" in order else 0
        return price, fee, 0
    except BinanceAPIException as e:
        print(f"[SELL ERROR] {symbol}: {e}")
        # Do not remove from positions here.
        return None, 0, 0

def estimate_trade_tax(entry_price, exit_price, qty, trade_time, exit_time):
    """
    Estimates the tax for a given trade based on holding period and gain.
    Uses 40% for short-term (<24h), 25% for long-term (>=24h).
    """
    trade_time = parse_trade_time(trade_time, time.time())
    exit_time = parse_trade_time(exit_time, time.time())
    trade_time_float = parse_trade_time(trade_time, time.time())
    exit_time_float = parse_trade_time(exit_time, time.time())
    holding_period = exit_time_float - trade_time_float
    profit = (exit_price - entry_price) * qty
    short_term_rate = 0.40
    long_term_rate = 0.25
    if holding_period < 24 * 3600:
        rate = short_term_rate
    else:
        rate = long_term_rate
    tax = profit * rate if profit > 0 else 0
    return tax


def log_trade(symbol, entry, exit_price, qty, trade_time, exit_time, fees=0, tax=0, action="sell"):
    pnl = (exit_price - entry) * qty if action == "sell" else 0
    pnl_pct = ((exit_price - entry) / entry * 100) if action == "sell" and entry != 0 else 0
    duration_sec = int(
    parse_trade_time(exit_time, time.time()) - parse_trade_time(trade_time, time.time())
    )
    trade = {
        'Time': datetime.fromtimestamp(trade_time).strftime("%Y-%m-%d %H:%M:%S"),
        'Action': action,
        'Symbol': symbol,
        'Entry': round(entry, 8),
        'Exit': round(exit_price, 8),
        'Qty': round(qty, 8),
        'PnL $': round(pnl, 8),
        'PnL %': round(pnl_pct, 3),
        'Duration (s)': duration_sec,
        'Fees': round(fees, 8),
        'Tax': round(tax, 8)
    }
    try:
        file_exists = os.path.isfile(TRADE_LOG_FILE)
        with open(TRADE_LOG_FILE, "a", newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(trade.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(trade)
    except Exception as e:
        print(f"[LOG ERROR] {e}")

def auto_sell_momentum_positions(min_profit=MIN_PROFIT, trailing_stop=TRAIL_STOP, max_hold_time=MAX_HOLD_TIME):
    now = time.time()
    for symbol, pos in list(positions.items()):
        try:
            entry = float(pos['entry'])
            qty = float(pos['qty'])
            trade_time = parse_trade_time(pos.get('trade_time', now), now)

            current_price = get_latest_price(symbol)
            if current_price is None:
                print(f"[SKIP] {symbol}: Could not fetch price (None), skipping auto-sell logic.")
                continue

            sell_qty = round_qty(symbol, qty)
            value = current_price * sell_qty
            if sell_qty == 0 or qty == 0:
                # print(f"[SKIP] {symbol}: Qty after rounding is 0. Skipping sell for now.")
                continue
            if value < DUST_LIMIT:
                print(f"[SKIP] {symbol}: Value after rounding is ${value:.2f} (below DUST_LIMIT ${DUST_LIMIT}), skipping sell.")
                continue

            pnl_pct = ((current_price - entry) / entry * 100) if entry else 0
            held_for = now - trade_time

            if 'max_price' not in pos:
                pos['max_price'] = entry
            pos['max_price'] = max(pos['max_price'], current_price)
            trail_pct = (current_price - pos['max_price']) / pos['max_price'] * 100

            should_sell = False
            reason = ""
            if pnl_pct >= min_profit and trail_pct <= -trailing_stop:
                should_sell = True
                reason = f"Trailing stop: profit {pnl_pct:.2f}%, now {trail_pct:.2f}% from high."
            elif held_for >= max_hold_time:
                should_sell = True
                reason = f"Timed exit after {held_for/60:.1f} minutes."

            if should_sell:
                exit_price, fee, _ = sell(symbol, qty)
                exit_time = time.time()
                tax = estimate_trade_tax(entry, exit_price, qty, trade_time, exit_time)
                log_trade(
                    symbol=symbol,
                    entry=entry,
                    exit_price=exit_price,
                    qty=qty,
                    trade_time=trade_time,
                    exit_time=exit_time,
                    fees=fee,
                    tax=tax,
                    action="sell"
                )
                print(f"[MOMENTUM SELL] {symbol}: {reason}")
                del positions[symbol]
        except Exception as e:
            print(f"[AUTO-SELL ERROR] {symbol}: {e}")



def get_1h_percent_change(symbol):
    '''Get last 2 hourly candles'''
    klines = client.get_klines(symbol=symbol, interval='1h', limit=2)
    if len(klines) < 2:
        return 0
    prev_close = float(klines[0][4])
    last_close = float(klines[1][4])
    return (last_close - prev_close) / prev_close * 100

def market_is_risky():
    '''For example, check if BTCUSDT 1h change is negative or > X% move'''
    btc_change_1h = get_1h_percent_change("BTCUSDT")
    return btc_change_1h < -1 or abs(btc_change_1h) > 3

def sync_investments_with_binance():
    try:
        account_info = client.get_account()
        # Keep ALL nonzero assets except base asset (USDC)
        balances = {
            a["asset"]: float(a["free"])
            for a in account_info["balances"]
            if float(a["free"]) > 0.0001 and a["asset"] != BASE_ASSET
        }
        new_positions = {}
        for asset, amount in balances.items():
            symbol = f"{asset}{BASE_ASSET}"
            try:
                price = float(client.get_symbol_ticker(symbol=symbol)["price"])
                new_positions[symbol] = {
                    "entry": price,  # For new syncs, "entry" is current price, unless you have historical
                    "qty": amount,
                    "timestamp": time.time(),
                    "trade_time": time.time()
                }
            except Exception:
                continue
        positions.clear()
        positions.update(new_positions)
        print("[INFO] Synced investments with real Binance balances.")
    except Exception as e:
        print(f"[SYNC ERROR] Could not sync investments with Binance: {e}")

def too_many_positions():
    count = 0
    for symbol, pos in positions.items():
        price = get_latest_price(symbol)
        if price is None:
            continue
        if pos.get('qty', 0) * price > DUST_LIMIT:
            count += 1
    return count >= MAX_POSITIONS


def reserve_taxes_and_reinvest():
    """
    Reserve USDC for taxes by selling just enough, from as many profitable (non-core, long-term) positions as needed.
    - Sells non-core first, then core if needed.
    - Avoids short-term gains unless required.
    - Uses real min_notional for every symbol.
    """
    # Helper: core coin detection
    def is_core(symbol):
        stats = load_symbol_stats()
        info = stats.get(symbol, {})
        return info.get("core", False)

    # Helper: short-term check
    SHORT_TERM_SECONDS = 24 * 3600  # 24 hours, adjust if needed
    def is_short_term(pos):
        trade_time = parse_trade_time(pos.get('trade_time', time.time()), time.time())
        held_for = time.time() - trade_time
        return held_for < SHORT_TERM_SECONDS


    # Helper: profit calculation
    def position_profit(sym):
        pos = positions[sym]
        cur_price = get_latest_price(sym)
        entry = pos['entry']
        return (cur_price - entry) * pos['qty']

    # 1. Calculate taxes owed from recent closed trades
    total_taxes_owed = sum(
        float(tr.get('Tax', 0)) for tr in trade_log[-20:] if float(tr.get('Tax', 0)) > 0
    )

    fetch_usdc_balance()
    free_usdc = balance['usd']

    # 2. Sell as little as needed from as many positions as needed
    while True:
        # Calculate how much USDC we still need to invest after tax reserve
        needed_usdc = 0

        # We'll use the minimum min_notional for ALL symbols in momentum (safe fallback)
        momentum_symbols = get_yaml_ranked_momentum(limit=3)
        min_notional = min([min_notional_for(sym) for sym in momentum_symbols] + [10.0])

        needed_usdc = (min_notional + total_taxes_owed) - free_usdc
        if needed_usdc <= 0:
            break  # We have enough, done selling

        # Step 1: Try non-core, profitable, long-term positions
        candidates = [
            sym for sym in positions
            if position_profit(sym) > 0
            and round_qty(sym, positions[sym]['qty']) > 0
            and not is_core(sym)
            and not is_short_term(positions[sym])
        ]
        # Step 2: If none, try core, profitable, long-term positions
        if not candidates:
            candidates = [
                sym for sym in positions
                if position_profit(sym) > 0
                and round_qty(sym, positions[sym]['qty']) > 0
                and is_core(sym)
                and not is_short_term(positions[sym])
            ]
        # Step 3: If still none, allow non-core, profitable, short-term positions
        if not candidates:
            candidates = [
                sym for sym in positions
                if position_profit(sym) > 0
                and round_qty(sym, positions[sym]['qty']) > 0
                and not is_core(sym)
            ]
        # Step 4: Last resort, allow core, profitable, short-term positions
        if not candidates:
            candidates = [
                sym for sym in positions
                if position_profit(sym) > 0
                and round_qty(sym, positions[sym]['qty']) > 0
            ]
        # If still none, give up
        if not candidates:
            print("[TAXES] No profitable positions to sell for taxes. Waiting to accumulate more USDC.")
            break

        # Sort: non-core first, lowest profit first (to avoid selling strong winners)
        candidates = sorted(
            candidates,
            key=lambda sym: (is_core(sym), position_profit(sym))
        )

        # Sell just enough from one position
        symbol_to_sell = candidates[0]
        pos = positions[symbol_to_sell]
        cur_price = get_latest_price(symbol_to_sell)
        entry = pos['entry']
        qty_available = pos['qty']
        trade_time = pos.get('trade_time', time.time())
        min_notional_this = min_notional_for(symbol_to_sell)

        # How much do we need from this position (in qty)?
        fetch_usdc_balance()
        free_usdc = balance['usd']
        needed_usdc = (min_notional + total_taxes_owed) - free_usdc
        qty_to_sell = min(qty_available, max(needed_usdc / cur_price, min_notional_this / cur_price))
        qty_to_sell = round_qty(symbol_to_sell, qty_to_sell)

        if qty_to_sell == 0:
            print(f"[SKIP] {symbol_to_sell}: Qty after rounding is 0. Skipping this position for now.")
            del positions[symbol_to_sell]
            continue

        print(
            f"[TAXES] Selling {qty_to_sell:.6f} {symbol_to_sell} "
            f"(profit: {position_profit(symbol_to_sell):.2f}, core: {is_core(symbol_to_sell)}, short-term: {is_short_term(pos)}) "
            f"to free up USDC for taxes."
        )

        exit_price, fee, tax = sell(symbol_to_sell, qty_to_sell)

        if exit_price is None:
            print(f"[SKIP] {symbol_to_sell}: Sell returned None. Skipping this position for now.")
            del positions[symbol_to_sell]
            continue

        exit_time = time.time()
        log_trade(symbol_to_sell, entry, exit_price, qty_to_sell, trade_time, exit_time, fee, tax, action="sell")

        # Update or remove position
        if qty_to_sell == qty_available:
            del positions[symbol_to_sell]
        else:
            positions[symbol_to_sell]['qty'] -= qty_to_sell

        fetch_usdc_balance()
        free_usdc = balance['usd']

    # Final check: Only invest with what's left after reserving for taxes
    investable_usdc = free_usdc - total_taxes_owed
    if investable_usdc < min_notional:
        print("[TAXES] Not enough USDC to invest after reserving for taxes.")
        return

    invest_momentum_with_usdc_limit(investable_usdc)

def load_symbol_stats():
    try:
        with open(YAML_SYMBOLS_FILE, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[YAML ERROR] Could not read {YAML_SYMBOLS_FILE}: {e}")
        return {}
    
def pct_change(klines):
    if len(klines) < 2: return 0
    prev_close = float(klines[0][4])
    last_close = float(klines[1][4])
    return (last_close - prev_close) / prev_close * 100

def has_recent_momentum(symbol, min_1m=MIN_1M, min_5m=MIN_5M, min_15m=MIN_15M):
    try:
        klines_1m = client.get_klines(symbol=symbol, interval='1m', limit=2)
        klines_5m = client.get_klines(symbol=symbol, interval='5m', limit=2)
        klines_15m = client.get_klines(symbol=symbol, interval='15m', limit=2)
        print(f"[DEBUG] {symbol}: 1m={pct_change(klines_1m):.4f} 5m={pct_change(klines_5m):.4f} 15m={pct_change(klines_15m):.4f}")
        return (
            pct_change(klines_1m) > min_1m and
            pct_change(klines_5m) > min_5m and
            pct_change(klines_15m) > min_15m
        )
    except Exception:
        return False

def get_yaml_ranked_momentum(
        limit=MAX_POSITIONS, 
        min_marketcap=MIN_MARKETCAP, 
        min_volume=MIN_VOLUME, 
        min_volatility=MIN_VOLATILITY):
    stats = load_symbol_stats()
    if not stats:
        return []
    tickers = {t['symbol']: t for t in client.get_ticker() if t['symbol'] in stats}
    candidates = []
    for symbol, s in stats.items():
        ticker = tickers.get(symbol)
        if not ticker:
            print(f"[SKIP] {symbol}: Not in live ticker data")
            continue
        mc = s.get("market_cap", 0) or 0
        vol = s.get("volume_1d", 0) or 0
        vola = s.get("volatility", {}).get("1d", 0) or 0
        price_change = float(ticker.get('priceChangePercent', 0))
        if mc < min_marketcap:
            print(f"[SKIP] {symbol}: market_cap {mc} < min {min_marketcap}")
            continue
        if vol < min_volume:
            print(f"[SKIP] {symbol}: volume {vol} < min {min_volume}")
            continue
        if vola < min_volatility:
            print(f"[SKIP] {symbol}: volatility {vola} < min {min_volatility}")
            continue
        if not has_recent_momentum(symbol):
            print(f"[SKIP] {symbol}: has no recent momentum")
            continue
        
        # Calculate the momentum score (sum of recent % changes)
        k1m = client.get_klines(symbol=symbol, interval='1m', limit=2)
        k5m = client.get_klines(symbol=symbol, interval='5m', limit=2)
        k15m = client.get_klines(symbol=symbol, interval='15m', limit=2)
        k1h = client.get_klines(symbol=symbol, interval='1h', limit=2)
        momentum_score = (
            pct_change(k1m)
            + pct_change(k5m) * 1.5
            + pct_change(k15m) * 2
            + pct_change(k1h)
        )
        candidates.append({
            "symbol": symbol,
            "market_cap": mc,
            "volume": vol,
            "volatility": vola,
            "price_change": price_change,
            "momentum_score": momentum_score,
        })

    ranked = sorted(
        candidates, 
        key=lambda x: (x["momentum_score"], x["market_cap"], x["volume"]), 
        reverse=True
    )
    return [x["symbol"] for x in ranked[:limit]]

def refresh_symbols():
    global SYMBOLS
    SYMBOLS = get_yaml_ranked_momentum(limit=10)

def invest_momentum_with_usdc_limit(usdc_limit):
    """
    Invest in as many eligible momentum symbols as possible, always using the min_notional per symbol,
    never all-or-nothing. Any remaining funds are left in USDC.
    This version treats coins you own (including dust) and coins you don't equally.
    """
    refresh_symbols()
    symbols = get_yaml_ranked_momentum(limit=10)
    print(f"[DEBUG] Momentum symbols eligible for investment: {symbols}")
    if not symbols:
        print("[DIAGNOSE] No symbols passed the momentum and filter criteria.")
        return
    if usdc_limit < 1:
        print("[INFO] Insufficient funds.")
        return

    min_notionals = []
    for symbol in symbols:
        min_notional = min_notional_for(symbol)
        min_notionals.append((symbol, min_notional))

    total_spent = 0
    symbols_to_buy = []
    for symbol, min_notional in sorted(min_notionals, key=lambda x: -x[1]):  # Buy more expensive coins first
        if usdc_limit - total_spent >= min_notional:
            symbols_to_buy.append((symbol, min_notional))
            total_spent += min_notional

    if not symbols_to_buy:
        print(f"[INFO] Not enough USDC to invest in any eligible symbol. Minimum needed: {min([mn for s, mn in min_notionals]):.2f} USDC.")
        return

    for symbol, min_notional in symbols_to_buy:
        amount = min_notional
        fetch_usdc_balance()
        if balance['usd'] < amount:
            print(f"[INFO] Out of funds before buying {symbol}.")
            break
        print(f"[INFO] Attempting to buy {symbol} with ${amount:.2f}")
        result = buy(symbol, amount=amount)
        if not result:
            print(f"[BUY ERROR] {symbol}: Buy failed, refreshing USDC balance and skipping.")
            fetch_usdc_balance()
        else:
            print(f"[INFO] Bought {symbol} for ${amount:.2f}")


def get_bot_state():
    if not os.path.exists(BOT_STATE_FILE):
        return {"balance": 0, "positions": {}, "paused": True, "log": [], "actions": []}
    with open(BOT_STATE_FILE, "r") as f:
        return json.load(f)

def save_bot_state(state):
    with open(BOT_STATE_FILE, "w") as f:
        json.dump(state, f)

def sync_state():
    state = get_bot_state()
    state["balance"] = balance['usd']
    state["positions"] = positions
    state["log"] = trade_log[-100:]
    save_bot_state(state)

def process_actions():
    state = get_bot_state()
    actions = state.get("actions", [])
    performed = []
    state["actions"] = [a for a in actions if a not in performed]
    save_bot_state(state)

def trading_loop():
    last_sync = time.time()
    SYNC_INTERVAL = 180

    while True:
        try:
            if market_is_risky():
                print("[INFO] Market too volatile. Skipping investing this round.")
                time.sleep(SYNC_INTERVAL)
                continue

            if is_paused():
                print("[INFO] Bot is paused. Skipping trading logic.")
                time.sleep(SYNC_INTERVAL)
                continue

            fetch_usdc_balance()
            auto_sell_momentum_positions()  # <<< use new sell logic

            if time.time() - last_sync > SYNC_INTERVAL:
                sync_investments_with_binance()
                last_sync = time.time()

            if not too_many_positions():
                reserve_taxes_and_reinvest()  # <<<< THIS IS THE NEW LOGIC

            sync_state()
            process_actions()
            fetch_usdc_balance()
        except Exception as e:
            print(f"[LOOP ERROR] {e}")

        sync_positions_with_binance(client, positions)
        display_portfolio(positions, get_latest_price)
        time.sleep(SYNC_INTERVAL)

def resume_positions_from_binance():
    try:
        account_info = client.get_account()
        balances = {
            a["asset"]: float(a["free"])
            for a in account_info["balances"]
            if float(a["free"]) > 0.0001 and a["asset"] != BASE_ASSET
        }
        resumed = {}
        for asset, amount in balances.items():
            symbol = f"{asset}{BASE_ASSET}"
            try:
                price = float(client.get_symbol_ticker(symbol=symbol)["price"])
                resumed[symbol] = {
                    "entry": price,
                    "qty": amount,
                    "timestamp": time.time(),
                    "trade_time": time.time()
                }
            except Exception:
                continue
        return resumed
    except Exception:
        return {}

if __name__ == "__main__":
    refresh_symbols()
    trade_log = load_trade_history()
    positions.clear()
    positions.update(rebuild_cost_basis(trade_log))
    reconcile_positions_with_binance(client, positions)
    print(f"[INFO] Bot paused state on startup: {is_paused()}")
    try:
        trading_thread = threading.Thread(target=trading_loop, daemon=True)
        trading_thread.start()
        telegram_main()  # This blocks; run in main thread for proper Ctrl+C
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down gracefully...")
    finally:
        print("[INFO] Goodbye!")
