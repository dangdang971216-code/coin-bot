# ================= 공격형 설정 =================

MIN_TP_GAP = 3.0        # 최소 수익폭 %
MAX_ENTRY_GAP = 0.8     # 진입가와 현재가 차이 %
MIN_VOL_RATIO = 0.6     # 거래량 최소 기준
MIN_ENTRY_SCORE = 2     # 자동매수 최소 점수

# ================= 핵심 변경 부분 =================

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

    rebound = detect_rebound(df)

    score = 0
    entry_score = 0
    reasons = []
    warnings = []

    # ===== 지지선 =====
    if support * 0.97 <= price <= support * 1.03:
        score += 2
        entry_score += 1
        reasons.append("- 지지선 근처")

    # ===== 추세 =====
    if ma5 > ma10:
        score += 1
        entry_score += 1
        reasons.append("- 상승 흐름 시작")

    # ===== RSI =====
    if 30 <= rsi <= 60:
        score += 1
        reasons.append("- 과열 아님")

    # ===== 거래량 필터 (강화됨) =====
    if vol_ratio >= MIN_VOL_RATIO:
        entry_score += 2
        reasons.append(f"- 거래량 좋음 ({vol_ratio:.2f})")
    else:
        return None  # ❗ 약하면 그냥 제거

    # ===== 반등 필수 =====
    if rebound:
        entry_score += 2
        reasons.append("- 반등 시작")
    else:
        return None  # ❗ 반등 없으면 제거

    # ===== 가격 계산 =====
    entry = support * 1.01
    stop = support * 0.97
    tp = entry * 1.035  # 공격형 → 3.5%

    entry_gap_pct = ((price - entry) / entry) * 100
    tp_gap_pct = ((tp - price) / price) * 100

    # ===== 핵심 필터 =====
    if tp_gap_pct < MIN_TP_GAP:
        return None

    if entry_gap_pct > MAX_ENTRY_GAP:
        return None

    qty = int(FIXED_ENTRY_KRW / entry)
    if qty <= 0:
        return None

    # ===== 상태 분류 =====
    if score >= 3 and entry_score >= MIN_ENTRY_SCORE:
        status = "🔥 강한 진입"
    elif score >= 2:
        status = "⚡ 단타"
    else:
        return None

    return {
        "ticker": ticker,
        "price": price,
        "entry": entry,
        "stop": stop,
        "tp": tp,
        "qty": qty,
        "status": status,
        "entry_score": entry_score,
        "tp_gap_pct": tp_gap_pct,
        "entry_gap_pct": entry_gap_pct,
        "reason": "\n".join(reasons)
    }

# ================= 자동매수 조건 =================

def try_auto_buy(coin):
    if active_positions:
        return False, "이미 보유중"

    if coin["status"] != "🔥 강한 진입":
        return False, "강한 진입 아님"

    if coin["tp_gap_pct"] < MIN_TP_GAP:
        return False, "수익폭 부족"

    if coin["entry_gap_pct"] > MAX_ENTRY_GAP:
        return False, "늦은 진입"

    if coin["entry_score"] < MIN_ENTRY_SCORE:
        return False, "점수 부족"

    try:
        result = bithumb.buy_market_order(coin["ticker"], coin["qty"])
        time.sleep(1)

        active_positions[coin["ticker"]] = coin

        send(f"🤖 자동매수\n{coin['ticker']}")
        return True, "완료"
    except Exception as e:
        return False, str(e)

# ================= 반복 알림 개선 =================

def should_send_alert(coin, now_ts):
    ticker = coin["ticker"]

    if ticker not in recent_alerts:
        return True

    prev = recent_alerts[ticker]

    # 점수 상승했을 때만 다시 알림
    if coin["entry_score"] > prev.get("entry_score", 0):
        return True

    return False
