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
    if not candles or len(candles) < 10:
        return {"status": "WARMING", "collected": len(candles), "needed": 10}

    sec_in_minute = int(time.time()) % 60
    # Входимо на старті та в першій половині активного руху (з 2-ї по 45-ту секунду)
    if sec_in_minute < 2 or sec_in_minute > 45:
        return {"status": "WAIT_CANDLE", "wait_sec": (2 - sec_in_minute) if sec_in_minute < 2 else (60 - sec_in_minute), "sec": sec_in_minute}

    opens = [float(x["open"]) for x in candles]
    highs = [float(x["high"]) for x in candles]
    lows = [float(x["low"]) for x in candles]
    closes = [float(x["close"]) for x in candles]

    # 1. ШВИДКИЙ ТРЕНД EMA (3, 6, 10)
    ema3 = ema(closes, 3)
    ema6 = ema(closes, 6)
    ema10 = ema(closes, 10)

    bull_ema = ema3[-1] > ema6[-1] and ema3[-1] > ema3[-2] and closes[-1] > ema10[-1]
    bear_ema = ema3[-1] < ema6[-1] and ema3[-1] < ema3[-2] and closes[-1] < ema10[-1]

    # 2. ШВИДКИЙ RSI (період 5) — Свіжий імпульс без перегріву
    rsi = calculate_rsi(closes, period=5)
    if rsi is None:
        return {"status": "FLAT", "price": closes[-1], "sec": sec_in_minute}

    # 3. STOCHASTIC OSCILLATOR (5, 3, 3)
    k_values = []
    for i in range(4, len(closes)):
        low_min = min(lows[i - 4:i + 1])
        high_max = max(highs[i - 4:i + 1])
        k_values.append(50.0 if high_max == low_min else ((closes[i] - low_min) / (high_max - low_min)) * 100)

    if len(k_values) < 3:
        return {"status": "FLAT", "price": closes[-1], "sec": sec_in_minute}

    stoch_k = statistics.mean(k_values[-2:])
    stoch_d = statistics.mean(k_values[-3:])

    # 4. АНТИ-ВИСНАЖЕННЯ (Не заходимо, якщо вже було 3 свічки одного кольору поспіль)
    prev_bulls = sum(1 for i in range(-4, -1) if closes[i] > opens[i])
    prev_bears = sum(1 for i in range(-4, -1) if closes[i] < opens[i])

    # 5. ЗАХИСТ ВІД ЗАПІЗНІЛОГО ВХОДУ:
    # Ловимо свічку на СТАРТІ (тіло 15%..55% діапазону), а якщо свічка вже роздулася > 55% — НЕ ВХОДИМО!
    curr_range = highs[-1] - lows[-1]
    if curr_range <= 0:
        return {"status": "FLAT", "price": closes[-1], "rsi": round(rsi, 1), "sec": sec_in_minute}

    body = abs(closes[-1] - opens[-1])
    body_ratio = body / curr_range
    upper_wick = highs[-1] - max(opens[-1], closes[-1])
    lower_wick = min(opens[-1], closes[-1]) - lows[-1]

    early_bull_candle = closes[-1] > opens[-1] and (0.15 <= body_ratio <= 0.55) and (upper_wick / curr_range) <= 0.25
    early_bear_candle = closes[-1] < opens[-1] and (0.15 <= body_ratio <= 0.55) and (lower_wick / curr_range) <= 0.25

    # 6. ЖИВИЙ ТІК У НАПРЯМКУ РУХУ
    tick_up = len(recent_ticks) >= 2 and recent_ticks[-1] > recent_ticks[-2]
    tick_down = len(recent_ticks) >= 2 and recent_ticks[-1] < recent_ticks[-2]

    call_checks = [
        bull_ema,
        50 <= rsi <= 70,
        stoch_k > stoch_d and 20 <= stoch_k <= 75,
        prev_bulls < 3,
        early_bull_candle,
        tick_up
    ]

    put_checks = [
        bear_ema,
        30 <= rsi <= 50,
        stoch_k < stoch_d and 25 <= stoch_k <= 80,
        prev_bears < 3,
        early_bear_candle,
        tick_down
    ]

    if all(call_checks):
        return {
            "status": "SIGNAL",
            "direction": "CALL",
            "price": closes[-1],
            "rsi": round(rsi, 1),
            "score": "6/6 (Старт руху)",
            "sec": sec_in_minute
        }

    if all(put_checks):
        return {
            "status": "SIGNAL",
            "direction": "PUT",
            "price": closes[-1],
            "rsi": round(rsi, 1),
            "score": "6/6 (Старт руху)",
            "sec": sec_in_minute
        }

    return {"status": "FLAT", "price": closes[-1], "rsi": round(rsi, 1), "sec": sec_in_minute}


    body = abs(closes[-1] - opens[-1])
    body_ratio = body / curr_range
    upper_wick = highs[-1] - max(opens[-1], closes[-1])
    lower_wick = min(opens[-1], closes[-1]) - lows[-1]

    no_upper_wick = (upper_wick / curr_range) <= 0.25
    no_lower_wick = (lower_wick / curr_range) <= 0.25

    # 7 & 8. ВИПЕРЕДЖУВАЛЬНИЙ ТРИГЕР НА 1-МУ КРОЦІ ІМПУЛЬСУ (БЕЗ ЗАПІЗНЕННЯ!)
    instant_bull_tick = False
    instant_bear_tick = False
    not_exhausted_bull = True
    not_exhausted_bear = True

    if len(recent_ticks) >= 2:
        # Сигнал спалахує миттєво на ПЕРШОМУ ж кроці ціни у бік тренду!
        instant_bull_tick = recent_ticks[-1] > recent_ticks[-2]
        instant_bear_tick = recent_ticks[-1] < recent_ticks[-2]

    if len(recent_ticks) >= 5:
        t5 = recent_ticks[-5:]
        # Якщо вже було 4 тіки поспіль без зупинки — імпульс запізнілий, блокуємо!
        not_exhausted_bull = not (t5 > t5 > t5 > t5 > t5)
        not_exhausted_bear = not (t5 < t5 < t5 < t5 < t5)

    # ЗБІР УСІХ 8 ФІЛЬТРІВ ДЛЯ МИТТЄВОГО 3-СЕКУНДНОГО ВХОДУ
    call_checks = [
        bull_ema,
        52 <= rsi <= 74,
        stoch_k > stoch_d and 25 <= stoch_k <= 76,
        cci >= 40,
        prev_bulls < 3,
        closes[-1] > opens[-1] and body_ratio >= 0.30 and no_upper_wick,
        instant_bull_tick,
        not_exhausted_bull
    ]

    put_checks = [
        bear_ema,
        26 <= rsi <= 48,
        stoch_k < stoch_d and 24 <= stoch_k <= 75,
        cci <= -40,
        prev_bears < 3,
        closes[-1] < opens[-1] and body_ratio >= 0.30 and no_lower_wick,
        instant_bear_tick,
        not_exhausted_bear
    ]

    call_score = sum(call_checks)
    put_score = sum(put_checks)

    # Вхід дозволяється ТІЛЬКИ на свіжому 1-му тіку (not_exhausted) + мінімум 6 із 8 індикаторів!
    call_score = sum(call_checks)
    put_score = sum(put_checks)

    if call_score == 7:
        return {"status": "SIGNAL", "direction": "CALL", "price": closes[-1], "rsi": round(rsi, 1), "score": "7/7", "sec": sec_in_minute}
    if put_score == 7:
        return {"status": "SIGNAL", "direction": "PUT", "price": closes[-1], "rsi": round(rsi, 1), "score": "7/7", "sec": sec_in_minute}

    if call_score == 6 and call_checks[0] and call_checks[6]:
        return {"status": "PREPARE", "direction": "CALL", "price": closes[-1], "rsi": round(rsi, 1), "score": "6/7", "sec": sec_in_minute}
    if put_score == 6 and put_checks[0] and put_checks[6]:
        return {"status": "PREPARE", "direction": "PUT", "price": closes[-1], "rsi": round(rsi, 1), "score": "6/7", "sec": sec_in_minute}

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
