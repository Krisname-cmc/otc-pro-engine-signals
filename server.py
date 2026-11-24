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
    "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDUSD_otc", "USDCAD_otc",
    "USDCHF_otc", "EURJPY_otc", "GBPJPY_otc", "AUDCAD_otc", "EURGBP_otc",
    "NZDUSD_otc", "EURCHF_otc", "EURAUD_otc", "EURNZD_otc", "GBPAUD_otc",
    "GBPCAD_otc", "GBPCHF_otc", "AUDJPY_otc", "CADJPY_otc", "CHFJPY_otc",
    "AUDNZD_otc", "CADCHF_otc", "NZDCAD_otc", "NZDCHF_otc", "NZDJPY_otc",
    "USDBRL_otc", "USDCLP_otc", "USDCNH_otc", "USDCOP_otc", "USDIDR_otc",
    "USDINR_otc", "USDMXN_otc", "USDPHP_otc", "USDTHB_otc", "USDVND_otc",
    "XAUUSD_otc", "XAGUSD_otc", "UKBrent_otc", "USCrude_otc", "BTCUSD_otc"
]

OTC_ASSET_LOOKUP = {s.lower() for s in OTC_ASSETS}

STATE = {
    "api": None,
    "assets": {},
    "candles": {},
    "ticks": {},
    "priority_symbol": "EURUSD_otc",
    "connected": False
}


def ema(values, period):
    k = 2 / (period + 1)
    result = [values[0]]
    for price in values[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def calculate_rsi(closes, period=14):
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


def calculate_stochastic(highs, lows, closes, k_period=14, smooth_k=3, smooth_d=3):
    if len(closes) < k_period + smooth_k + smooth_d:
        return None, None, None
    raw_k = []
    for i in range(k_period - 1, len(closes)):
        low_min = min(lows[i - k_period + 1:i + 1])
        high_max = max(highs[i - k_period + 1:i + 1])
        raw_k.append(50.0 if high_max == low_min else ((closes[i] - low_min) / (high_max - low_min)) * 100)

    k_line = []
    for i in range(smooth_k - 1, len(raw_k)):
        k_line.append(statistics.mean(raw_k[i - smooth_k + 1:i + 1]))

    if len(k_line) < smooth_d + 1:
        return None, None, None

    d_now = statistics.mean(k_line[-smooth_d:])
    return k_line[-1], k_line[-2], d_now


def analyze_market(candles, recent_ticks):
    """ВАРІАНТ Б: Спокійний трендовий аналіз на M5 (EMA 3/5/8 + Pullback Stochastic + RSI 6)"""
    if not candles or len(candles) < 8:
        return {"status": "WARMING", "collected": len(candles), "needed": 8}

    sec_in_5m = int(time.time()) % 300
    # Спокійне вікно входу в перші 2.5 хвилини 5-хвилинної свічки (:05 – :150 сек)
    if sec_in_5m < 5 or sec_in_5m > 150:
        wait = (5 - sec_in_5m) if sec_in_5m < 5 else (300 - sec_in_5m)
        return {
            "status": "WAIT_CANDLE",
            "wait_sec": wait,
            "sec": sec_in_5m % 60
        }

    opens = [float(x["open"]) for x in candles]
    highs = [float(x["high"]) for x in candles]
    lows = [float(x["low"]) for x in candles]
    closes = [float(x["close"]) for x in candles]

    # 1. ВІЯЛО ТРЕНДУ EMA (3, 5, 8)
    ema9 = ema(closes, 3)
    ema21 = ema(closes, 5)
    ema50 = ema(closes, min(8, len(closes)))

    uptrend = ema9[-1] > ema21[-1] > ema50[-1] and ema21[-1] > ema21[-3]
    downtrend = ema9[-1] < ema21[-1] < ema50[-1] and ema21[-1] < ema21[-3]

    trend_label = "UP-TREND" if uptrend else ("DOWN-TREND" if downtrend else "FLAT")

    # 2. ВІДКАТ ТА РОЗВОРОТ ПО STOCHASTIC (5, 2, 2)
    k_now, k_prev, d_now = calculate_stochastic(highs, lows, closes, 5, 2, 2)
    if k_now is None:
        return {"status": "FLAT", "price": closes[-1], "trend": trend_label, "sec": sec_in_5m % 60}

    pullback_buy = (k_prev <= 42) and (k_now > k_prev) and (k_now > d_now)
    pullback_sell = (k_prev >= 58) and (k_now < k_prev) and (k_now < d_now)

    # 3. ПІДТВЕРДЖЕННЯ СИЛИ ТРЕНДУ RSI (6)
    rsi14 = calculate_rsi(closes, period=6)
    if rsi14 is None:
        return {"status": "FLAT", "price": closes[-1], "trend": trend_label, "sec": sec_in_5m % 60}

    # 4. ПІДТВЕРДЖЕННЯ ПОТОЧНОЇ СВІЧКИ ТА ТІКА
    curr_green = closes[-1] > opens[-1] and closes[-1] > ema21[-1]
    curr_red = closes[-1] < opens[-1] and closes[-1] < ema21[-1]
    tick_up = len(recent_ticks) >= 2 and recent_ticks[-1] >= recent_ticks[-2]
    tick_down = len(recent_ticks) >= 2 and recent_ticks[-1] <= recent_ticks[-2]

    if uptrend and pullback_buy and (50 <= rsi14 <= 68) and curr_green and tick_up:
        rec_exp = "5m" if sec_in_5m <= 60 else "10m"
        return {
            "status": "SIGNAL",
            "direction": "CALL",
            "rec_exp": rec_exp,
            "price": closes[-1],
            "rsi": round(rsi14, 1),
            "stoch": round(k_now, 1),
            "trend": "UP-TREND",
            "sec": sec_in_5m % 60
        }

    if downtrend and pullback_sell and (32 <= rsi14 <= 50) and curr_red and tick_down:
        rec_exp = "5m" if sec_in_5m <= 60 else "10m"
        return {
            "status": "SIGNAL",
            "direction": "PUT",
            "rec_exp": rec_exp,
            "price": closes[-1],
            "rsi": round(rsi14, 1),
            "stoch": round(k_now, 1),
            "trend": "DOWN-TREND",
            "sec": sec_in_5m % 60
        }

    return {
        "status": "FLAT",
        "price": closes[-1],
        "rsi": round(rsi14, 1),
        "stoch": round(k_now, 1),
        "trend": trend_label,
        "sec": sec_in_5m % 60
    }

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


def parse_history_candles(candles_raw):
    formatted = []
    if not candles_raw:
        return formatted
    for c in candles_raw[-55:]:
        try:
            if isinstance(c, dict):
                t = int(c.get("time") or c.get("timestamp") or 0)
                o = float(c.get("open", 0))
                h = float(c.get("high", 0))
                l = float(c.get("low", 0))
                cl = float(c.get("close", 0))
            elif isinstance(c, (list, tuple)) and len(c) >= 5:
                t = int(c[0])
                o = float(c[1])
                cl = float(c[2])
                h = float(c[3])
                l = float(c[4])
            else:
                continue
            if o > 0 and cl > 0:
                formatted.append({"time": t, "open": o, "high": h, "low": l, "close": cl})
        except Exception:
            continue
    return formatted


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


async def fetch_symbol_data(api, sym):
    """Швидкий збір 5-хвилинних свічок (period=300) без таймаутів"""
    sub_set = STATE.setdefault("subscribed", set())
    if sym not in sub_set:
        await asyncio.to_thread(api.subscribe, sym, 300)
        sub_set.add(sym)

    ticks = await asyncio.to_thread(api.get_realtime_ticks, sym, 5)
    if ticks:
        ts, price = ticks[-1]
        price = float(price)

        t_hist = STATE["ticks"].setdefault(sym, [])
        if not t_hist or t_hist[-1] != price:
            t_hist.append(price)
            if len(t_hist) > 15:
                STATE["ticks"][sym] = t_hist[-15:]

        bucket = int(ts // 300) * 300
        hist = STATE["candles"].setdefault(sym, [])
        if not hist or hist[-1]["time"] < bucket:
            hist.append({"time": bucket, "open": price, "high": price, "low": price, "close": price})
            if len(hist) > 55:
                STATE["candles"][sym] = hist[-55:]
        else:
            cur = hist[-1]
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price


async def market_worker():
    while True:
        api = STATE["api"]
        if api is None or not STATE["connected"]:
            STATE["connected"] = False
            if not PO_SSID:
                await asyncio.sleep(5)
                continue
            try:
                if api is not None:
                    try:
                        await asyncio.to_thread(api.disconnect_websocket)
                    except Exception:
                        pass
                new_api = PocketOption(PO_SSID)
                ok, err = await asyncio.to_thread(new_api.connect)
                if ok:
                    STATE["api"] = new_api
                    STATE["connected"] = True
                    STATE["subscribed"] = set()
                    logging.info("CONNECTED TO POCKET OPTION (M5 ENGINE OK)")
                else:
                    STATE["connected"] = False
                    logging.error("PO AUTH ERROR: %s", err)
            except Exception as e:
                logging.error("CONNECT ERROR: %s", e)

            await asyncio.sleep(10)
            continue

        if not api.check_connect():
            STATE["connected"] = False
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
        symbols = [s for s in open_syms if s != prio]

        for sym in symbols:
            if not api.check_connect():
                STATE["connected"] = False
                break
            try:
                await fetch_symbol_data(api, sym)
            except Exception:
                pass
            await asyncio.sleep(0.25)


async def priority_tick_worker():
    while True:
        api = STATE["api"]
        prio = STATE["priority_symbol"]
        if api is not None and STATE["connected"] and api.check_connect() and prio:
            try:
                await fetch_symbol_data(api, prio)
            except Exception:
                pass
        await asyncio.sleep(0.35)


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
    logging.info("WEB ENGINE M5 RUNNING ON PORT %s", PORT)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
