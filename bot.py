import pybithumb
import time
import uuid
import json
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

ACCOUNT_SIZE = 20000
FIXED_ENTRY_KRW = 11000
MIN_ORDER = 5000
RETRY = 3

ALERT_COOLDOWN = 1800
STOP_OUT_COOLDOWN = 3600
BUTTON_EXPIRE = 600   # 10분

TOP_TICKERS = 30
MAX_SIGNALS = 1

LOG_FILE = "trade_log.json"

# ===== 추천 필터 =====
MIN_TOTAL_SCORE = 4
MIN_ENTRY_SCORE = 2

SUPPORT_NEAR_PCT = 0.03
SUPPORT_REBOUND_ALLOW_PCT = 0.006
RECENT_PUMP_LIMIT = 6.0
MIN_RESISTANCE_ROOM = 0.015
MIN_VOLUME_RATIO = 1.0
MAX_RSI_FOR_ENTRY = 68
MIN_RSI_FOR_ENTRY = 28

# ===== 매수 방패 설정 =====
MAX_CHASE_PCT = 1.0
MAX_DROP_FROM_ENTRY_PCT = 1.5
MIN_TP1_GAP_PCT = 1.0
MIN_STOP_GAP_PCT = 1.0
MAX_STOP_GAP_PCT = 4.5
MIN_RR = 0.8
FAST_DROP_LIMIT = -1.5

# ===== 익절/손절 구조 =====
TP1_MULTIPLIER = 1.022   # 2.2% 익절
TP2_MULTIPLIER = 1.040   # 표시용만 유지 가능
STOP_MULTIPLIER = 0.982

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

button_data_store = {}
active_positions = {}
recent_alerts = {}
recent_stopouts = {}

# ================= 숫자 표시 =================
def r(x, n=4):
    try:
        return round(float(x), n)
    except Exception:
        return 0.0

def fmt_price(x):
    try:
        x = float(x)
    except Exception:
        return "0"

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
    except Exception:
        return "0.00%"

# ================= 텔레그램 전송 =================
def send_tg(text, reply_markup=None, retries=3):
    for i in range(retries):
        try:
            bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                reply_markup=reply_markup,
                timeout=10
            )
            return True
        except Exception as e:
            print(f"[텔레그램 전송 오류 {i+1}/{retries}] {e}")
            time.sleep(1.5)
    return False

# ================= 로그 =================
def load_logs():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

trade_logs = load_logs()

def save_logs():
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(trade_logs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 로그 저장 에러: {e}")

def add_log(data):
    trade_logs.append(data)
    save_logs()

# ================= 안전 조회 =================
def get_price_safe(ticker):
    for _ in range(RETRY):
        try:
            price = pybithumb.get_current_price(ticker)
            if price is not None and price != -1:
                return float(price)
        except Exception:
            pass
        time.sleep(0.2)
    return -1

def get_ohlcv_safe(ticker):
    for _ in range(RETRY):
        try:
            df = pybithumb.get_ohlcv(ticker)
            if df is not None and len(df) >= 40:
                return df
        except Exception:
            pass
        time.sleep(0.2)
    return None

def get_balance_safe(ticker):
    try:
        bal = bithumb.get_balance(ticker)
        return float(bal[0] or 0)
    except Exception:
        return 0.0

def get_krw_balance_safe():
    try:
        bal = bithumb.get_balance("BTC")
        if bal and len(bal) >= 3:
            return float(bal[2] or 0)
    except Exception:
        pass
    return float(ACCOUNT_SIZE)

# ================= 지표 =================
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_position(entry):
    if entry <= 0:
        return 0
    qty = FIXED_ENTRY_KRW / entry
    return int(qty)

def pct_change(a, b):
    try:
        if b == 0:
            return 0.0
        return ((a - b) / b) * 100
    except Exception:
        return 0.0

# ================= 주문 =================
def safe_buy(ticker, qty, max_entry):
    price = get_price_safe(ticker)
    if price == -1:
        return False, "현재가 조회 실패"

    if price > max_entry * (1 + MAX_CHASE_PCT / 100):
        return False, f"현재가 {fmt_price(price)} / 진입가 {fmt_price(max_entry)} 대비 +{MAX_CHASE_PCT}% 초과"

    order_value = price * qty

    if order_value < MIN_ORDER:
        qty = int(MIN_ORDER / price) + 1
        order_value = price * qty

    if order_value < FIXED_ENTRY_KRW:
        qty = int(FIXED_ENTRY_KRW / price) + 1
        order_value = price * qty

    krw_balance = get_krw_balance_safe()
    if krw_balance < order_value:
        qty = int(krw_balance / price)
        if qty <= 0:
            return False, "KRW 잔액 부족"

    try:
        result = bithumb.buy_market_order(ticker, qty)
        time.sleep(1)

        balance = get_balance_safe(ticker)
        if balance <= 0:
            return False, f"매수 후 잔고 없음 / 응답: {result}"

        return True, {
            "balance": balance,
            "filled_price": price,
            "result": result
        }
    except Exception as e:
        return False, str(e)

def safe_sell_market(ticker, qty):
    try:
        if qty <= 0:
            return False, "매도 수량 0"
        result = bithumb.sell_market_order(ticker, qty)
        return True, result
    except Exception as e:
        return False, str(e)

# ================= 실전 필터 =================
def detect_support_rebound(df, support):
    recent = df.tail(5)

    prev_low = float(recent["low"].iloc[-2])
    last_low = float(recent["low"].iloc[-1])
    last_open = float(recent["open"].iloc[-1])
    last_close = float(recent["close"].iloc[-1])
    prev_close = float(recent["close"].iloc[-2])

    touched = (
        prev_low <= support * (1 + SUPPORT_REBOUND_ALLOW_PCT)
        or last_low <= support * (1 + SUPPORT_REBOUND_ALLOW_PCT)
    )

    bullish_close = (last_close >= last_open) or (last_close > prev_close)
    recovered = last_close >= support * 0.998

    return touched and bullish_close and recovered

def detect_rsi_recovery(rsi_series):
    rs = rsi_series.dropna()
    if len(rs) < 3:
        return False
    r1 = float(rs.iloc[-1])
    r2 = float(rs.iloc[-2])
    r3 = float(rs.iloc[-3])
    return r1 > r2 and r2 >= r3

def detect_volume_surge(df):
    recent_vol = float(df["volume"].tail(3).mean())
    base_vol = float(df["volume"].tail(20).mean())
    if base_vol <= 0:
        return 0
    return recent_vol / base_vol

def detect_recent_pump(df):
    close_now = float(df["close"].iloc[-1])
    close_3 = float(df["close"].iloc[-4])
    return pct_change(close_now, close_3)

def detect_trend(df):
    ma5 = float(df["close"].rolling(5).mean().iloc[-1])
    ma10 = float(df["close"].rolling(10).mean().iloc[-1])
    ma20 = float(df["close"].rolling(20).mean().iloc[-1])
    price = float(df["close"].iloc[-1])

    return {
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "price_above_ma20": price > ma20,
        "ma5_above_ma10": ma5 > ma10,
        "ma10_above_ma20": ma10 > ma20
    }

# ================= 쉬운 문구 =================
def make_reason_warning_texts(score_data):
    reasons = []
    warnings = []

    if score_data["support_gap"] <= SUPPORT_NEAR_PCT:
        reasons.append(f"- 지지선 근처라 진입 자리로 볼 수 있어 ({r(score_data['support_gap'] * 100, 2)}%)")
    elif score_data["support_gap"] <= 0.04:
        reasons.append(f"- 지지선과 아주 멀진 않아 ({r(score_data['support_gap'] * 100, 2)}%)")
    else:
        warnings.append(f"- 지지선과 거리가 있어서 지금 바로 들어가긴 애매해 ({r(score_data['support_gap'] * 100, 2)}%)")

    if score_data["rebound_ok"]:
        reasons.append("- 지지선 근처에서 다시 올라오려는 모습이 보여")
    else:
        warnings.append("- 아직 반등 시작이라고 확신하긴 어려워")

    if score_data["ma5_above_ma10"]:
        reasons.append("- 최근 흐름이 조금씩 좋아지고 있어")
    else:
        warnings.append("- 최근 흐름이 아직 강하진 않아")

    if score_data["price_above_ma20"]:
        reasons.append("- 전체 분위기로는 아직 완전히 무너진 자리는 아니야")
    else:
        warnings.append("- 전체 분위기는 아직 조금 약한 편이야")

    rsi = score_data["rsi"]
    if MIN_RSI_FOR_ENTRY <= rsi <= MAX_RSI_FOR_ENTRY:
        reasons.append(f"- 과열도 아니고 너무 약하지도 않은 상태야 ({r(rsi, 2)})")
    elif rsi < MIN_RSI_FOR_ENTRY:
        warnings.append(f"- 아직 약한 흐름일 수 있어 ({r(rsi, 2)})")
    else:
        warnings.append(f"- 이미 꽤 오른 상태일 수 있어, 추격 주의 ({r(rsi, 2)})")

    if score_data["rsi_recovery"]:
        reasons.append("- 매수세가 조금씩 살아나는 중이야")
    else:
        warnings.append("- 힘이 붙는 모습은 아직 약해")

    vol_ratio = score_data["vol_ratio"]
    if vol_ratio >= MIN_VOLUME_RATIO:
        reasons.append(f"- 거래량이 평소보다 들어오고 있어 ({r(vol_ratio, 2)}배)")
    elif vol_ratio >= 0.9:
        reasons.append(f"- 거래량은 보통 수준이야 ({r(vol_ratio, 2)}배)")
    else:
        warnings.append(f"- 거래량이 부족해서 힘이 약할 수 있어 ({r(vol_ratio, 2)}배)")

    recent_pump = score_data["recent_pump"]
    if recent_pump >= RECENT_PUMP_LIMIT:
        warnings.append(f"- 최근 너무 빨리 올라서 지금 사면 늦을 수 있어 ({r(recent_pump, 2)}%)")
    elif recent_pump >= 2.0:
        warnings.append(f"- 최근 이미 좀 올라서 급하게 사면 불리할 수 있어 ({r(recent_pump, 2)}%)")
    else:
        reasons.append(f"- 최근 급하게 오른 상태는 아니야 ({r(recent_pump, 2)}%)")

    if score_data["near_high"]:
        warnings.append("- 최근 고점 근처라 바로 사면 물릴 수 있어")

    if score_data["resistance_room"] >= MIN_RESISTANCE_ROOM:
        reasons.append(f"- 위로 더 오를 공간이 조금 있어 ({r(score_data['resistance_room'] * 100, 2)}%)")
    else:
        warnings.append(f"- 위쪽에 막힐 가능성이 있어 ({r(score_data['resistance_room'] * 100, 2)}%)")

    return reasons, warnings

# ================= 코인 분석 =================
def analyze_coin(ticker):
    df = get_ohlcv_safe(ticker)
    if df is None:
        return None

    price = get_price_safe(ticker)
    if price == -1:
        return None

    recent5 = df.tail(5)
    recent20 = df.tail(20)

    low5 = float(recent5["low"].min())
    high5 = float(recent5["high"].max())

    support = float(recent20["low"].min())
    resistance = float(recent20["high"].max())

    trend = detect_trend(df)
    ma5 = trend["ma5"]
    ma10 = trend["ma10"]
    ma20 = trend["ma20"]

    rsi_series = calculate_rsi(df)
    rsi = float(rsi_series.iloc[-1])

    vol_ratio = detect_volume_surge(df)
    recent_pump = detect_recent_pump(df)
    rebound_ok = detect_support_rebound(df, support)
    rsi_recovery = detect_rsi_recovery(rsi_series)

    score = 0
    entry_score = 0

    support_gap = abs(price - support) / support if support > 0 else 999
    near_high = price >= high5 * 0.99
    resistance_room = (resistance - price) / price if price > 0 else 0

    if support_gap <= SUPPORT_NEAR_PCT:
        score += 2
        entry_score += 2
    elif support_gap <= 0.04:
        score += 1
        entry_score += 1
    else:
        entry_score -= 2

    if rebound_ok:
        score += 2
        entry_score += 2
    else:
        entry_score -= 1

    if trend["ma5_above_ma10"]:
        score += 1
        entry_score += 1

    if trend["price_above_ma20"]:
        score += 2
        entry_score += 1
    else:
        score -= 1
        entry_score -= 1

    if MIN_RSI_FOR_ENTRY <= rsi <= MAX_RSI_FOR_ENTRY:
        score += 1

    if rsi_recovery:
        score += 1
        entry_score += 1

    if vol_ratio >= MIN_VOLUME_RATIO:
        score += 2
        entry_score += 1
    elif vol_ratio >= 0.9:
        score += 1
    else:
        entry_score -= 1

    if recent_pump >= RECENT_PUMP_LIMIT:
        score -= 3
        entry_score -= 3
    elif recent_pump >= 2.0:
        entry_score -= 1

    if near_high:
        entry_score -= 2

    if resistance_room >= MIN_RESISTANCE_ROOM:
        score += 1
        entry_score += 1
    else:
        entry_score -= 2

    entry_price = max(support * 1.002, low5 + (high5 - low5) * 0.25)
    entry_price = min(entry_price, price * 1.002)
    entry_price = float(entry_price)

    stop_base = min(low5, support)
    stop_price = float(stop_base) * STOP_MULTIPLIER

    tp1 = float(entry_price) * TP1_MULTIPLIER
    tp2 = float(entry_price) * TP2_MULTIPLIER  # 참고용

    qty = calculate_position(entry_price)

    if stop_price >= entry_price:
        entry_score -= 2
        stop_price = float(entry_price) * 0.985

    score_data = {
        "support_gap": support_gap,
        "rebound_ok": rebound_ok,
        "ma5_above_ma10": trend["ma5_above_ma10"],
        "price_above_ma20": trend["price_above_ma20"],
        "rsi": rsi,
        "rsi_recovery": rsi_recovery,
        "vol_ratio": vol_ratio,
        "recent_pump": recent_pump,
        "near_high": near_high,
        "resistance_room": resistance_room
    }

    reasons, warnings = make_reason_warning_texts(score_data)

    if score >= MIN_TOTAL_SCORE and entry_score >= MIN_ENTRY_SCORE and qty > 0:
        status = "진입 가능"
    elif score >= 3 and entry_score >= 1 and qty > 0:
        status = "조금 더 보고 들어가는 게 좋음"
    else:
        status = "지금은 쉬는 게 좋음"

    return {
        "ticker": ticker,
        "price": float(price),
        "entry": float(entry_price),
        "stop": float(stop_price),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "qty": qty,
        "status": status,
        "score": score,
        "entry_score": entry_score,
        "reason": "\n".join(reasons) if reasons else "- 없음",
        "warning": "\n".join(warnings) if warnings else "- 특별한 경고 없음",
        "support": float(support),
        "resistance": float(resistance),
        "ma5": float(ma5),
        "ma10": float(ma10),
        "ma20": float(ma20),
        "rsi": r(rsi, 2),
        "vol_ratio": r(vol_ratio, 2),
        "recent_pump": r(recent_pump, 2),
        "rebound_ok": rebound_ok,
        "rsi_recovery": rsi_recovery,
    }

# ================= 매수 전 방패 =================
def pre_buy_guard_checks(ticker, coin):
    current_price = get_price_safe(ticker)
    if current_price == -1:
        return False, "현재가를 불러오지 못했어"

    if current_price < coin["support"] * 0.997:
        return False, (
            f"지지선 아래로 내려갔어\n"
            f"현재가: {fmt_price(current_price)} / 지지선: {fmt_price(coin['support'])}"
        )

    if current_price > coin["entry"] * (1 + MAX_CHASE_PCT / 100):
        return False, (
            f"추천 진입가보다 너무 올라갔어\n"
            f"현재가: {fmt_price(current_price)} / 추천 진입가: {fmt_price(coin['entry'])}"
        )

    if current_price < coin["entry"] * (1 - MAX_DROP_FROM_ENTRY_PCT / 100):
        return False, (
            f"추천 받았던 자리보다 너무 많이 내려왔어\n"
            f"현재가: {fmt_price(current_price)} / 추천 진입가: {fmt_price(coin['entry'])}"
        )

    if coin["tp1"] > 0:
        tp_gap = (coin["tp1"] - current_price) / current_price * 100
        if tp_gap <= MIN_TP1_GAP_PCT:
            return False, (
                f"목표가가 너무 가까워\n"
                f"현재가: {fmt_price(current_price)} / 목표가: {fmt_price(coin['tp1'])}"
            )

    stop_gap = ((coin["entry"] - coin["stop"]) / coin["entry"] * 100) if coin["entry"] > 0 else 0
    if stop_gap < MIN_STOP_GAP_PCT:
        return False, (
            f"손절폭이 너무 좁아 ({round(stop_gap, 2)}%)\n"
            f"작은 흔들림에도 바로 잘릴 수 있어"
        )
    if stop_gap > MAX_STOP_GAP_PCT:
        return False, (
            f"손절폭이 너무 넓어 ({round(stop_gap, 2)}%)\n"
            f"잃을 수 있는 폭이 커"
        )

    risk = coin["entry"] - coin["stop"]
    reward = coin["tp1"] - coin["entry"]
    if risk <= 0:
        return False, "손절 구조가 이상해서 이번 진입은 넘길게"
    rr = reward / risk
    if rr < MIN_RR:
        return False, f"먹을 수 있는 폭보다 잃을 위험이 더 커 (손익비 {round(rr, 2)})"

    krw_balance = get_krw_balance_safe()
    order_value = current_price * coin["qty"]

    if order_value < MIN_ORDER:
        return False, f"최소 주문금액이 안 맞아 (예상 주문금액 {round(order_value)}원)"
    if krw_balance < order_value:
        return False, f"잔액이 부족해 (예상 {round(order_value)}원 / 잔액 {round(krw_balance)}원)"

    df_now = get_ohlcv_safe(ticker)
    if df_now is not None and len(df_now) >= 3:
        recent = df_now.tail(3)
        close_now = float(recent["close"].iloc[-1])
        close_prev = float(recent["close"].iloc[-2])
        drop_pct = ((close_now - close_prev) / close_prev) * 100 if close_prev > 0 else 0
        if drop_pct <= FAST_DROP_LIMIT:
            return False, f"방금 급하게 밀리는 중이야 ({round(drop_pct, 2)}%)"

    return True, f"매수 가능 / 현재가: {fmt_price(current_price)}"

# ================= 정리 함수 =================
def cleanup_button_store():
    now = time.time()
    delete_keys = []
    for k, v in button_data_store.items():
        if now - v["created_at"] > BUTTON_EXPIRE or v.get("used"):
            delete_keys.append(k)

    for k in delete_keys:
        button_data_store.pop(k, None)

def cleanup_stopouts():
    now = time.time()
    delete_keys = []
    for k, ts in recent_stopouts.items():
        if now - ts > STOP_OUT_COOLDOWN:
            delete_keys.append(k)

    for k in delete_keys:
        recent_stopouts.pop(k, None)

# ================= 추천 전송 =================
def send_multi_signal():
    cleanup_button_store()
    cleanup_stopouts()

    try:
        tickers = pybithumb.get_tickers()
        if not tickers:
            return
    except Exception as e:
        print(f"⚠️ 티커 조회 실패: {e}")
        return

    now = time.time()

    if len(active_positions) >= 1:
        return

    ticker_data = []
    for t in tickers:
        df = get_ohlcv_safe(t)
        if df is None or len(df) < 20:
            continue
        try:
            vol = float(df["volume"].tail(5).mean())
            ticker_data.append((t, vol))
        except Exception:
            continue
        time.sleep(0.08)

    ticker_data.sort(key=lambda x: x[1], reverse=True)
    top_tickers = [t[0] for t in ticker_data[:TOP_TICKERS]]

    coins = []
    for t in top_tickers:
        if t in active_positions:
            continue

        if t in recent_alerts and now - recent_alerts[t] < ALERT_COOLDOWN:
            continue

        if t in recent_stopouts and now - recent_stopouts[t] < STOP_OUT_COOLDOWN:
            continue

        coin = analyze_coin(t)
        if coin:
            coins.append(coin)
        time.sleep(0.15)

    coins.sort(key=lambda x: (x["score"], x["entry_score"]), reverse=True)

    for c in coins[:MAX_SIGNALS]:
        if c["qty"] <= 0:
            continue
        if c["status"] == "지금은 쉬는 게 좋음":
            continue

        recent_alerts[c["ticker"]] = now

        reply_markup = None
        status_text = ""

        if c["status"] == "진입 가능":
            button_id = str(uuid.uuid4())[:8]
            button_data_store[button_id] = {
                "coin": c,
                "used": False,
                "created_at": now
            }
            keyboard = [
                [InlineKeyboardButton("🔥 매수", callback_data=f"BUY|{button_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            status_text = "✅ 프로그램 판단: 지금 들어가도 되는 편"
        else:
            status_text = "⏳ 프로그램 판단: 조금 더 지켜보는 게 좋아"

        msg = f"""
🔥 {c['ticker']}

{status_text}

현재가: {fmt_price(c['price'])}
지지선: {fmt_price(c['support'])}
저항선: {fmt_price(c['resistance'])}

추천 진입가: {fmt_price(c['entry'])}
손절가: {fmt_price(c['stop'])}

🎯 목표가
1차: {fmt_price(c['tp1'])}

추천 점수: {c['score']}
진입 적합도: {c['entry_score']}
고정 진입금: {FIXED_ENTRY_KRW}원
매수 방식: 1회 진입
수량: {c['qty']}

보조 지표
- RSI: {c['rsi']}
- 거래량 배수: {c['vol_ratio']}
- 최근 상승률: {c['recent_pump']}%
- MA5/10/20: {fmt_price(c['ma5'])} / {fmt_price(c['ma10'])} / {fmt_price(c['ma20'])}

왜 괜찮아 보이냐면
{c['reason']}

주의할 점
{c['warning']}
"""
        send_tg(msg.strip(), reply_markup=reply_markup)

# ================= 버튼 핸들러 =================
def button_handler(update, context):
    query = update.callback_query
    query.answer()

    data = query.data.split("|")
    if len(data) != 2:
        send_tg(f"⚠️ 버튼 정보가 이상해: {query.data}")
        return

    action, button_id = data
    if action != "BUY":
        send_tg(f"⚠️ 알 수 없는 버튼이야: {action}")
        return

    item = button_data_store.get(button_id)

    if not item:
        send_tg("⚠️ 이 버튼은 만료됐거나 이미 사용할 수 없어")
        return

    if item.get("used"):
        send_tg("⚠️ 이 버튼은 이미 사용했어")
        return

    if time.time() - item["created_at"] > BUTTON_EXPIRE:
        send_tg("⚠️ 추천 나온 지 시간이 좀 지나서 버튼이 만료됐어")
        button_data_store.pop(button_id, None)
        return

    coin = item["coin"]
    ticker = coin["ticker"]

    if coin["status"] != "진입 가능":
        send_tg(f"⚠️ {ticker}는 아직 바로 들어갈 자리까진 아니야")
        return

    if ticker in active_positions:
        send_tg(f"⚠️ {ticker}는 이미 들고 있어")
        button_data_store[button_id]["used"] = True
        return

    if len(active_positions) >= 1:
        send_tg("⚠️ 지금은 한 종목만 운영 중이야")
        button_data_store[button_id]["used"] = True
        return

    if ticker in recent_stopouts and time.time() - recent_stopouts[ticker] < STOP_OUT_COOLDOWN:
        send_tg(f"⚠️ {ticker}는 최근 손절한 코인이라 잠깐 쉬는 중이야")
        button_data_store[button_id]["used"] = True
        return

    if coin["qty"] <= 0:
        send_tg(f"⚠️ {ticker}는 주문 수량이 0이라 매수할 수 없어")
        return

    ok_guard, guard_msg = pre_buy_guard_checks(ticker, coin)
    if not ok_guard:
        send_tg(
            f"🛡️ {ticker} 매수 방어 작동\n"
            f"{guard_msg}\n"
            f"이번 진입은 건너갈게"
        )
        return

    button_data_store[button_id]["used"] = True

    send_tg(
        f"💡 {ticker} 매수 시작\n"
        f"추천 진입가: {fmt_price(coin['entry'])}\n"
        f"지지선: {fmt_price(coin['support'])}\n"
        f"손절가: {fmt_price(coin['stop'])}\n"
        f"목표가: {fmt_price(coin['tp1'])}\n"
        f"고정 진입금: {FIXED_ENTRY_KRW}원"
    )

    success, res = safe_buy(ticker, coin["qty"], coin["entry"])

    if success:
        bought_balance = float(res["balance"])
        avg_entry = float(res["filled_price"])

        active_positions[ticker] = {
            **coin,
            "entry": avg_entry,
            "bought_balance": bought_balance,
            "last_state": "진입완료",
            "stop_triggered": False,
            "tp_triggered": False
        }

        add_log({
            "time": int(time.time()),
            "type": "BUY",
            "ticker": ticker,
            "avg_entry": avg_entry,
            "balance": bought_balance,
            "score": coin["score"],
            "entry_score": coin["entry_score"]
        })

        send_tg(
            f"✅ {ticker} 매수 완료\n"
            f"체결 추정가: {fmt_price(avg_entry)}\n"
            f"목표가: {fmt_price(coin['tp1'])}\n"
            f"손절가: {fmt_price(coin['stop'])}\n"
            f"이제 목표가 도달 시 전량 익절, 손절가 이탈 시 전량 정리할게"
        )
    else:
        send_tg(f"❌ {ticker} 매수 실패: {res}")

# ================= 보유 포지션 감시 =================
def build_position_message(ticker, pos, state, price, balance):
    pnl = ((price - pos["entry"]) / pos["entry"] * 100) if pos["entry"] > 0 else 0
    trend = "흐름이 아직 괜찮아" if pos.get("ma5", 0) > pos.get("ma10", 0) else "흐름이 조금 약해"

    msg = (
        f"📊 {ticker} 상태 알림\n\n"
        f"현재가: {fmt_price(price)}\n"
        f"평균단가: {fmt_price(pos['entry'])}\n"
        f"수익률: {fmt_pct(pnl)}\n"
        f"보유수량: {round(balance, 6)}\n\n"
        f"지지선: {fmt_price(pos['support'])}\n"
        f"손절기준: {fmt_price(pos['stop'])}\n"
        f"목표가: {fmt_price(pos['tp1'])}\n"
        f"흐름: {trend}\n"
        f"상태: {state}\n\n"
        f"판단:\n"
    )

    if state == "손절이탈":
        msg += "- 기준이 깨졌어\n- 빠르게 정리하는 게 우선이야\n- 추가매수는 하지 않는 게 좋아"
    elif state == "익절도달":
        msg += "- 목표가에 도달했어\n- 정해둔 수익을 챙길 자리야"
    elif state == "경계":
        msg += "- 지지선 근처를 다시 확인하는 중이야\n- 다시 올라오는지 보고 판단하는 게 좋아"
    elif state == "홀딩":
        msg += "- 진입가 위에서 잘 버티고 있어\n- 목표가까지 가는지 지켜보는 중이야"
    else:
        msg += "- 진입가 아래에 있어\n- 반등 힘이 약하면 조심하는 게 좋아"

    return msg

def monitor_positions():
    remove_list = []

    for ticker, pos in list(active_positions.items()):
        price = get_price_safe(ticker)
        if price == -1:
            continue

        balance = get_balance_safe(ticker)

        if balance <= 0:
            if pos.get("last_state") != "종료":
                send_tg(f"📦 {ticker} 포지션 종료 감지")
                add_log({
                    "time": int(time.time()),
                    "type": "CLOSE",
                    "ticker": ticker,
                    "reason": "잔고없음"
                })
            remove_list.append(ticker)
            continue

        if price <= pos["stop"]:
            state = "손절이탈"
        elif price >= pos["tp1"]:
            state = "익절도달"
        elif price <= pos["support"]:
            state = "경계"
        elif price >= pos["entry"]:
            state = "홀딩"
        else:
            state = "진입가아래"

        # 손절
        if state == "손절이탈" and not pos.get("stop_triggered", False):
            success, res = safe_sell_market(ticker, balance)
            active_positions[ticker]["stop_triggered"] = True

            if success:
                send_tg(
                    f"🚨 {ticker} 손절 기준 이탈 → 전량 시장가 매도\n"
                    f"현재가: {fmt_price(price)} / 손절기준: {fmt_price(pos['stop'])}"
                )
                recent_stopouts[ticker] = time.time()
                add_log({
                    "time": int(time.time()),
                    "type": "STOP_OUT",
                    "ticker": ticker,
                    "price": r(price),
                    "entry": pos["entry"],
                    "reason": "손절이탈"
                })
                remove_list.append(ticker)
                continue
            else:
                send_tg(
                    f"❌ {ticker} 손절 시장가 매도 실패\n"
                    f"현재가: {fmt_price(price)} / 오류: {res}"
                )

        # 익절
        if state == "익절도달" and not pos.get("tp_triggered", False):
            success, res = safe_sell_market(ticker, balance)
            active_positions[ticker]["tp_triggered"] = True

            if success:
                pnl = ((price - pos["entry"]) / pos["entry"] * 100) if pos["entry"] > 0 else 0
                send_tg(
                    f"🎉 {ticker} 목표가 도달 → 전량 익절 완료\n"
                    f"현재가: {fmt_price(price)} / 목표가: {fmt_price(pos['tp1'])}\n"
                    f"수익률: {fmt_pct(pnl)}"
                )
                add_log({
                    "time": int(time.time()),
                    "type": "TAKE_PROFIT",
                    "ticker": ticker,
                    "price": r(price),
                    "entry": pos["entry"],
                    "pnl_pct": r(pnl, 2),
                    "reason": "목표가도달"
                })
                remove_list.append(ticker)
                continue
            else:
                send_tg(
                    f"❌ {ticker} 익절 시장가 매도 실패\n"
                    f"현재가: {fmt_price(price)} / 오류: {res}"
                )

        if state != pos.get("last_state"):
            send_tg(build_position_message(ticker, pos, state, price, balance))
            active_positions[ticker]["last_state"] = state

    for ticker in remove_list:
        if ticker in active_positions:
            del active_positions[ticker]

# ================= 실행 =================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CallbackQueryHandler(button_handler))

updater.start_polling()
print("🚀 실전형 반자동 트레이딩 시스템 실행")

last_position_check = 0

while True:
    now = time.time()

    try:
        send_multi_signal()
    except Exception as e:
        print(f"⚠️ 스캔 에러: {e}")

    if now - last_position_check >= POSITION_CHECK_INTERVAL:
        try:
            monitor_positions()
        except Exception as e:
            print(f"⚠️ 포지션 감시 에러: {e}")
        last_position_check = now

    time.sleep(SCAN_INTERVAL)