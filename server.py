import asyncio
import logging
import os
import statistics
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from aiohttp import web
from pocketoptionapi import PocketOption


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PO_SSID = os.getenv("PO_SSID", "").strip()
PORT = int(os.getenv("PORT", "8080"))
KYIV_TZ = ZoneInfo("Europe/Kyiv")

OTC_ASSETS = [
    # Основні валютні мажори та кроси (20)
    "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDUSD_otc", "USDCAD_otc",
    "USDCHF_otc", "EURJPY_otc", "GBPJPY_otc", "AUDCAD_otc", "EURGBP_otc",
    "NZDUSD_otc", "EURCHF_otc", "EURAUD_otc", "EURNZD_otc", "GBPAUD_otc",
    "GBPCAD_otc", "GBPCHF_otc", "AUDJPY_otc", "CADJPY_otc", "CHFJPY_otc",
    # Додаткові крос-пари та високодохідні OTC активи (8)
    "AUDNZD_otc", "CADCHF_otc", "NZDCAD_otc", "NZDCHF_otc", "NZDJPY_otc",
    "USDBRL_otc", "XAUUSD_otc", "XAGUSD_otc"
]
OTC_ASSET_LOOKUP = {s.lower() for s in OTC_ASSETS}

STATE = {
    "api": None,
    "assets": {},
    "candles": {},
    "ticks": {},  # Зберігаємо останні 15 живих тіків для миттєвого аналізу на 3 сек
    "priority_symbol": "EURUSD_otc",
    "connected": False
}


def ema(values, period):
    k = 2 / (period + 1)
    result = [values[0]]
    for price in values[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def calculate_rsi(closes, period=5):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = statistics.mean(gains)
    avg_loss = statistics.mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze_market(candles, recent_ticks):
    # Для запуску потрібно 14 хвилинних свічок (для EMA14, CCI, Stochastic, RSI)
    if not candles or len(candles) < 14:
        return {"status": "WARMING", "collected": len(candles), "needed": 14}

    sec_in_minute = int(time.time()) % 60
    # 1. ТАЙМІНГ: Не заходимо на останніх 3 секундах хвилини
    if sec_in_minute >= 57:
        return {"status": "WAIT_CANDLE", "wait_sec": 60 - sec_in_minute, "sec": sec_in_minute}

    opens = [float(x["open"]) for x in candles]
    highs = [float(x["high"]) for x in candles]
    lows = [float(x["low"]) for x in candles]
    closes = [float(x["close"]) for x in candles]

    # 2. ТРЕНДОВЕ ВІЯЛО EMA (3, 7, 14)
    ema3 = ema(closes, 3)
    ema7 = ema(closes, 7)
    ema14 = ema(closes, 14)

    bull_ema = ema3[-1] > ema7[-1] > ema14[-1] and ema3[-1] > ema3[-2]
    bear_ema = ema3[-1] < ema7[-1] < ema14[-1] and ema3[-1] < ema3[-2]

    # 3. ШВИДКИЙ RSI (період 5) — Захист від перекупленості/перепроданості
    rsi = calculate_rsi(closes, period=5)
    if rsi is None:
        return {"status": "FLAT", "price": closes[-1], "sec": sec_in_minute}

    # 4. STOCHASTIC OSCILLATOR (5, 3, 3)
    k_values = []
    for i in range(4, len(closes)):
        low_min = min(lows[i - 4:i + 1])
        high_max = max(highs[i - 4:i + 1])
        k_values.append(50.0 if high_max == low_min else ((closes[i] - low_min) / (high_max - low_min)) * 100)

    if len(k_values) < 3:
        return {"status": "FLAT", "price": closes[-1], "sec": sec_in_minute}

    stoch_k = statistics.mean(k_values[-2:])
    stoch_d = statistics.mean(k_values[-3:])

    # 5. CCI (період 7) — Сила виходу ціни із середнього діапазону
    tp = [(h + l + c) / 3 for h, l, c in zip(highs[-7:], lows[-7:], closes[-7:])]
    tp_sma = statistics.mean(tp)
    mean_dev = statistics.mean([abs(x - tp_sma) for x in tp])
    cci = (tp[-1] - tp_sma) / (0.015 * mean_dev) if mean_dev > 0 else 0

    # 6. АНТИ-ВІДКАТ ТА АНТИ-ВИСНАЖЕННЯ НА M1
    prev_bulls = sum(1 for i in range(-4, -1) if closes[i] > opens[i])
    prev_bears = sum(1 for i in range(-4, -1) if closes[i] < opens[i])

    curr_range = highs[-1] - lows[-1]
    if curr_range <= 0:
        return {"status": "FLAT", "price": closes[-1], "rsi": round(rsi, 1), "sec": sec_in_minute}

    body = abs(closes[-1] - opens[-1])
    body_ratio = body / curr_range
    upper_wick = highs[-1] - max(opens[-1], closes[-1])
    lower_wick = min(opens[-1], closes[-1]) - lows[-1]

    no_upper_wick = (upper_wick / curr_range) <= 0.25
    no_lower_wick = (lower_wick / curr_range) <= 0.25

    # 7 & 8. МІКРО-ІМПУЛЬС ТА ПРИСКОРЕННЯ ЖИВИХ ТІКІВ (3 тіки для миттєвого 3-секундного входу!)
    tick_bull_streak = False
    tick_bear_streak = False
    tick_accel_bull = False
    tick_accel_bear = False

    if len(recent_ticks) >= 3:
        t = recent_ticks[-3:]
        tick_bull_streak = t[2] > t[1] > t[0]
        tick_bear_streak = t[2] < t[1] < t[0]
        tick_accel_bull = (t[2] - t[1]) >= (t[1] - t[0]) > 0
        tick_accel_bear = (t[1] - t[2]) >= (t[0] - t[1]) > 0

    # ЗБІР УСІХ 8 ФІЛЬТРІВ ДЛЯ 3-СЕКУНДНОГО СНАЙПЕРСЬКОГО ВХОДУ
    call_checks = [
        bull_ema,
        52 <= rsi <= 75,
        stoch_k > stoch_d and 25 <= stoch_k <= 78,
        cci >= 45,
        prev_bulls < 3,
        closes[-1] > opens[-1] and body_ratio >= 0.35 and no_upper_wick,
        tick_bull_streak,
        tick_accel_bull
    ]

    put_checks = [
        bear_ema,
        25 <= rsi <= 48,
        stoch_k < stoch_d and 22 <= stoch_k <= 75,
        cci <= -45,
        prev_bears < 3,
        closes[-1] < opens[-1] and body_ratio >= 0.35 and no_lower_wick,
        tick_bear_streak,
        tick_accel_bear
    ]

    call_score = sum(call_checks)
    put_score = sum(put_checks)

    # Обов'язково має бути живий імпульс тіків (tick_streak) + мінімум 6 із 8 індикаторів!
    if call_score >= 6 and call_score > put_score and tick_bull_streak and prev_bulls < 3:
        return {
            "status": "SIGNAL",
            "direction": "CALL",
            "price": closes[-1],
            "rsi": round(rsi, 1),
            "score": f"{call_score}/8 (Тік-імпульс)",
            "sec": sec_in_minute
        }

    if put_score >= 6 and put_score > call_score and tick_bear_streak and prev_bears < 3:
        return {
            "status": "SIGNAL",
            "direction": "PUT",
            "price": closes[-1],
            "rsi": round(rsi, 1),
            "score": f"{put_score}/8 (Тік-імпульс)",
            "sec": sec_in_minute
        }

    return {"status": "FLAT", "price": closes[-1], "rsi": round(rsi, 1), "sec": sec_in_minute}


def parse_assets(raw_assets):
    result = {}
    if not isinstance(raw_assets, dict):
        return result
    for sym, item in raw_assets.items():
        if isinstance(item, dict) and str(sym).lower() in OTC_ASSET_LOOKUP:
            payout = item.get("payout")
            is_avail = item.get("is_available", True)
            try:
                p_val = float(payout) if payout is not None else 0
            except Exception:
                p_val = 0
            result[str(sym)] = {"payout": p_val, "is_open": bool(is_avail)}
    return result


    for item in items:
        if isinstance(item, dict):
            sym = item.get("symbol") or item.get("name") or item.get("asset")
            payout = item.get("payout") or item.get("profit")
            is_open = item.get("is_open", item.get("open", True))
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            sym = item[1]
            payout = item[5] if len(item) > 5 else None
            is_open = bool(item[14]) if len(item) > 14 else True
        else:
            continue

        if sym and str(sym).lower() in OTC_ASSET_LOOKUP:
            try:
                p_val = float(payout) if payout is not None else 0
            except Exception:
                p_val = 0
            result[str(sym)] = {"payout": p_val, "is_open": bool(is_open)}
    return result


async def market_worker():
    subscribed = set()
    while True:
        api = STATE["api"]
        if api is None or not api.check_connect():
            STATE["connected"] = False
            subscribed.clear()
            if not PO_SSID:
                await asyncio.sleep(5)
                continue
            try:
                new_api = PocketOption(PO_SSID)
                ok, err = await asyncio.to_thread(new_api.connect)
                if ok:
                    STATE["api"] = new_api
                    STATE["connected"] = True
                    logging.info("CONNECTED TO POCKET OPTION")
                else:
                    logging.error("PO AUTH ERROR: %s", err)
            except Exception as e:
                logging.error("CONNECT ERROR: %s", e)

            await asyncio.sleep(3)
            continue


        try:
            raw = await asyncio.to_thread(api.get_assets)
            parsed = parse_assets(raw)
            if parsed:
                STATE["assets"] = parsed
        except Exception:
            pass

        prio = STATE["priority_symbol"]
        open_syms = [s for s, info in STATE["assets"].items() if info.get("is_open")]
        symbols = ([prio] if prio in open_syms else []) + [s for s in open_syms if s != prio]

        for sym in symbols:
            try:
                if sym not in subscribed:
                    if await asyncio.to_thread(api.subscribe, sym, 60):
                        subscribed.add(sym)
                    await asyncio.sleep(0.05)
                    continue

                ticks = await asyncio.to_thread(api.get_realtime_ticks, sym, 5)
                if not ticks:
                    continue

                ts, price = ticks[-1]
                price = float(price)

                # Оновлюємо буфер живих тіків для миттєвого 3-секундного аналізу
                t_hist = STATE["ticks"].setdefault(sym, [])
                if not t_hist or t_hist[-1] != price:
                    t_hist.append(price)
                    if len(t_hist) > 15:
                        STATE["ticks"][sym] = t_hist[-15:]

                # Оновлюємо хвилинні свічки M1
                bucket = int(ts // 60) * 60
                hist = STATE["candles"].setdefault(sym, [])

                if not hist or hist[-1]["time"] < bucket:
                    hist.append({"time": bucket, "open": price, "high": price, "low": price, "close": price})
                    if len(hist) > 25:
                        STATE["candles"][sym] = hist[-25:]
                else:
                    cur = hist[-1]
                    cur["high"] = max(cur["high"], price)
                    cur["low"] = min(cur["low"], price)
                    cur["close"] = price
            except Exception:
                pass

        await asyncio.sleep(0.35)


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


async def handle_status(request):
    symbol = request.query.get("symbol", "EURUSD_otc")
    STATE["priority_symbol"] = symbol

    candles = STATE["candles"].get(symbol, [])
    recent_ticks = STATE["ticks"].get(symbol, [])
    analysis = analyze_market(candles, recent_ticks)

    sorted_pairs = sorted(
        [{"symbol": k, "payout": int(v["payout"])} for k, v in STATE["assets"].items() if v.get("is_open")],
        key=lambda x: x["payout"],
        reverse=True
    )

    return web.json_response({
        "connected": STATE["connected"],
        "symbol": symbol,
        "time": datetime.now(KYIV_TZ).strftime("%H:%M:%S"),
        "analysis": analysis,
        "pairs": sorted_pairs
    })


async def priority_tick_worker():
    """Окремий надшвидкий потік (0.15с) виключно для поточної обраної пари"""
    while True:
        api = STATE["api"]
        prio = STATE["priority_symbol"]
        if api is not None and STATE["connected"] and prio:
            try:
                ticks = await asyncio.to_thread(api.get_realtime_ticks, prio, 5)
                if ticks:
                    ts, price = ticks[-1]
                    price = float(price)

                    t_hist = STATE["ticks"].setdefault(prio, [])
                    if not t_hist or t_hist[-1] != price:
                        t_hist.append(price)
                        if len(t_hist) > 15:
                            STATE["ticks"][prio] = t_hist[-15:]

                    bucket = int(ts // 60) * 60
                    hist = STATE["candles"].setdefault(prio, [])
                    if not hist or hist[-1]["time"] < bucket:
                        hist.append({"time": bucket, "open": price, "high": price, "low": price, "close": price})
                        if len(hist) > 25:
                            STATE["candles"][prio] = hist[-25:]
                    else:
                        cur = hist[-1]
                        cur["high"] = max(cur["high"], price)
                        cur["low"] = min(cur["low"], price)
                        cur["close"] = price
            except Exception:
                pass
        await asyncio.sleep(0.15)


async def on_startup(app):
    app["worker"] = asyncio.create_task(market_worker())
    app["prio_worker"] = asyncio.create_task(priority_tick_worker())


async def main():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/status", handle_status)
    app.on_startup.append(on_startup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("WEB ENGINE RUNNING ON PORT %s", PORT)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
