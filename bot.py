import pybithumb
import time
import uuid
import os
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CallbackQueryHandler

# ================= 설정 =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

SCAN_INTERVAL = 60
POSITION_CHECK_INTERVAL = 10

FIXED_ENTRY_KRW = 11000
MIN_ORDER = 5000

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

active_positions = {}
button_data_store = {}

# ================= 유틸 =================
def r(x, n=4):
    try:
        return round(float(x), n)
    except:
        return 0.0

def fmt(x):
    try:
        return f"{float(x):.6f}"
    except:
        return "0.000000"

# ================= 안전 조회 =================
def get_price(ticker):
    try:
        return float(pybithumb.get_current_price(ticker))
    except:
        return -1

def get_ohlcv(ticker):
    try:
        return pybithumb.get_ohlcv(ticker)
    except:
        return None

# ================= 지표 =================
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# ================= 방패 =================
def detect_bad_flow(df):
    recent = df.tail(3)
    closes = list(recent["close"])
    return closes[2] < closes[1] < closes[0]

def pre_buy_check(ticker, coin):
    price = get_price(ticker)

    if price < coin["support"]:
        return False, "지지선 아래로 내려가서 위험한 자리야"

    if price > coin["entry"] * 1.015:
        return False, "이미 많이 올라서 늦은 자리야"

    if price < coin["entry"] * 0.985:
        return False, "추천 때보다 많이 내려서 흐름이 깨졌어"

    return True, "OK"

# ================= 분석 =================
def analyze_coin(ticker):
    df = get_ohlcv(ticker)
    if df is None or len(df) < 20:
        return None

    price = get_price(ticker)
    if price <= 0:
        return None

    recent = df.tail(20)

    support = float(recent["low"].min())
    resistance = float(recent["high"].max())

    ma5 = df["close"].rolling(5).mean().iloc[-1]
    ma10 = df["close"].rolling(10).mean().iloc[-1]

    rsi = calculate_rsi(df).iloc[-1]

    vol = recent["volume"].iloc[-1]
    avg_vol = recent["volume"].mean()
    vol_ratio = vol / avg_vol if avg_vol > 0 else 0

    score = 0
    entry_score = 0

    if support * 0.97 <= price <= support * 1.03:
        score += 2

    if ma5 > ma10:
        score += 1
        entry_score += 1

    if 30 <= rsi <= 60:
        score += 1

    if vol_ratio >= 0.8:
        score += 1
        entry_score += 1
    else:
        entry_score -= 1

    if detect_bad_flow(df):
        entry_score -= 2

    entry = support * 1.01
    stop = support * 0.97
    tp = entry * 1.025

    qty = int(FIXED_ENTRY_KRW / entry)

    if score >= 3 and entry_score >= 1:
        status = "진입 가능"
    else:
        status = "관망"

    return {
        "ticker": ticker,
        "price": r(price),
        "entry": r(entry),
        "stop": r(stop),
        "tp": r(tp),
        "qty": qty,
        "status": status,
        "support": r(support)
    }

# ================= 텔레그램 =================
def send(msg, keyboard=None):
    try:
        bot.send_message(chat_id=CHAT_ID, text=msg, reply_markup=keyboard)
    except Exception as e:
        print(f"[텔레그램 오류] {e}")

# ================= 추천 =================
def scan():
    if active_positions:
        return

    tickers = pybithumb.get_tickers()
    candidates = []

    for t in tickers[:30]:
        coin = analyze_coin(t)
        if coin and coin["status"] == "진입 가능":
            candidates.append(coin)

    if not candidates:
        print("조건 맞는 코인 없음")
        return

    coin = candidates[0]

    bid = str(uuid.uuid4())[:8]
    button_data_store[bid] = coin

    keyboard = [[InlineKeyboardButton("🔥 매수", callback_data=f"BUY|{bid}")]]

    msg = f"""
🔥 {coin['ticker']}

지금 들어가도 되는 자리야

현재가: {fmt(coin['price'])}
진입가: {fmt(coin['entry'])}
손절가: {fmt(coin['stop'])}
목표가: {fmt(coin['tp'])}

👉 짧게 먹고 빠지는 전략
"""

    send(msg, InlineKeyboardMarkup(keyboard))

# ================= 매수 =================
def handle(update, context):
    query = update.callback_query
    query.answer()

    _, bid = query.data.split("|")
    coin = button_data_store.get(bid)

    if not coin:
        send("이미 만료된 신호야")
        return

    ticker = coin["ticker"]

    ok, msg = pre_buy_check(ticker, coin)
    if not ok:
        send(f"❌ 매수 취소\n{msg}")
        return

    try:
        bithumb.buy_market_order(ticker, coin["qty"])
        active_positions[ticker] = coin
        send(f"✅ 매수 완료\n{ticker}")
    except Exception as e:
        send(f"❌ 매수 실패\n{e}")

# ================= 감시 =================
def monitor():
    remove = []

    for ticker, coin in active_positions.items():
        price = get_price(ticker)

        if price <= coin["stop"]:
            bithumb.sell_market_order(ticker, coin["qty"])
            send(f"🚨 손절\n{ticker}")
            remove.append(ticker)

        elif price >= coin["tp"]:
            bithumb.sell_market_order(ticker, coin["qty"])
            send(f"🎯 익절\n{ticker}")
            remove.append(ticker)

    for t in remove:
        active_positions.pop(t)

# ================= 실행 =================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CallbackQueryHandler(handle))

# ⭐⭐⭐ 핵심: Conflict 해결 ⭐⭐⭐
updater.start_polling(drop_pending_updates=True)

print("🚀 실전형 시스템 실행")

last_position_check = 0

while True:
    now = time.time()

    try:
        scan()
    except Exception as e:
        print(f"[스캔 오류] {e}")

    if now - last_position_check >= POSITION_CHECK_INTERVAL:
        try:
            monitor()
        except Exception as e:
            print(f"[감시 오류] {e}")
        last_position_check = now

    time.sleep(SCAN_INTERVAL)
