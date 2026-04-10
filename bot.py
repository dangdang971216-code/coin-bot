import os
import time
import json
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pybithumb
from telegram import Bot
from telegram.ext import Updater, CommandHandler, CallbackContext

# =========================================================
# 버전
# =========================================================
BOT_VERSION = "수익형 v6.5.9-stable2"

# =========================================================
# 환경변수
# =========================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not TELEGRAM_TOKEN or not CHAT_ID or not API_KEY or not API_SECRET:
    raise ValueError("환경변수(API_KEY, API_SECRET, TELEGRAM_TOKEN, CHAT_ID) 중 비어 있는 값이 있어요.")

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

# =========================================================
# 파일 / 시간
# =========================================================
TIMEZONE = "Asia/Seoul"
LOG_FILE = "trade_log.json"
POSITIONS_FILE = "active_positions.json"
PENDING_SELLS_FILE = "pending_sells.json"
PENDING_BUYS_FILE = "pending_buy_candidates.json"

# =========================================================
# 실행 주기
# =========================================================
LOOP_SLEEP = 1

# =========================================================
# 주문 / 자금
# =========================================================
MIN_ORDER_KRW = 5000
DUST_KEEP_MIN_KRW = 100
MAX_ENTRY_KRW = 10000
KRW_USE_RATIO = 0.88
ORDER_BUFFER_KRW = 500

AUTO_BUY = False
AUTO_BUY_START_HOUR = 0
AUTO_BUY_END_HOUR = 24
MAX_HOLDINGS = 1

# =========================================================
# BTC / 시장 필터
# =========================================================
BTC_FILTER_ON = True
BTC_CRASH_BLOCK_PCT = -2.0
BTC_STRONG_BLOCK_PCT = -2.8
BTC_MA_FILTER = True

REGIME_SIDEWAYS_MAX_ABS_PCT = 0.50
REGIME_WEAK_MAX_ABS_PCT = -0.70
REGIME_STRONG_UP_PCT = 1.0

# =========================================================
# 리스크 관리
# =========================================================
BASE_STOP_LOSS_PCT = -1.7
BASE_TP_PCT = 4.6
TIME_STOP_MIN_SEC = 480
TIME_STOP_MAX_SEC = 1080
BREAKEVEN_TRIGGER_PCT = 1.2
BREAKEVEN_FALLBACK_PCT = 0.15

BTC_REPORT_INTERVAL = 3600
STATUS_REPORT_INTERVAL = 1800

last_btc_report_time = 0
last_status_report_time = 0

# =========================================================
# 상태 저장
# =========================================================
active_positions = {}
pending_sells = {}
pending_buy_candidates = {}
paused_until = 0
pause_reason = ""
auto_pause_bypass_until = 0
auto_pause_reset_ignore_before = 0

# =========================================================
# 유틸
# =========================================================
def now_kst():
    return datetime.now(ZoneInfo(TIMEZONE))


def is_auto_time():
    return AUTO_BUY_START_HOUR <= now_kst().hour < AUTO_BUY_END_HOUR


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def fmt_price(x):
    try:
        x = float(x)
    except Exception:
        return "0"
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 100:
        return f"{x:,.1f}"
    if x >= 1:
        return f"{x:,.2f}"
    if x >= 0.1:
        return f"{x:,.3f}"
    if x >= 0.01:
        return f"{x:,.4f}"
    return f"{x:,.6f}"


def fmt_pct(x):
    try:
        return f"{float(x):.2f}%"
    except Exception:
        return "0.00%"


def send(msg: str):
    try:
        bot.send_message(chat_id=CHAT_ID, text=msg.strip())
    except Exception as e:
        print(f"[텔레그램 오류] {e}")


def send_startup_message():
    send(
        f"""
✅ 자동매매 봇 시작

버전: {BOT_VERSION}
자동매수: {'켜짐' if AUTO_BUY else '꺼짐'}
운영시간: {AUTO_BUY_START_HOUR}:00 ~ {AUTO_BUY_END_HOUR}:00
주문 최대금액: {MAX_ENTRY_KRW:,}원

현재 상태:
- /status 정상 응답
- /btc 정상 응답
- 기본 포지션 감시 사용
- 시간 정리(TIME_STOP) 사용
- 본절 보호 사용
"""
    )


# =========================================================
# 파일 I/O
# =========================================================
def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[JSON 저장 오류] {path} / {e}")


trade_logs = load_json_file(LOG_FILE, [])


def save_logs():
    save_json_file(LOG_FILE, trade_logs)


def add_log(item: dict):
    trade_logs.append(item)
    save_logs()


def save_positions():
    save_json_file(POSITIONS_FILE, active_positions)


def load_positions():
    global active_positions
    active_positions = load_json_file(POSITIONS_FILE, {})


def clear_position_file_if_empty():
    if not active_positions and os.path.exists(POSITIONS_FILE):
        try:
            os.remove(POSITIONS_FILE)
        except Exception:
            pass


def save_pending_sells():
    save_json_file(PENDING_SELLS_FILE, pending_sells)


def load_pending_sells():
    global pending_sells
    pending_sells = load_json_file(PENDING_SELLS_FILE, {})


def save_pending_buys():
    save_json_file(PENDING_BUYS_FILE, pending_buy_candidates)


def load_pending_buys():
    global pending_buy_candidates
    pending_buy_candidates = load_json_file(PENDING_BUYS_FILE, {})


# =========================================================
# 거래소 데이터
# =========================================================
def get_price(ticker: str) -> float:
    try:
        p = pybithumb.get_current_price(ticker)
        return -1 if p is None else float(p)
    except Exception:
        return -1


def get_ohlcv(ticker: str, interval: str = "minute3"):
    try:
        return pybithumb.get_ohlcv(ticker, interval=interval)
    except Exception:
        return None


def get_balance(ticker: str) -> float:
    try:
        bal = bithumb.get_balance(ticker)
        return float(bal[0] or 0)
    except Exception:
        return 0.0


def get_krw_balance():
    try:
        bal = bithumb.get_balance("BTC")
        if not bal or len(bal) < 4:
            return 0.0
        return float(bal[2] or 0)
    except Exception:
        return 0.0


# =========================================================
# 지표
# =========================================================
def ma(df, period):
    try:
        return float(df["close"].rolling(period).mean().iloc[-1])
    except Exception:
        return 0.0


def get_recent_change_pct(df, n=5):
    try:
        recent = df.tail(n)
        start = float(recent["close"].iloc[0])
        end = float(recent["close"].iloc[-1])
        if start <= 0:
            return 0.0
        return ((end - start) / start) * 100
    except Exception:
        return 0.0


# =========================================================
# BTC / 시장 상태
# =========================================================
def get_btc_df():
    for interval in ["minute60", "minute30", "minute10"]:
        df = get_ohlcv("BTC", interval)
        if df is not None and len(df) >= 20:
            return df, interval
    df = get_ohlcv("BTC", "day")
    if df is not None and len(df) >= 20:
        return df, "day"
    return None, None


def get_btc_market_state():
    df, _ = get_btc_df()
    if df is None:
        return True, "BTC 조회 실패지만 진행", 0.0
    price = get_price("BTC")
    if price <= 0:
        return True, "BTC 현재가 조회 실패지만 진행", 0.0
    recent_drop_pct = get_recent_change_pct(df, 4)
    ma20 = ma(df, 20)

    if recent_drop_pct <= BTC_STRONG_BLOCK_PCT:
        return False, f"BTC가 크게 빠지는 중이야 ({recent_drop_pct:.2f}%)", recent_drop_pct
    if recent_drop_pct <= BTC_CRASH_BLOCK_PCT:
        return False, f"BTC가 약해서 신규 진입을 쉬고 있어 ({recent_drop_pct:.2f}%)", recent_drop_pct
    if BTC_MA_FILTER and ma20 > 0 and price < ma20 * 0.992:
        return False, "BTC가 약한 자리라 신규 진입을 쉬고 있어", recent_drop_pct
    return True, "시장 분위기 무난", recent_drop_pct


def get_market_regime():
    df, label = get_btc_df()
    if df is None:
        return {"name": "UNKNOWN", "label": label or "unknown", "btc_change_pct": 0.0, "allow_auto_buy": True, "allow_breakout": True, "message": "시장 상태 판단 실패"}

    price = get_price("BTC")
    if price <= 0:
        return {"name": "UNKNOWN", "label": label or "unknown", "btc_change_pct": 0.0, "allow_auto_buy": True, "allow_breakout": True, "message": "시장 상태 판단 실패"}

    change_pct = get_recent_change_pct(df, 4)
    ma5v, ma20v = ma(df, 5), ma(df, 20)

    if change_pct <= BTC_STRONG_BLOCK_PCT:
        return {"name": "BLOCK", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": False, "allow_breakout": False, "message": "BTC 급락 구간이라 자동매수 쉬는 장"}
    if change_pct <= REGIME_WEAK_MAX_ABS_PCT or (ma20v > 0 and price < ma20v * 0.992):
        return {"name": "WEAK", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": True, "allow_breakout": False, "message": "약한 장"}
    if abs(change_pct) <= REGIME_SIDEWAYS_MAX_ABS_PCT:
        return {"name": "SIDEWAYS", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": True, "allow_breakout": False, "message": "횡보 장"}
    if change_pct >= REGIME_STRONG_UP_PCT and price >= ma5v and price >= ma20v:
        return {"name": "STRONG_UP", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": True, "allow_breakout": True, "message": "강한 상승 장"}
    return {"name": "NORMAL", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": True, "allow_breakout": True, "message": "무난한 장"}


def analyze_btc_flow():
    regime = get_market_regime()
    df, label = get_btc_df()
    if df is None:
        return "📊 BTC 리포트\nBTC 데이터를 불러오지 못했어."
    price = get_price("BTC")
    if price <= 0:
        return "📊 BTC 리포트\nBTC 현재가 조회 실패."
    change_pct = get_recent_change_pct(df, 4)
    ma5v, ma20v = ma(df, 5), ma(df, 20)

    if regime["name"] == "BLOCK":
        state = "🚨 강한 하락"
    elif regime["name"] == "WEAK":
        state = "⚠️ 약한 장"
    elif regime["name"] == "SIDEWAYS":
        state = "😐 횡보"
    elif regime["name"] == "STRONG_UP":
        state = "🔥 강한 상승"
    else:
        state = "👍 보통"

    loc = "단기 이동평균 위" if price >= ma5v and price >= ma20v else "단기 이동평균 아래/혼조"

    return f"""
📊 BTC 리포트 ({label} 기준)

💰 현재가: {fmt_price(price)}
📉 최근 변동률: {change_pct:.2f}%
📌 상태: {state}
📎 현재 위치: {loc}

한줄 해석:
{regime['message']}
"""


# =========================================================
# 자동 쉬기
# =========================================================
def should_pause_auto_buy_now():
    return False, ""


def reset_auto_pause_state(bypass_sec=900):
    global paused_until, pause_reason, auto_pause_bypass_until, auto_pause_reset_ignore_before
    now_ts = int(time.time())
    paused_until = 0
    pause_reason = ""
    auto_pause_bypass_until = now_ts + max(int(bypass_sec), 0)
    auto_pause_reset_ignore_before = now_ts


# =========================================================
# 매매 / 포지션 관리
# =========================================================
def sell_market_confirmed(ticker: str, reason_type: str, pos: dict, current_price: float, pnl_pct: float, held_sec: int = 0):
    try:
        before_balance = get_balance(ticker)
        if before_balance <= 0:
            active_positions.pop(ticker, None)
            save_positions()
            clear_position_file_if_empty()
            return False, "잔고가 없어"

        result = bithumb.sell_market_order(ticker, before_balance)

        add_log({
            "time": int(time.time()),
            "type": reason_type,
            "ticker": ticker,
            "strategy": pos.get("strategy", ""),
            "strategy_label": pos.get("strategy_label", ""),
            "entry": float(pos.get("entry_price", 0)),
            "exit": float(current_price),
            "pnl_pct": round(float(pnl_pct), 2),
            "held_sec": int(held_sec),
            "edge_score": pos.get("edge_score", 0),
            "max_profit_pct": pos.get("max_profit_pct", 0),
            "max_drawdown_pct": pos.get("max_drawdown_pct", 0),
        })

        active_positions.pop(ticker, None)
        pending_sells.pop(ticker, None)
        save_positions()
        save_pending_sells()
        clear_position_file_if_empty()
        return True, str(result)
    except Exception as e:
        return False, str(e)


def monitor_positions():
    for ticker, pos in list(active_positions.items()):
        try:
            current_price = get_price(ticker)
            if current_price <= 0:
                continue

            balance = get_balance(ticker)
            if balance <= 0:
                active_positions.pop(ticker, None)
                pending_sells.pop(ticker, None)
                save_positions()
                save_pending_sells()
                clear_position_file_if_empty()
                continue

            market_value = balance * current_price
            if market_value < MIN_ORDER_KRW:
                continue

            entry_price = float(pos.get("entry_price", 0))
            if entry_price <= 0:
                continue

            pnl_pct = ((current_price - entry_price) / entry_price) * 100
            held_sec = int(time.time() - int(pos.get("entered_at", time.time())))
            stop_price = entry_price * (1 + float(pos.get("stop_loss_pct", BASE_STOP_LOSS_PCT)) / 100)
            tp_price = entry_price * (1 + float(pos.get("take_profit_pct", BASE_TP_PCT)) / 100)

            changed = False
            if current_price > float(pos.get("peak_price", entry_price)):
                pos["peak_price"] = current_price
                changed = True

            if pnl_pct > float(pos.get("max_profit_pct", 0)):
                pos["max_profit_pct"] = round(float(pnl_pct), 2)
                changed = True

            if pnl_pct < float(pos.get("max_drawdown_pct", 0)):
                pos["max_drawdown_pct"] = round(float(pnl_pct), 2)
                changed = True

            if changed:
                save_positions()

            if current_price <= stop_price:
                ok, msg = sell_market_confirmed(ticker, "STOP", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"🚨 손절\n\n📊 {ticker}\n현재가: {fmt_price(current_price)}\n손익: {fmt_pct(pnl_pct)}")
                else:
                    send(f"⚠️ 손절 시도 실패\n\n📊 {ticker}\n사유: {msg}")
                continue

            if float(pos.get("max_profit_pct", 0)) >= BREAKEVEN_TRIGGER_PCT and pnl_pct <= BREAKEVEN_FALLBACK_PCT:
                ok, msg = sell_market_confirmed(ticker, "BREAKEVEN", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"🛡 본절 보호\n\n📊 {ticker}\n현재가: {fmt_price(current_price)}\n손익: {fmt_pct(pnl_pct)}")
                else:
                    send(f"⚠️ 본절 보호 실패\n\n📊 {ticker}\n사유: {msg}")
                continue

            if current_price >= tp_price:
                ok, msg = sell_market_confirmed(ticker, "TP", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"🎉 익절\n\n📊 {ticker}\n현재가: {fmt_price(current_price)}\n손익: {fmt_pct(pnl_pct)}")
                else:
                    send(f"⚠️ 익절 시도 실패\n\n📊 {ticker}\n사유: {msg}")
                continue

            if held_sec >= int(TIME_STOP_MAX_SEC) and pnl_pct < 0.8:
                ok, msg = sell_market_confirmed(ticker, "TIME_STOP", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"⏰ 시간 정리\n\n📊 {ticker}\n현재가: {fmt_price(current_price)}\n손익: {fmt_pct(pnl_pct)}\n보유시간: {held_sec}초")
                else:
                    send(f"⚠️ 시간 정리 실패\n\n📊 {ticker}\n사유: {msg}")
                continue

        except Exception as e:
            print(f"[monitor_positions 오류] {ticker} / {e}")


def recover_positions_from_exchange():
    return


def cleanup_pending_buy_candidates():
    changed = False
    now_ts = time.time()
    for ticker in list(pending_buy_candidates.keys()):
        if ticker in active_positions:
            pending_buy_candidates.pop(ticker, None)
            changed = True
            continue
        created_at = safe_float(pending_buy_candidates[ticker].get("created_at", 0))
        if created_at <= 0 or now_ts - created_at > 165:
            pending_buy_candidates.pop(ticker, None)
            changed = True
    if changed:
        save_pending_buys()


def check_pending_sells():
    return


# =========================================================
# 로그 / 통계
# =========================================================
CLOSE_TYPES = {"TP", "STOP", "TIME_STOP", "TRAIL_TP", "BREAKEVEN", "SCENARIO_FAIL", "EARLY_FAIL"}


def get_closed_logs_all():
    return [x for x in trade_logs if x.get("type") in CLOSE_TYPES]


def get_closed_logs_today():
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    result = []
    for x in trade_logs:
        if x.get("type") not in CLOSE_TYPES:
            continue
        ts = int(x.get("time", 0))
        d = datetime.fromtimestamp(ts, tz).date()
        if d == today:
            result.append(x)
    return result


def summarize_logs(logs):
    if not logs:
        return None
    total = len(logs)
    wins = len([x for x in logs if float(x.get("pnl_pct", 0)) > 0])
    losses = total - wins
    total_pnl = sum(float(x.get("pnl_pct", 0)) for x in logs)
    avg_pnl = total_pnl / total if total > 0 else 0.0
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total) * 100 if total > 0 else 0,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
    }


def summarize_by_strategy(logs):
    bucket = {}
    for x in logs:
        strategy = x.get("strategy", "UNKNOWN")
        bucket.setdefault(strategy, []).append(x)

    lines = []
    for strategy, items in sorted(bucket.items(), key=lambda kv: len(kv[1]), reverse=True):
        info = summarize_logs(items)
        lines.append(
            f"{strategy}\n- 거래: {info['total']}\n- 승률: {info['win_rate']:.2f}%\n- 평균: {info['avg_pnl']:.2f}%\n- 누적: {info['total_pnl']:.2f}%"
        )
    return lines


# =========================================================
# 텔레그램 명령
# =========================================================
def summary_command(update, context: CallbackContext):
    closes = get_closed_logs_all()
    info = summarize_logs(closes)
    if not info:
        send("📊 아직 종료된 거래가 없어")
        return
    send(
        f"""
📊 거래 요약

총 종료 거래: {info['total']}
익절/플러스 종료: {info['wins']}
손절/마이너스 종료: {info['losses']}
승률: {info['win_rate']:.2f}%
누적 수익률: {info['total_pnl']:.2f}%
평균 수익률: {info['avg_pnl']:.2f}%
"""
    )


def today_command(update, context: CallbackContext):
    today_closes = get_closed_logs_today()
    info = summarize_logs(today_closes)
    if not info:
        send("📊 오늘 종료된 거래가 아직 없어")
        return
    lines = [f"• {x.get('ticker','?')} / {x.get('type')} / {float(x.get('pnl_pct',0)):.2f}%" for x in today_closes[-10:]]
    send(
        f"""
📊 오늘 거래 요약

총 종료 거래: {info['total']}
익절/플러스 종료: {info['wins']}
손절/마이너스 종료: {info['losses']}
승률: {info['win_rate']:.2f}%
누적 수익률: {info['total_pnl']:.2f}%
평균 수익률: {info['avg_pnl']:.2f}%

최근 종료 거래
""" + "\n".join(lines)
    )


def summary_strategy_command(update, context: CallbackContext):
    closes = get_closed_logs_all()
    if not closes:
        send("📊 아직 종료된 거래가 없어")
        return
    send("📊 전략별 전체 결과\n\n" + "\n\n".join(summarize_by_strategy(closes)))


def today_strategy_command(update, context: CallbackContext):
    closes = get_closed_logs_today()
    if not closes:
        send("📊 오늘 종료된 거래가 아직 없어")
        return
    send("📊 전략별 오늘 결과\n\n" + "\n\n".join(summarize_by_strategy(closes)))


def btc_command(update, context: CallbackContext):
    send(analyze_btc_flow())


def reset_pause_command(update, context: CallbackContext):
    reset_auto_pause_state(bypass_sec=900)
    send(
        """
🛠 자동 쉬기 수동 해제

자동매수 쉬기 상태를 풀었어.
"""
    )


def scan_debug_command(update, context: CallbackContext):
    send("🔎 scan_debug는 현재 간단 복구본이라 상세 출력은 비활성화 상태야.")


def status_command(update, context: CallbackContext):
    parts = []

    paused, pause_msg = should_pause_auto_buy_now()
    if paused:
        parts.append("⏸ 자동매수 쉬는 중\n\n" + pause_msg)
    elif auto_pause_bypass_until and time.time() < auto_pause_bypass_until:
        remain = int(auto_pause_bypass_until - time.time())
        parts.append("🛠 자동 쉬기 수동 해제 적용 중\n\n" + f"{max(remain, 0)}초 남음")

    parts.append(f"⚙️ 자동매수: {'켜짐' if AUTO_BUY else '꺼짐'}")

    if active_positions:
        lines = []
        dust_lines = []
        for ticker, pos in active_positions.items():
            price = get_price(ticker)
            entry = float(pos.get("entry_price", 0))
            qty = float(pos.get("qty", 0))
            pnl = ((price - entry) / entry) * 100 if entry > 0 and price > 0 else 0
            held_min = int((time.time() - int(pos.get("entered_at", time.time()))) / 60)
            value = qty * price if price > 0 else 0
            line = (
                f"• {ticker} / 방식 {pos.get('strategy_label','?')} / 진입 {fmt_price(entry)} / 현재 {fmt_price(price)} "
                f"/ 수익률 {fmt_pct(pnl)} / 평가금액 {fmt_price(value)} / 보유 {held_min}분"
            )
            if value < MIN_ORDER_KRW:
                dust_lines.append(line + " / 상태 소액포지션 대기중")
            else:
                lines.append(line)

        if lines:
            parts.append("📌 현재 포지션\n\n" + "\n".join(lines))
        if dust_lines:
            parts.append("🪙 소액 포지션(최소 주문금액 미만)\n\n" + "\n".join(dust_lines))
    else:
        parts.append("📭 현재 보유 포지션 없음")

    if pending_buy_candidates:
        c_lines = []
        sorted_candidates = sorted(
            pending_buy_candidates.values(),
            key=lambda x: safe_float(x.get("edge_score", 0)),
            reverse=True
        )[:6]
        for item in sorted_candidates:
            age = int(time.time() - safe_float(item.get("created_at", 0)))
            c_lines.append(
                f"• {item.get('ticker','?')} / 방식 {item.get('strategy_label','?')} / 후보 유지 시간 {age}초 / 진입점수 {safe_float(item.get('edge_score', 0)):.2f}"
            )
        parts.append("🕒 관찰 후보\n\n" + "\n".join(c_lines))

    if pending_sells:
        p = ["⚠️ 매도 확인 대기중"]
        for ticker in pending_sells.keys():
            p.append(f"• {ticker}")
        parts.append("\n".join(p))

    send("\n\n".join(parts))


# =========================================================
# 텔레그램 업데이터
# =========================================================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CommandHandler("summary", summary_command))
dispatcher.add_handler(CommandHandler("today", today_command))
dispatcher.add_handler(CommandHandler("summary_strategy", summary_strategy_command))
dispatcher.add_handler(CommandHandler("today_strategy", today_strategy_command))
dispatcher.add_handler(CommandHandler("btc", btc_command))
dispatcher.add_handler(CommandHandler("reset_pause", reset_pause_command))
dispatcher.add_handler(CommandHandler("status", status_command))
dispatcher.add_handler(CommandHandler("scan_debug", scan_debug_command))
updater.start_polling(drop_pending_updates=True)

# =========================================================
# 시작
# =========================================================
load_positions()
load_pending_sells()
load_pending_buys()
recover_positions_from_exchange()
cleanup_pending_buy_candidates()
save_positions()
save_pending_sells()
save_pending_buys()
send_startup_message()

print(f"🚀 {BOT_VERSION} 실행 / {TIMEZONE}")

while True:
    now_ts = time.time()
    try:
        check_pending_sells()
        cleanup_pending_buy_candidates()
        monitor_positions()

        if now_ts - last_btc_report_time >= BTC_REPORT_INTERVAL:
            try:
                send(analyze_btc_flow())
            except Exception as e:
                print(f"[BTC 리포트 오류] {e}")
            last_btc_report_time = now_ts

        if now_ts - last_status_report_time >= STATUS_REPORT_INTERVAL:
            try:
                if active_positions or pending_sells or pending_buy_candidates:
                    status_command(None, None)
            except Exception as e:
                print(f"[상태 리포트 오류] {e}")
            last_status_report_time = now_ts

    except Exception as e:
        print(f"[메인 루프 오류] {e}")
        traceback.print_exc()

    time.sleep(LOOP_SLEEP)
