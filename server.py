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


def calculate_psar(highs, lows, step=0.02, max_step=0.2):
    length = len(highs)
    if length < 5:
        return None, None
    psar = [lows[0]]
    bull = True
    af = step
    ep = highs[0]

    for i in range(1, length):
        prev_psar = psar[-1]
        if bull:
            curr_psar = prev_psar + af * (ep - prev_psar)
            curr_psar = min(curr_psar, lows[i - 1], lows[max(0, i - 2)])
            if lows[i] < curr_psar:
                bull = False
                curr_psar = ep
                ep = lows[i]
                af = step
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + step, max_step)
        else:
            curr_psar = prev_psar + af * (ep - prev_psar)
            curr_psar = max(curr_psar, highs[i - 1], highs[max(0, i - 2)])
            if highs[i] > curr_psar:
                bull = True
                curr_psar = ep
                ep = highs[i]
                af = step
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + step, max_step)
        psar.append(curr_psar)
    return psar[-1], bull


def analyze_market(candles, recent_ticks):
    if not candles or len(candles) < 14:
        return {"status": "WARMING", "collected": len(candles), "needed": 14}

    sec_in_minute = int(time.time()) % 60
    # Входимо на початку та в середині хвилинної свічки (з 2-ї по 45-ту секунду)
    if sec_in_minute < 2 or sec_in_minute > 45:
        return {
            "status": "WAIT_CANDLE",
            "wait_sec": (2 - sec_in_minute) if sec_in_minute < 2 else (60 - sec_in_minute),
            "sec": sec_in_minute
        }

    opens = [float(x["open"]) for x in candles]
    highs = [float(x["high"]) for x in candles]
    lows = [float(x["low"]) for x in candles]
    closes = [float(x["close"]) for x in candles]

    # 1. PARABOLIC SAR (0.02, 0.2) — Напрямок хвилі
    psar_val, psar_bull = calculate_psar(highs, lows, step=0.02, max_step=0.2)
    if psar_val is None:
        return {"status": "FLAT", "price": closes[-1], "sec": sec_in_minute}

    sar_buy = psar_bull and closes[-1] > psar_val
    sar_sell = (not psar_bull) and closes[-1] < psar_val

    # 2. ТРЕНД EMA (3 та 10)
    ema3 = ema(closes, 3)
    ema10 = ema(closes, 10)
    bull_ema = ema3[-1] > ema10[-1] and ema3[-1] > ema3[-2]
    bear_ema = ema3[-1] < ema10[-1] and ema3[-1] < ema3[-2]

    # 3. KELTNER CHANNEL (EMA 10, ATR 10) — Захист від входу на піку
    tr_list = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(-10, 0)
    ]
    atr10 = statistics.mean(tr_list)
    kc_upper = ema10[-1] + (1.5 * atr10)
    kc_lower = ema10[-1] - (1.5 * atr10)

    in_bull_keltner = (atr10 > 0) and (ema10[-1] < closes[-1] < kc_upper)
    in_bear_keltner = (atr10 > 0) and (kc_lower < closes[-1] < ema10[-1])

    # 4. RSI (період 5) + ЖИВИЙ ТІК
    rsi = calculate_rsi(closes, period=5)
    if rsi is None:
        return {"status": "FLAT", "price": closes[-1], "sec": sec_in_minute}

    tick_up = len(recent_ticks) >= 2 and recent_ticks[-1] >= recent_ticks[-2]
    tick_down = len(recent_ticks) >= 2 and recent_ticks[-1] <= recent_ticks[-2]

    # Анти-виснаження (не входимо після 3 свічок одного кольору підряд)
    prev_bulls = sum(1 for i in range(-4, -1) if closes[i] > opens[i])
    prev_bears = sum(1 for i in range(-4, -1) if closes[i] < opens[i])

    # ЧІТКИЙ ВХІД ПО 4 КЛЮЧОВИХ ФІЛЬТРАХ (Експірація 1m)
    if sar_buy and bull_ema and in_bull_keltner and (50 <= rsi <= 74) and tick_up and prev_bulls < 3:
        return {
            "status": "SIGNAL",
            "direction": "CALL",
            "rec_exp": "1m",
            "price": closes[-1],
            "rsi": round(rsi, 1),
            "sar": "BUY",
            "score": "4/4 (SAR+Keltner+RSI)",
            "sec": sec_in_minute
        }

    if sar_sell and bear_ema and in_bear_keltner and (26 <= rsi <= 50) and tick_down and prev_bears < 3:
        return {
            "status": "SIGNAL",
            "direction": "PUT",
            "rec_exp": "1m",
            "price": closes[-1],
            "rsi": round(rsi, 1),
            "sar": "SELL",
            "score": "4/4 (SAR+Keltner+RSI)",
            "sec": sec_in_minute
        }

    return {
        "status": "FLAT",
        "price": closes[-1],
        "rsi": round(rsi, 1),
        "sar": "BUY" if sar_buy else "SELL",
        "sec": sec_in_minute
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
    for c in candles_raw[-25:]:
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
    """Отримання історичних свічок та живих тіків без скидання при перемиканні"""
    existing = STATE["candles"].get(sym, [])

    # Завантажуємо історію лише один раз, якщо свічок ще менше 14
    if len(existing) < 14 and hasattr(api, "get_historical_candles"):
        try:
            raw_c = await asyncio.to_thread(api.get_historical_candles, sym, 60, 30)
            formatted = parse_history_candles(raw_c)
            if len(formatted) >= 14:
                STATE["candles"][sym] = formatted[-25:]
        except Exception:
            pass

    sub_set = STATE.setdefault("subscribed", set())
    if sym not in sub_set:
        await asyncio.to_thread(api.subscribe, sym, 60)
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
                    logging.info("CONNECTED TO POCKET OPTION (OK)")
                else:
                    STATE["connected"] = False
                    logging.error("PO AUTH ERROR: %s", err)
            except Exception as e:
                logging.error("CONNECT ERROR: %s", e)

            await asyncio.sleep(10)
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
        if prio and prio not in open_syms:
            open_syms.insert(0, prio)
        symbols = ([prio] if prio in open_syms else []) + [s for s in open_syms if s != prio]

        for sym in symbols:
            try:
                await fetch_symbol_data(api, sym)
            except Exception as e:
                logging.error("CANDLES ERROR (%s): %s", sym, e)
            await asyncio.sleep(0.25)


async def priority_tick_worker():
    """Швидке оновлення (0.25с) поточної обраної пари з авто-відновленням підписки"""
    while True:
        api = STATE["api"]
        prio = STATE["priority_symbol"]
        if api is not None and STATE["connected"] and prio:
            try:
                sub_set = STATE.setdefault("subscribed", set())
                if prio not in sub_set:
                    await asyncio.to_thread(api.subscribe, prio, 60)
                    sub_set.add(prio)
                await fetch_symbol_data(api, prio)
            except Exception:
                pass
        await asyncio.sleep(0.25)


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
