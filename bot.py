import os
import time
import uuid
import traceback
from datetime import datetime
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
ALERT_COOLDOWN = 1200      # 같은 코인 재알림 제한 20분
BUTTON_EXPIRE = 600        # 버튼 유효 시간 10분
TOP_TICKERS = 40

FIXED_ENTRY_KRW = 11000
MIN_ORDER = 5000

# ===== 자동매수 설정 =====
AUTO_BUY = True
AUTO_BUY_START_HOUR = 9
AUTO_BUY_END_HOUR = 18   # 18시는 포함 안 함 → 09:00~17:59

# ================= 기본 체크 =================
if not TELEGRAM_TOKEN or not CHAT_ID or not API_KEY or not API_SECRET:
    raise ValueError("환경변수(API_KEY, API_SECRET, TELEGRAM_TOKEN, CHAT_ID) 중 비어 있는 값이 있어요.")

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

active_positions = {}
button_data_store = {}
recent_alerts = {}

# ================= 유틸 =================
def r(x, n=4):
    try:
        return round(float(x), n)
    except Exception:
        return 0.0

def fmt_price(x):
    """
    빗썸 앱 느낌으로 표시:
    - 1원 이상이면 정수
    - 1원 미만이면 소수점 표시
    """
    try:
        x = float(x)
    except Exception:
        return "0"

    if x >= 1:
        return f"{x:,.0f}"
    elif x >= 0.1:
        return f"{x:,.4f}"
    elif x >= 0.01:
        return f"{x:,.5f}"
    elif x >= 0.001:
        return f"{x:,.6f}"
    else:
        return f"{x:,.8f}"

def fmt_pct(x):
    try:
        return f"{float(x):.2f}%"
    except Exception:
        return "0.00%"

def send(msg, keyboard=None):
    try:
        bot.send_message(chat_id=CHAT_ID, text=msg.strip(), reply_markup=keyboard)
    except Exception as e:
        print(f"[텔레그램 오류] {e}")

def is_weekday_auto_time():
    now = datetime.now()
    # 월=0 ~ 일=6
    is_weekday = now.weekday() < 5
    is_time_ok = AUTO_BUY_START_HOUR <= now.hour < AUTO_BUY_END_HOUR
    return is_weekday and is_time_ok

# ================= 데이터 조회 =================
def get_price(ticker):
    try:
        price = pybithumb.get_current_price(ticker)
        if price is None:
            return -1
        return float(price)
    except Exception:
        return -1

def get_ohlcv(ticker):
    try:
        df = pybithumb.get_ohlcv(ticker)
        if df is None or len(df) < 30:
            return None
        return df
    except Exception:
        return None

def get_balance(ticker):
    try:
        bal = bithumb.get_balance(ticker)
        return float(bal[0] or 0)
    except Exception:
        return 0.0

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
    if len(recent) < 4:
        return 0.0
    start = float(recent["close"].iloc[0])
    end = float(recent["close"].iloc[-1])
    return ((end - start) / start) * 100 if start > 0 else 0.0

def detect_volume_recovery(df):
    try:
        recent_vol = float(df["volume"].tail(3).mean())
        prev_vol = float(df["volume"].tail(13).head(10).mean())
        if prev_vol <= 0:
            return False, 0.0
        ratio = recent_vol / prev_vol
        return ratio >= 1.05, ratio
    except Exception:
        return False, 0.0

# ================= 버튼 정리 =================
def cleanup_buttons():
    now = time.time()
    delete_keys = []
    for k, v in button_data_store.items():
        if now - v["created_at"] > BUTTON_EXPIRE:
            delete_keys.append(k)

    for k in delete_keys:
        button_data_store.pop(k, None)

# ================= 매수 전 방어 =================
def pre_buy_check(ticker, coin):
    price = get_price(ticker)
    if price <= 0:
        return False, "현재가를 불러오지 못했어"

    if price < coin["support"]:
        return False, "지지선 아래로 내려가서 위험한 자리야"

    if price >= coin["tp"]:
        return False, "이미 목표가 근처라 지금은 너무 늦었어"

    if price > coin["entry"] * 1.02:
        return False, "추천 진입가보다 너무 올라서 늦은 자리야"

    if price < coin["entry"] * 0.985:
        return False, "추천 받았던 자리보다 많이 내려와서 흐름이 깨졌어"

    tp_gap_pct = ((coin["tp"] - price) / price) * 100 if price > 0 else 0
    if tp_gap_pct < 0.6:
        return False, "목표가가 너무 가까워서 먹을 자리가 적어"

    stop_gap_pct = ((coin["entry"] - coin["stop"]) / coin["entry"]) * 100 if coin["entry"] > 0 else 0
    if stop_gap_pct > 5.0:
        return False, "손절폭이 너무 커서 잃을 위험이 커"

    return True, "OK"

# ================= 코인 분석 =================
def analyze_coin(ticker):
    df = get_ohlcv(ticker)
    if df is None:
        return None

    price = get_price(ticker)
    if price <= 0:
        return None

    recent20 = df.tail(20)

    support = float(recent20["low"].min())
    resistance = float(recent20["high"].max())

    ma5 = float(df["close"].rolling(5).mean().iloc[-1])
    ma10 = float(df["close"].rolling(10).mean().iloc[-1])

    rsi = float(calculate_rsi(df).iloc[-1])

    vol = float(recent20["volume"].iloc[-1])
    avg_vol = float(recent20["volume"].mean())
    vol_ratio = vol / avg_vol if avg_vol > 0 else 0

    pump = detect_recent_pump(df)
    rebound = detect_rebound(df)
    volume_recovery_ok, volume_recovery_ratio = detect_volume_recovery(df)

    score = 0
    entry_score = 0
    reasons = []
    warnings = []

    # 1) 지지선 범위
    if support * 0.965 <= price <= support * 1.04:
        score += 2
        reasons.append("- 지지선 근처라 진입 자리로 볼 수 있어")
    elif support * 0.95 <= price <= support * 1.06:
        score += 1
        reasons.append("- 지지선에서 아주 멀진 않아")
    else:
        warnings.append("- 지지선과 거리가 있어서 지금 바로 들어가긴 애매해")

    # 2) 단기 추세
    if ma5 > ma10:
        score += 1
        entry_score += 1
        reasons.append("- 최근 흐름이 조금씩 좋아지고 있어")
    else:
        warnings.append("- 최근 흐름이 아직 강하진 않아")

    # 3) RSI
    if 28 <= rsi <= 65:
        score += 1
        reasons.append(f"- 과열도 아니고 너무 약하지도 않은 상태야 ({rsi:.2f})")
    elif rsi < 28:
        warnings.append(f"- 아직 약한 흐름일 수 있어 ({rsi:.2f})")
    else:
        warnings.append(f"- 이미 좀 오른 상태일 수 있어 ({rsi:.2f})")

    # 4) 거래량 기본 체크
    if vol_ratio >= 0.75:
        score += 1
        entry_score += 1
        reasons.append(f"- 거래량이 평소보다 들어오고 있어 ({vol_ratio:.2f}배)")
    elif vol_ratio >= 0.55:
        reasons.append(f"- 거래량은 아주 나쁘진 않아 ({vol_ratio:.2f}배)")
    else:
        entry_score -= 1
        warnings.append(f"- 거래량이 약해서 힘이 부족할 수 있어 ({vol_ratio:.2f}배)")

    # 5) 거래량 회복
    if volume_recovery_ok:
        entry_score += 1
        reasons.append(f"- 최근 거래량이 다시 살아나는 중이야 ({volume_recovery_ratio:.2f}배)")
    else:
        warnings.append(f"- 최근 거래량 회복은 아직 약해 ({volume_recovery_ratio:.2f}배)")

    # 6) 연속 하락 흐름 방지
    if detect_bad_flow(df):
        entry_score -= 2
        warnings.append("- 최근 종가 흐름이 계속 밀리고 있어서 조심해야 해")

    # 7) 급등 추격 방지
    if pump >= 5.0:
        score -= 2
        entry_score -= 2
        warnings.append(f"- 최근 너무 빨리 올라서 지금 사면 늦을 수 있어 ({pump:.2f}%)")
    elif pump >= 2.5:
        entry_score -= 1
        warnings.append(f"- 최근 이미 좀 올라서 급하게 사면 불리할 수 있어 ({pump:.2f}%)")
    else:
        reasons.append(f"- 최근 급하게 오른 상태는 아니야 ({pump:.2f}%)")

    # 8) 눌림 후 반등 확인
    if rebound:
        entry_score += 2
        reasons.append("- 눌렸다가 다시 반등하려는 움직임이 보여")
    else:
        warnings.append("- 아직 반등 신호는 약해서 조금 더 지켜보는 게 좋아")

    # ================= 가격 계산 =================
    entry = support * 1.01
    stop = support * 0.97
    tp = entry * 1.025

    # 이미 늦은 자리 제거
    if price >= tp:
        return None

    if price > entry * 1.02:
        return None

    qty = int(FIXED_ENTRY_KRW / entry) if entry > 0 else 0

    if qty > 0 and entry * qty < MIN_ORDER:
        qty = int(MIN_ORDER / entry) + 1

    # 손익비 체크
    risk = entry - stop
    reward = tp - entry
    rr = reward / risk if risk > 0 else 0
    if rr < 0.45:
        entry_score -= 2
        warnings.append(f"- 먹을 자리보다 잃을 위험이 조금 더 커 ({rr:.2f})")

    # 쓰레기 필터
    if vol_ratio < 0.30:
        return None

    if entry_score < 0:
        return None

    if qty <= 0:
        return None

    # 상태 판정
    if score >= 2 and entry_score >= 3:
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
        "entry_score": entry_score,
        "reason": "\n".join(reasons) if reasons else "- 없음",
        "warning": "\n".join(warnings) if warnings else "- 특별한 경고 없음",
    }

# ================= 자동매수 =================
def try_auto_buy(coin):
    ticker = coin["ticker"]

    if ticker in active_positions:
        return False, f"{ticker}는 이미 들고 있어"

    if active_positions:
        return False, "이미 다른 코인 보유 중이야"

    if not AUTO_BUY:
        return False, "자동매수가 꺼져 있어"

    if not is_weekday_auto_time():
        return False, "자동매수 시간대가 아니야"

    ok, msg = pre_buy_check(ticker, coin)
    if not ok:
        return False, msg

    try:
        result = bithumb.buy_market_order(ticker, coin["qty"])
        time.sleep(1)

        real_balance = get_balance(ticker)
        if real_balance <= 0:
            return False, f"매수는 시도했는데 실제 잔고 확인이 안 돼 / 응답: {result}"

        active_positions[ticker] = {
            **coin,
            "qty": real_balance
        }

        send(
            f"🤖 자동매수 완료\n"
            f"{ticker}\n"
            f"보유수량: {real_balance:.8f}\n"
            f"진입가: {fmt_price(coin['entry'])}\n"
            f"목표가: {fmt_price(coin['tp'])}\n"
            f"손절가: {fmt_price(coin['stop'])}\n"
            f"자동매수 시간: 평일 {AUTO_BUY_START_HOUR}:00~{AUTO_BUY_END_HOUR}:00"
        )
        return True, "자동매수 완료"
    except Exception as e:
        return False, str(e)

# ================= 스캔 =================
def scan():
    if active_positions:
        return

    cleanup_buttons()
    now = time.time()

    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print(f"[티커 조회 오류] {e}")
        return

    if not tickers:
        return

    candidates = []

    for t in tickers[:TOP_TICKERS]:
        if t in recent_alerts and now - recent_alerts[t] < ALERT_COOLDOWN:
            continue

        coin = analyze_coin(t)
        if coin and coin["status"] != "관망":
            candidates.append(coin)

    if not candidates:
        print("조건 맞는 코인 없음")
        return

    candidates.sort(
        key=lambda x: (
            1 if x["status"] == "진입 가능" else 0,
            x["score"],
            x["entry_score"]
        ),
        reverse=True
    )

    coin = candidates[0]
    recent_alerts[coin["ticker"]] = now

    # 자동매수 먼저 시도
    if coin["status"] == "진입 가능" and AUTO_BUY and is_weekday_auto_time():
        success, msg = try_auto_buy(coin)
        if success:
            return
        else:
            print(f"[자동매수 미실행] {coin['ticker']} / {msg}")

    reply_markup = None
    status_text = ""

    if coin["status"] == "진입 가능":
        bid = str(uuid.uuid4())[:8]
        button_data_store[bid] = {
            "coin": coin,
            "created_at": now
        }
        keyboard = [[InlineKeyboardButton("🔥 매수", callback_data=f"BUY|{bid}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        status_text = "✅ 지금 들어가도 되는 편"
    else:
        status_text = "⏳ 아직은 관찰만 추천"

    auto_text = ""
    if AUTO_BUY:
        if is_weekday_auto_time():
            auto_text = f"\n자동매수: ON (평일 {AUTO_BUY_START_HOUR}:00~{AUTO_BUY_END_HOUR}:00)"
        else:
            auto_text = f"\n자동매수: 대기 중 (평일 {AUTO_BUY_START_HOUR}:00~{AUTO_BUY_END_HOUR}:00만 실행)"

    msg = f"""
🔥 {coin['ticker']}

{status_text}{auto_text}

현재가: {fmt_price(coin['price'])}
지지선: {fmt_price(coin['support'])}
저항선: {fmt_price(coin['resistance'])}

추천 진입가: {fmt_price(coin['entry'])}
손절가: {fmt_price(coin['stop'])}
목표가: {fmt_price(coin['tp'])}

추천 점수: {coin['score']}
진입 적합도: {coin['entry_score']}
고정 진입금: {FIXED_ENTRY_KRW}원
수량: {coin['qty']}

왜 괜찮아 보이냐면
{coin['reason']}

주의할 점
{coin['warning']}
"""
    send(msg, reply_markup)

# ================= 수동 매수 =================
def handle(update, context):
    query = update.callback_query
    query.answer()

    try:
        action, bid = query.data.split("|")
    except Exception:
        send("버튼 정보가 이상해")
        return

    if action != "BUY":
        send("알 수 없는 버튼이야")
        return

    item = button_data_store.get(bid)
    if not item:
        send("만료된 신호")
        return

    coin = item["coin"]
    ticker = coin["ticker"]

    if ticker in active_positions:
        send(f"{ticker}는 이미 들고 있어")
        return

    if active_positions:
        send("이미 다른 코인 보유 중이야")
        return

    ok, msg = pre_buy_check(ticker, coin)
    if not ok:
        send(f"🛡️ 매수 취소: {msg}")
        return

    try:
        result = bithumb.buy_market_order(ticker, coin["qty"])
        time.sleep(1)

        real_balance = get_balance(ticker)
        if real_balance <= 0:
            send(f"❌ 매수는 시도했는데 실제 잔고 확인이 안 돼\n응답: {result}")
            return

        active_positions[ticker] = {
            **coin,
            "qty": real_balance
        }

        send(
            f"✅ 수동 매수 완료\n"
            f"{ticker}\n"
            f"보유수량: {real_balance:.8f}\n"
            f"목표가: {fmt_price(coin['tp'])}\n"
            f"손절가: {fmt_price(coin['stop'])}"
        )
    except Exception as e:
        send(f"❌ 매수 실패\n{e}")

# ================= 감시 =================
def monitor():
    remove = []

    for ticker, coin in list(active_positions.items()):
        price = get_price(ticker)
        if price <= 0:
            continue

        balance = get_balance(ticker)
        if balance <= 0:
            remove.append(ticker)
            continue

        pnl = ((price - coin["entry"]) / coin["entry"] * 100) if coin["entry"] > 0 else 0

        if price <= coin["stop"]:
            try:
                bithumb.sell_market_order(ticker, balance)
                send(
                    f"🚨 손절\n"
                    f"{ticker}\n"
                    f"현재가: {fmt_price(price)}\n"
                    f"손절기준: {fmt_price(coin['stop'])}\n"
                    f"수익률: {fmt_pct(pnl)}"
                )
                remove.append(ticker)
                continue
            except Exception as e:
                print(f"[손절 오류] {ticker} / {e}")

        elif price >= coin["tp"]:
            try:
                bithumb.sell_market_order(ticker, balance)
                send(
                    f"🎯 익절 완료\n"
                    f"{ticker}\n"
                    f"현재가: {fmt_price(price)}\n"
                    f"목표가: {fmt_price(coin['tp'])}\n"
                    f"수익률: {fmt_pct(pnl)}"
                )
                remove.append(ticker)
                continue
            except Exception as e:
                print(f"[익절 오류] {ticker} / {e}")

    for t in remove:
        active_positions.pop(t, None)

# ================= 실행 =================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CallbackQueryHandler(handle))

# 텔레그램 충돌 방지
updater.start_polling(drop_pending_updates=True)

print("🚀 실전형 시스템 실행")

last_position_check = 0

while True:
    now = time.time()

    try:
        scan()
    except Exception as e:
        print(f"[스캔 오류] {e}")
        traceback.print_exc()

    if now - last_position_check >= POSITION_CHECK_INTERVAL:
        try:
            monitor()
        except Exception as e:
            print(f"[감시 오류] {e}")
            traceback.print_exc()
        last_position_check = now

    time.sleep(SCAN_INTERVAL)
