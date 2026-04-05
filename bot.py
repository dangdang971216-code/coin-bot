import os
import time
import uuid
import traceback
import pybithumb

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CallbackQueryHandler

# ================= 설정 =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

SCAN_INTERVAL = 60
POSITION_CHECK_INTERVAL = 10
ALERT_COOLDOWN = 1200
BUTTON_EXPIRE = 600
TOP_TICKERS = 30

FIXED_ENTRY_KRW = 11000
MIN_ORDER = 5000

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

active_positions = {}
button_data_store = {}
recent_alerts = {}

# ================= 유틸 =================
def r(x, n=4):
    try:
        return round(float(x), n)
    except:
        return 0.0

def fmt(x):
    try:
        x = float(x)
    except:
        return "0.000000"

    if x >= 1000:
        return f"{x:,.0f}"
    elif x >= 100:
        return f"{x:,.2f}"
    elif x >= 1:
        return f"{x:,.4f}"
    elif x >= 0.1:
        return f"{x:,.5f}"
    elif x >= 0.01:
        return f"{x:,.6f}"
    else:
        return f"{x:,.8f}"

def fmt_pct(x):
    try:
        return f"{float(x):.2f}%"
    except:
        return "0.00%"

def send(msg, keyboard=None):
    try:
        bot.send_message(chat_id=CHAT_ID, text=msg.strip(), reply_markup=keyboard)
    except Exception as e:
        print(f"[텔레그램 오류] {e}")

# ================= 데이터 =================
def get_price(ticker):
    try:
        return float(pybithumb.get_current_price(ticker))
    except:
        return -1

def get_ohlcv(ticker):
    try:
        df = pybithumb.get_ohlcv(ticker)
        if df is None or len(df) < 20:
            return None
        return df
    except:
        return None

def get_balance(ticker):
    try:
        return float(bithumb.get_balance(ticker)[0] or 0)
    except:
        return 0

# ================= 지표 =================
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def detect_bad_flow(df):
    closes = list(df.tail(3)["close"])
    return len(closes) == 3 and closes[2] < closes[1] < closes[0]

def detect_rebound(df):
    closes = list(df.tail(5)["close"])
    if len(closes) < 5:
        return False
    return closes[-3] > closes[-2] and closes[-1] > closes[-2]

def detect_recent_pump(df):
    recent = df.tail(4)
    start = float(recent["close"].iloc[0])
    end = float(recent["close"].iloc[-1])
    return ((end - start) / start) * 100 if start > 0 else 0

# ================= 분석 =================
def analyze_coin(ticker):
    df = get_ohlcv(ticker)
    if df is None:
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

    pump = detect_recent_pump(df)
    rebound = detect_rebound(df)

    score = 0
    entry_score = 0
    reasons = []
    warnings = []

    # 지지선
    if support * 0.965 <= price <= support * 1.04:
        score += 2
        reasons.append("- 지지선 근처")
    elif support * 0.95 <= price <= support * 1.06:
        score += 1

    # 추세
    if ma5 > ma10:
        score += 1
        entry_score += 1

    # RSI
    if 28 <= rsi <= 65:
        score += 1

    # 거래량
    if vol_ratio >= 0.75:
        score += 1
        entry_score += 1
    elif vol_ratio < 0.5:
        entry_score -= 1

    # 흐름 방지
    if detect_bad_flow(df):
        entry_score -= 2

    # 급등 방지
    if pump > 5:
        entry_score -= 2

    # 반등 확인
    if rebound:
        entry_score += 2
    else:
        entry_score += 0

    entry = support * 1.01
    stop = support * 0.97
    tp = entry * 1.025

    qty = int(FIXED_ENTRY_KRW / entry)

    if qty * entry < MIN_ORDER:
        qty = int(MIN_ORDER / entry) + 1

    # 상태
    if score >= 2 and entry_score >= 2:
        status = "진입 가능"
    elif score >= 2:
        status = "관찰 추천"
    else:
        status = "관망"

    return {
        "ticker": ticker,
        "price": r(price),
        "entry": r(entry, 8),
        "stop": r(stop, 8),
        "tp": r(tp, 8),
        "qty": qty,
        "status": status,
        "support": r(support, 8),
        "resistance": r(resistance, 8),
        "score": score,
        "entry_score": entry_score
    }

# ================= 매수 방어 =================
def pre_buy_check(ticker, coin):
    price = get_price(ticker)

    if price < coin["support"]:
        return False, "지지선 이탈"

    if price > coin["entry"] * 1.015:
        return False, "늦은 진입"

    if price < coin["entry"] * 0.985:
        return False, "흐름 깨짐"

    return True, "OK"

# ================= 스캔 =================
def scan():
    if active_positions:
        return

    tickers = pybithumb.get_tickers()
    now = time.time()

    candidates = []

    for t in tickers[:TOP_TICKERS]:
        if t in recent_alerts and now - recent_alerts[t] < ALERT_COOLDOWN:
            continue

        coin = analyze_coin(t)
        if coin and coin["status"] != "관망":
            candidates.append(coin)

    if not candidates:
        return

    candidates.sort(key=lambda x: (x["status"] == "진입 가능", x["score"], x["entry_score"]), reverse=True)

    coin = candidates[0]
    recent_alerts[coin["ticker"]] = now

    if coin["status"] == "진입 가능":
        bid = str(uuid.uuid4())[:8]
        button_data_store[bid] = coin

        keyboard = [[InlineKeyboardButton("🔥 매수", callback_data=f"BUY|{bid}")]]
        markup = InlineKeyboardMarkup(keyboard)
        text = "지금 들어가도 되는 자리"
    else:
        markup = None
        text = "아직은 관찰"

    send(f"""
🔥 {coin['ticker']}
{text}

현재가: {fmt(coin['price'])}
진입가: {fmt(coin['entry'])}
손절가: {fmt(coin['stop'])}
목표가: {fmt(coin['tp'])}

점수: {coin['score']} / 진입점수: {coin['entry_score']}
""", markup)

# ================= 매수 =================
def handle(update, context):
    query = update.callback_query
    query.answer()

    _, bid = query.data.split("|")
    coin = button_data_store.get(bid)

    if not coin:
        send("만료된 신호")
        return

    ok, msg = pre_buy_check(coin["ticker"], coin)

    if not ok:
        send(f"매수 취소: {msg}")
        return

    try:
        bithumb.buy_market_order(coin["ticker"], coin["qty"])
        active_positions[coin["ticker"]] = coin
        send(f"매수 완료: {coin['ticker']}")
    except Exception as e:
        send(f"매수 실패: {e}")

# ================= 감시 =================
def monitor():
    remove = []

    for ticker, coin in active_positions.items():
        price = get_price(ticker)

        if price <= coin["stop"]:
            bithumb.sell_market_order(ticker, coin["qty"])
            send(f"손절: {ticker}")
            remove.append(ticker)

        elif price >= coin["tp"]:
            bithumb.sell_market_order(ticker, coin["qty"])
            send(f"익절: {ticker}")
            remove.append(ticker)

    for t in remove:
        active_positions.pop(t)

# ================= 실행 =================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CallbackQueryHandler(handle))

updater.start_polling(drop_pending_updates=True)

print("🚀 실전형 시스템 실행")

last_check = 0

while True:
    now = time.time()

    try:
        scan()
    except:
        traceback.print_exc()

    if now - last_check >= POSITION_CHECK_INTERVAL:
        try:
            monitor()
        except:
            traceback.print_exc()
        last_check = now

    time.sleep(SCAN_INTERVAL)
