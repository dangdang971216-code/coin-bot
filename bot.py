import os
import time
import uuid
import json
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pybithumb
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CallbackQueryHandler, CommandHandler, CallbackContext

# ================= 설정 =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

SCAN_INTERVAL = 60
POSITION_CHECK_INTERVAL = 10
BUTTON_EXPIRE = 600
TOP_TICKERS = 80

FIXED_ENTRY_KRW = 11000
MIN_ORDER = 5000

# ===== 자동매수 설정 =====
AUTO_BUY = True
AUTO_BUY_START_HOUR = 9
AUTO_BUY_END_HOUR = 18
TIMEZONE = "Asia/Seoul"

# ===== 알림 / 재추천 =====
SAME_STATUS_COOLDOWN = 1200
ENTRY_NEAR_RANGE = 0.8

# ===== BTC 시장 필터 =====
BTC_MARKET_FILTER = True
BTC_CRASH_LOCK_MINUTES = 20
BTC_CRASH_THRESHOLD = -2.0
btc_lock_until = 0

# ===== 공격형 v3.3 세팅 =====
MIN_TP_GAP = 1.5
MAX_ENTRY_GAP = 1.5
MIN_VOL_RATIO = 0.35
MIN_ENTRY_SCORE = 1
MIN_RESISTANCE_GAP = 0.6

# ===== 본절 보호 =====
BREAKEVEN_TRIGGER = 1.5   # +1.5% 이상 갔을 때 활성화
BREAKEVEN_MARGIN = 0.2    # 진입가 +0.2%까지 밀리면 본절 정리

# ===== 트레일링 익절 옵션 =====
USE_TRAILING_TP = False
TRAILING_ARM_PCT = 2.0
TRAILING_GIVEBACK_PCT = 0.8

# ===== 로그 =====
LOG_FILE = "trade_log.json"

# ================= 기본 체크 =================
if not TELEGRAM_TOKEN or not CHAT_ID or not API_KEY or not API_SECRET:
    raise ValueError("환경변수(API_KEY, API_SECRET, TELEGRAM_TOKEN, CHAT_ID) 중 비어 있는 값이 있어요.")

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

active_positions = {}
button_data_store = {}
recent_alerts = {}

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
        print(f"[로그 저장 오류] {e}")

def add_log(log):
    trade_logs.append(log)
    save_logs()

def get_trade_summary_text():
    closes = [x for x in trade_logs if x.get("type") in ["TP", "STOP", "TRAIL_TP", "BREAKEVEN"]]

    if not closes:
        return "📊 아직 종료된 거래가 없어"

    total = len(closes)
    wins = len([x for x in closes if x["type"] in ["TP", "TRAIL_TP", "BREAKEVEN"]])
    losses = len([x for x in closes if x["type"] == "STOP"])
    win_rate = (wins / total) * 100 if total > 0 else 0
    total_pnl = sum(float(x.get("pnl_pct", 0)) for x in closes)
    avg_pnl = total_pnl / total if total > 0 else 0

    return (
        f"📊 거래 요약\n"
        f"총 종료 거래: {total}\n"
        f"익절/본절: {wins}\n"
        f"손절: {losses}\n"
        f"승률: {win_rate:.2f}%\n"
        f"누적 수익률: {total_pnl:.2f}%\n"
        f"평균 수익률: {avg_pnl:.2f}%"
    )

# ================= 유틸 =================
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
        return f"{x:,.1f}"
    elif x >= 1:
        return f"{x:,.2f}"
    elif x >= 0.1:
        return f"{x:,.3f}"
    elif x >= 0.01:
        return f"{x:,.4f}"
    else:
        return f"{x:,.6f}"

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

def now_kst():
    return datetime.now(ZoneInfo(TIMEZONE))

def is_weekday_auto_time():
    now = now_kst()
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
        return ratio >= 0.75, ratio
    except Exception:
        return False, 0.0

# ================= BTC 시장 필터 =================
def get_btc_market_state():
    global btc_lock_until

    if time.time() < btc_lock_until:
        remain = int((btc_lock_until - time.time()) / 60)
        return False, f"BTC 급락 잠금 중 ({remain}분 남음)"

    df = get_ohlcv("BTC")
    if df is None:
        return True, "BTC 조회 실패지만 진행"

    price = get_price("BTC")
    if price <= 0:
        return True, "BTC 현재가 조회 실패지만 진행"

    ma20 = float(df["close"].rolling(20).mean().iloc[-1])

    recent = df.tail(4)
    start = float(recent["close"].iloc[0])
    end = float(recent["close"].iloc[-1])
    drop_pct = ((end - start) / start) * 100 if start > 0 else 0

    if drop_pct <= BTC_CRASH_THRESHOLD:
        btc_lock_until = time.time() + BTC_CRASH_LOCK_MINUTES * 60
        return False, f"BTC 급락 감지 ({drop_pct:.2f}%), {BTC_CRASH_LOCK_MINUTES}분 잠금"

    if price < ma20 * 0.992:
        return False, "BTC가 MA20 아래라 시장 분위기가 약함"

    return True, "시장 분위기 무난"

# ================= 알림 관련 =================
def cleanup_buttons():
    now_ts = time.time()
    delete_keys = []
    for k, v in button_data_store.items():
        if now_ts - v["created_at"] > BUTTON_EXPIRE:
            delete_keys.append(k)
    for k in delete_keys:
        button_data_store.pop(k, None)

def should_send_alert(coin, now_ts):
    ticker = coin["ticker"]
    status = coin["status"]
    entry_score = coin.get("entry_score", 0)
    entry_near = coin.get("entry_near", False)

    if ticker not in recent_alerts:
        return True

    last_info = recent_alerts[ticker]
    last_status = last_info.get("status")
    last_time = last_info.get("time", 0)
    last_entry_score = last_info.get("entry_score", -999)
    last_entry_near = last_info.get("entry_near", False)

    if status != last_status:
        return True
    if entry_near and not last_entry_near:
        return True
    if entry_score > last_entry_score:
        return True
    if now_ts - last_time >= SAME_STATUS_COOLDOWN:
        return True

    return False

# ================= 매수 전 방어 =================
def pre_buy_check(ticker, coin):
    price = get_price(ticker)
    if price <= 0:
        return False, "현재가를 불러오지 못했어"

    if price < coin["support"]:
        return False, "지지선 아래로 내려가서 위험한 자리야"

    if price >= coin["tp"]:
        return False, "이미 목표가 근처라 지금은 너무 늦었어"

    if price > coin["entry"] * (1 + MAX_ENTRY_GAP / 100):
        return False, "추천 진입가보다 올라서 지금은 늦은 편이야"

    if price < coin["entry"] * 0.98:
        return False, "추천 받았던 자리보다 많이 내려와서 흐름이 깨졌어"

    tp_gap_pct = ((coin["tp"] - price) / price) * 100 if price > 0 else 0
    if tp_gap_pct < MIN_TP_GAP:
        return False, "목표가가 너무 가까워서 먹을 자리가 적어"

    stop_gap_pct = ((coin["entry"] - coin["stop"]) / coin["entry"]) * 100 if coin["entry"] > 0 else 0
    if stop_gap_pct > 6.0:
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

    if support * 0.97 <= price <= support * 1.04:
        score += 2
        entry_score += 1
        reasons.append("- 지지선 근처")
    elif support * 0.95 <= price <= support * 1.06:
        score += 1
        reasons.append("- 지지선에서 아주 멀진 않아")
    else:
        warnings.append("- 지지선과 거리가 있음")

    if ma5 > ma10:
        score += 1
        entry_score += 1
        reasons.append("- 상승 흐름 시작")
    else:
        warnings.append("- 최근 흐름이 아직 강하진 않아")

    if 28 <= rsi <= 68:
        score += 1
        reasons.append(f"- 과열도 아니고 너무 약하지도 않음 ({rsi:.2f})")
    elif rsi < 28:
        warnings.append(f"- 아직 약한 흐름일 수 있어 ({rsi:.2f})")
    else:
        warnings.append(f"- 이미 좀 오른 상태일 수 있어 ({rsi:.2f})")

    if vol_ratio >= 0.8:
        score += 1
        entry_score += 2
        reasons.append(f"- 거래량 좋음 ({vol_ratio:.2f}배)")
    elif vol_ratio >= MIN_VOL_RATIO:
        score += 1
        entry_score += 1
        reasons.append(f"- 거래량 무난 ({vol_ratio:.2f}배)")
    elif vol_ratio >= 0.25:
        reasons.append(f"- 거래량은 약하지만 완전 죽진 않음 ({vol_ratio:.2f}배)")
    else:
        return None

    if volume_recovery_ok:
        entry_score += 1
        reasons.append(f"- 최근 거래량 회복 중 ({volume_recovery_ratio:.2f}배)")
    else:
        warnings.append(f"- 최근 거래량 회복은 아직 약해 ({volume_recovery_ratio:.2f}배)")

    if detect_bad_flow(df):
        entry_score -= 1
        warnings.append("- 최근 종가 흐름이 계속 밀림")

    if pump >= 8.0:
        return None
    elif pump >= 4.0:
        entry_score -= 1
        warnings.append(f"- 최근 이미 좀 올라서 급하게 사면 불리해 ({pump:.2f}%)")
    else:
        reasons.append(f"- 최근 급하게 오른 상태는 아님 ({pump:.2f}%)")

    if rebound:
        entry_score += 2
        reasons.append("- 눌렸다가 다시 반등 시작")
    else:
        entry_score -= 1
        warnings.append("- 반등이 아직 약함")

    entry = support * 1.01
    stop = support * 0.97

    if vol_ratio >= 1.0 and rebound and volume_recovery_ok and entry_score >= 4:
        tp_mult = 1.06
        tp_type = "강한 목표"
    elif vol_ratio >= 0.6 and rebound:
        tp_mult = 1.04
        tp_type = "보통 목표"
    else:
        tp_mult = 1.02
        tp_type = "짧은 목표"

    tp = entry * tp_mult

    entry_gap_pct = ((price - entry) / entry) * 100 if entry > 0 else 999
    tp_gap_pct = ((tp - price) / price) * 100 if price > 0 else -999
    resistance_gap_pct = ((resistance - price) / price) * 100 if price > 0 else -999
    entry_near = abs(entry_gap_pct) <= ENTRY_NEAR_RANGE

    if resistance_gap_pct < MIN_RESISTANCE_GAP:
        return None
    elif resistance_gap_pct < 1.2:
        entry_score -= 1
        warnings.append(f"- 위쪽 저항이 가까운 편이야 ({resistance_gap_pct:.2f}%)")
    else:
        reasons.append(f"- 위쪽 저항까지 공간 있음 ({resistance_gap_pct:.2f}%)")

    if tp_type == "짧은 목표" and tp_gap_pct < 1.5:
        return None
    elif tp_type == "보통 목표" and tp_gap_pct < 2.0:
        return None
    elif tp_type == "강한 목표" and tp_gap_pct < 3.0:
        return None

    if price >= tp:
        return None
    if entry_gap_pct > 2.5:
        return None

    qty = int(FIXED_ENTRY_KRW / entry) if entry > 0 else 0
    if qty <= 0:
        return None

    if qty * entry < MIN_ORDER:
        qty = int(MIN_ORDER / entry) + 1

    risk = entry - stop
    reward = tp - entry
    rr = reward / risk if risk > 0 else 0
    if rr < 0.35:
        return None

    if entry_score < -1:
        return None

    if score >= 2 and entry_score >= MIN_ENTRY_SCORE and entry_gap_pct <= MAX_ENTRY_GAP:
        status = "🔥 강한 진입"
    elif score >= 2 and entry_score >= 0:
        status = "⚡ 단타 가능"
    else:
        status = "관찰"

    return {
        "ticker": ticker,
        "price": r(price),
        "entry": r(entry, 8),
        "stop": r(stop, 8),
        "tp": r(tp, 8),
        "tp_type": tp_type,
        "qty": qty,
        "status": status,
        "support": r(support, 8),
        "resistance": r(resistance, 8),
        "score": score,
        "entry_score": entry_score,
        "entry_gap_pct": r(entry_gap_pct, 2),
        "tp_gap_pct": r(tp_gap_pct, 2),
        "resistance_gap_pct": r(resistance_gap_pct, 2),
        "entry_near": entry_near,
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

    market_ok, market_msg = get_btc_market_state()
    if BTC_MARKET_FILTER and not market_ok:
        return False, f"시장 필터 차단: {market_msg}"

    if coin["status"] != "🔥 강한 진입":
        return False, "강한 진입이 아니야"
    if not coin.get("entry_near", False):
        return False, "진입가 근처가 아니야"
    if coin["tp_gap_pct"] < MIN_TP_GAP:
        return False, "수익폭 부족"
    if coin["entry_gap_pct"] > MAX_ENTRY_GAP:
        return False, "늦은 진입"
    if coin["entry_score"] < MIN_ENTRY_SCORE:
        return False, "점수 부족"

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
            "qty": real_balance,
            "peak_price": coin["entry"],
            "trailing_armed": False,
            "trailing_stop_price": 0.0,
            "breakeven_armed": False
        }

        add_log({
            "time": int(time.time()),
            "type": "BUY",
            "ticker": ticker,
            "entry": coin["entry"],
            "qty": real_balance,
            "mode": "AUTO"
        })

        send(
            f"🤖 자동매수 완료\n"
            f"{ticker}\n"
            f"보유수량: {real_balance:.8f}\n"
            f"진입가: {fmt_price(coin['entry'])}\n"
            f"목표가: {fmt_price(coin['tp'])}\n"
            f"손절가: {fmt_price(coin['stop'])}\n"
            f"목표가 유형: {coin['tp_type']}"
        )
        return True, "자동매수 완료"
    except Exception as e:
        return False, str(e)

# ================= 스캔 =================
def scan():
    if active_positions:
        return

    cleanup_buttons()
    now_ts = time.time()

    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print(f"[티커 조회 오류] {e}")
        return

    if not tickers:
        return

    market_ok, market_msg = get_btc_market_state()

    candidates = []
    for t in tickers[:TOP_TICKERS]:
        if t == "BTC":
            continue
        coin = analyze_coin(t)
        if coin and coin["status"] in ["관찰", "⚡ 단타 가능", "🔥 강한 진입"]:
            candidates.append(coin)

    if not candidates:
        print("조건 맞는 코인 없음")
        return

    candidates.sort(
        key=lambda x: (
            1 if x.get("entry_near", False) else 0,
            1 if x["tp_type"] == "강한 목표" else 0,
            1 if x["tp_type"] == "보통 목표" else 0,
            1 if x["status"] == "🔥 강한 진입" else 0,
            x["entry_score"],
            x["tp_gap_pct"],
            x["score"]
        ),
        reverse=True
    )

    coin = candidates[0]
    prev = recent_alerts.get(coin["ticker"], {})

    if coin.get("entry_near", False) and coin["status"] == "🔥 강한 진입" and AUTO_BUY and is_weekday_auto_time() and (not BTC_MARKET_FILTER or market_ok):
        success, msg = try_auto_buy(coin)
        if success:
            recent_alerts[coin["ticker"]] = {
                "time": now_ts,
                "status": "자동매수완료",
                "entry_score": coin["entry_score"],
                "entry_near": True,
                "tp_type": coin["tp_type"]
            }
            return
        else:
            print(f"[자동매수 실패 또는 미실행] {coin['ticker']} / {msg}")

    if not should_send_alert(coin, now_ts):
        return

    change_reason = ""
    if prev:
        if coin["entry_score"] > prev.get("entry_score", 0):
            change_reason += "\n📈 진입 점수 상승"
        if coin.get("entry_near") and not prev.get("entry_near", False):
            change_reason += "\n🎯 진입가 근접"
        if coin["tp_type"] != prev.get("tp_type", ""):
            change_reason += f"\n🚀 목표 유형 변화: {coin['tp_type']}"

    recent_alerts[coin["ticker"]] = {
        "time": now_ts,
        "status": coin["status"],
        "entry_score": coin["entry_score"],
        "entry_near": coin.get("entry_near", False),
        "tp_type": coin["tp_type"]
    }

    reply_markup = None

    if coin.get("entry_near", False) and coin["status"] == "🔥 강한 진입":
        status_text = "🚨 지금 진입 타이밍 (진입가 근접)"
        bid = str(uuid.uuid4())[:8]
        button_data_store[bid] = {"coin": coin, "created_at": now_ts}
        keyboard = [[InlineKeyboardButton("🔥 매수", callback_data=f"BUY|{bid}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

    elif coin["status"] == "🔥 강한 진입":
        status_text = "✅ 지금 타이밍 괜찮음"
        bid = str(uuid.uuid4())[:8]
        button_data_store[bid] = {"coin": coin, "created_at": now_ts}
        keyboard = [[InlineKeyboardButton("🔥 매수", callback_data=f"BUY|{bid}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

    elif coin["status"] == "⚡ 단타 가능":
        status_text = "⚡ 짧게 노려볼 수 있는 자리"
    else:
        status_text = "⏳ 아직은 관찰용"

    extra_text = ""
    if coin["status"] == "🔥 강한 진입":
        if AUTO_BUY and is_weekday_auto_time():
            if BTC_MARKET_FILTER and not market_ok:
                extra_text = f"\n자동매수: 시장 필터로 보류 ({market_msg})"
            elif coin.get("entry_near", False):
                extra_text = "\n자동매수: 진입가 근처라 우선 실행 대상"
            else:
                extra_text = "\n자동매수: 실행 가능한 시간대"
        elif AUTO_BUY:
            extra_text = f"\n자동매수: 시간 밖이라 대기 중 (평일 {AUTO_BUY_START_HOUR}:00~{AUTO_BUY_END_HOUR}:00)"

    msg = f"""
🔥 {coin['ticker']}

{status_text}{extra_text}{change_reason}

현재가: {fmt_price(coin['price'])}
지지선: {fmt_price(coin['support'])}
저항선: {fmt_price(coin['resistance'])}

추천 진입가: {fmt_price(coin['entry'])}
손절가: {fmt_price(coin['stop'])}
목표가: {fmt_price(coin['tp'])}
목표가 유형: {coin['tp_type']}

진입가와 현재가 차이: {fmt_pct(coin['entry_gap_pct'])}
목표가까지 여유: {fmt_pct(coin['tp_gap_pct'])}
저항까지 여유: {fmt_pct(coin['resistance_gap_pct'])}

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
def handle(update: Update, context: CallbackContext):
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
    if coin["status"] != "🔥 강한 진입":
        send("지금은 수동 진입까지 하기엔 애매한 자리야")
        return

    market_ok, market_msg = get_btc_market_state()
    if BTC_MARKET_FILTER and not market_ok:
        send(f"🛡️ 매수 취소: 시장 필터 차단 ({market_msg})")
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
            "qty": real_balance,
            "peak_price": coin["entry"],
            "trailing_armed": False,
            "trailing_stop_price": 0.0,
            "breakeven_armed": False
        }

        add_log({
            "time": int(time.time()),
            "type": "BUY",
            "ticker": ticker,
            "entry": coin["entry"],
            "qty": real_balance,
            "mode": "MANUAL"
        })

        send(
            f"✅ 수동 매수 완료\n"
            f"{ticker}\n"
            f"보유수량: {real_balance:.8f}\n"
            f"목표가: {fmt_price(coin['tp'])}\n"
            f"손절가: {fmt_price(coin['stop'])}\n"
            f"목표가 유형: {coin['tp_type']}"
        )
    except Exception as e:
        send(f"❌ 매수 실패\n{e}")

# ================= 요약 명령 =================
def summary_command(update: Update, context: CallbackContext):
    send(get_trade_summary_text())

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

        if price > coin.get("peak_price", coin["entry"]):
            coin["peak_price"] = price

        # 손절
        if price <= coin["stop"]:
            try:
                bithumb.sell_market_order(ticker, balance)
                add_log({
                    "time": int(time.time()),
                    "type": "STOP",
                    "ticker": ticker,
                    "entry": coin["entry"],
                    "exit": price,
                    "pnl_pct": round(pnl, 2)
                })
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

        # 본절 보호
        if pnl >= BREAKEVEN_TRIGGER:
            coin["breakeven_armed"] = True

        if coin.get("breakeven_armed", False):
            breakeven_price = coin["entry"] * (1 + BREAKEVEN_MARGIN / 100)
            if price <= breakeven_price:
                try:
                    bithumb.sell_market_order(ticker, balance)
                    add_log({
                        "time": int(time.time()),
                        "type": "BREAKEVEN",
                        "ticker": ticker,
                        "entry": coin["entry"],
                        "exit": price,
                        "pnl_pct": round(pnl, 2)
                    })
                    send(
                        f"🛡️ 본절 처리\n"
                        f"{ticker}\n"
                        f"현재가: {fmt_price(price)}\n"
                        f"수익률: {fmt_pct(pnl)}"
                    )
                    remove.append(ticker)
                    continue
                except Exception as e:
                    print(f"[본절 오류] {ticker} / {e}")

        # 트레일링 익절
        if USE_TRAILING_TP:
            if pnl >= TRAILING_ARM_PCT:
                coin["trailing_armed"] = True
                coin["trailing_stop_price"] = coin["peak_price"] * (1 - TRAILING_GIVEBACK_PCT / 100)

            if coin.get("trailing_armed", False) and price <= coin.get("trailing_stop_price", 0):
                try:
                    bithumb.sell_market_order(ticker, balance)
                    add_log({
                        "time": int(time.time()),
                        "type": "TRAIL_TP",
                        "ticker": ticker,
                        "entry": coin["entry"],
                        "exit": price,
                        "pnl_pct": round(pnl, 2)
                    })
                    send(
                        f"📈 트레일링 익절 완료\n"
                        f"{ticker}\n"
                        f"현재가: {fmt_price(price)}\n"
                        f"고점대비 되밀림 익절\n"
                        f"수익률: {fmt_pct(pnl)}"
                    )
                    remove.append(ticker)
                    continue
                except Exception as e:
                    print(f"[트레일링 익절 오류] {ticker} / {e}")

        # 일반 익절
        elif price >= coin["tp"]:
            try:
                bithumb.sell_market_order(ticker, balance)
                add_log({
                    "time": int(time.time()),
                    "type": "TP",
                    "ticker": ticker,
                    "entry": coin["entry"],
                    "exit": price,
                    "pnl_pct": round(pnl, 2)
                })
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
dispatcher.add_handler(CommandHandler("summary", summary_command))

updater.start_polling(drop_pending_updates=True)

print(f"🚀 공격형 v3.3 시스템 실행 / 기준시간대: {TIMEZONE}")

last_position_check = 0

while True:
    now_ts = time.time()

    try:
        scan()
    except Exception as e:
        print(f"[스캔 오류] {e}")
        traceback.print_exc()

    if now_ts - last_position_check >= POSITION_CHECK_INTERVAL:
        try:
            monitor()
        except Exception as e:
            print(f"[감시 오류] {e}")
            traceback.print_exc()
        last_position_check = now_ts

    time.sleep(SCAN_INTERVAL)
