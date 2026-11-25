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

STATE = {
    "api": None,
    "assets": {},
    "candles": {},
    "ticks": {},
    "last_signals": {},
    "connected": False
}


def calculate_rsi(closes, period=6):
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


def calculate_donchian_and_wpr(highs, lows, closes, period=3):
    if len(closes) < period + 1:
        return None, None, None, None
    # Нерухомий тунель Donchian по 3 закритих свічках
    upper = max(highs[-(period + 1):-1])
    lower = min(lows[-(period + 1):-1])

    def wpr_at(idx_end):
        h_max = max(highs[idx_end - period:idx_end])
        l_min = min(lows[idx_end - period:idx_end])
        if h_max == l_min:
            return -50.0
        return ((h_max - closes[idx_end - 1]) / (h_max - l_min)) * -100.0

    wpr_now = wpr_at(len(closes))
    wpr_prev = wpr_at(len(closes) - 1)
    return upper, lower, wpr_now, wpr_prev


def analyze_pair_m5(sym, candles, recent_ticks):
    """M5 Швидкий старт (4 свічки): Donchian Channel (3) + Williams %R (3) + RSI (3)"""
    if not candles or len(candles) < 4:
        return {
            "status": "WARMING",
            "collected": len(candles),
            "needed": 4,
            "price": round(recent_ticks[-1], 5) if recent_ticks else None
        }

    highs = [float(x["high"]) for x in candles]
    lows = [float(x["low"]) for x in candles]
    closes = [float(x["close"]) for x in candles]

    upper, lower, wpr_now, wpr_prev = calculate_donchian_and_wpr(highs, lows, closes, period=3)
    rsi3 = calculate_rsi(closes, period=3)

    if upper is None or rsi3 is None:
        return {"status": "SCAN", "price": closes[-1]}

    tick_up = len(recent_ticks) >= 2 and recent_ticks[-1] > recent_ticks[-2]
    tick_down = len(recent_ticks) >= 2 and recent_ticks[-1] < recent_ticks[-2]

    at_bottom = (lows[-1] <= lower) or (lows[-2] <= lower)
    at_top = (highs[-1] >= upper) or (highs[-2] >= upper)

    wpr_buy = (wpr_prev <= -78 or wpr_now <= -78) and (wpr_now >= wpr_prev)
    wpr_sell = (wpr_prev >= -22 or wpr_now >= -22) and (wpr_now <= wpr_prev)

    sec_in_5m = int(time.time()) % 300
    now_str = datetime.now(KYIV_TZ).strftime("%H:%M")
    cur_bucket = candles[-1]["time"]

    def pick_exp(wpr_val, is_buy, sec_val):
        extreme = abs(-100 - wpr_val) if is_buy else abs(wpr_val)
        if extreme <= 8:
            return "5m"
        if sec_val <= 150:
            return "10m"
        return "15m"

    if at_bottom and wpr_buy and (rsi3 <= 45) and tick_up:
        rec_exp = pick_exp(min(wpr_now, wpr_prev), True, sec_in_5m)
        sig_data = {
            "direction": "BUY",
            "rec_exp": rec_exp,
            "time": now_str,
            "bucket": cur_bucket,
            "ts": time.time()
        }
        prev_saved = STATE["last_signals"].get(sym)
        if not prev_saved or prev_saved.get("bucket") != cur_bucket or prev_saved.get("direction") != "BUY":
            STATE["last_signals"][sym] = sig_data

    elif at_top and wpr_sell and (rsi3 >= 55) and tick_down:
        rec_exp = pick_exp(max(wpr_now, wpr_prev), False, sec_in_5m)
        sig_data = {
            "direction": "SELL",
            "rec_exp": rec_exp,
            "time": now_str,
            "bucket": cur_bucket,
            "ts": time.time()
        }
        prev_saved = STATE["last_signals"].get(sym)
        if not prev_saved or prev_saved.get("bucket") != cur_bucket or prev_saved.get("direction") != "SELL":
            STATE["last_signals"][sym] = sig_data

    return {
        "status": "READY",
        "price": closes[-1],
        "rsi": round(rsi3, 1),
        "cci": round(wpr_now, 0),
        "cur_bucket": cur_bucket
    }

FIAT_AND_METALS = {
    "EUR", "USD", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
    "BRL", "CLP", "CNH", "COP", "IDR", "INR", "MXN", "PHP",
    "THB", "VND", "SGD", "HKD", "ZAR", "TRY", "PLN", "SEK",
    "NOK", "DKK", "HUF", "CZK", "ILS", "KRW", "MYR", "TWD",
    "XAU", "XAG", "XPT", "XPD"
}


def parse_assets(raw_assets):
    result = {}
    if not isinstance(raw_assets, dict):
        return result
    for sym, item in raw_assets.items():
        s_str = str(sym)
        if not (isinstance(item, dict) and s_str.lower().endswith("_otc")):
            continue

        base_code = s_str[:-4].upper()  # Наприклад, "EURUSD" або "XAUUSD"
        # Пропускаємо тільки 6-літерні пари, де обидві частини — реальні валюти або метали (без акцій та крипти)
        if len(base_code) == 6 and base_code[:3] in FIAT_AND_METALS and base_code[3:] in FIAT_AND_METALS:
            payout = item.get("payout")
            is_avail = item.get("is_available", True)
            try:
                p_val = float(payout) if payout is not None else 0
            except Exception:
                p_val = 0
            result[s_str] = {"payout": p_val, "is_open": bool(is_avail)}
    return result


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
    rows = []
    for sym, info in STATE["assets"].items():
        if info.get("is_open") and info.get("payout", 0) >= 92:
            c_list = STATE["candles"].get(sym, [])
            t_list = STATE["ticks"].get(sym, [])
            res = analyze_pair_m5(sym, c_list, t_list)
            last_sig = STATE["last_signals"].get(sym)
            is_fresh = bool(last_sig and res.get("cur_bucket") == last_sig.get("bucket"))
            rows.append({
                "symbol": sym,
                "payout": int(info["payout"]),
                "last_signal": last_sig,
                "is_fresh": is_fresh,
                **res
            })

    def sort_key(r):
        ls = r.get("last_signal")
        if r.get("is_fresh"):
            return (0, -(ls.get("ts", 0)))
        if ls:
            return (1, -(ls.get("ts", 0)))
        if r.get("status") == "READY":
            return (2, r["symbol"])
        return (3, r["symbol"])

    rows.sort(key=sort_key)

    return web.json_response({
        "connected": STATE["connected"],
        "time": datetime.now(KYIV_TZ).strftime("%H:%M:%S"),
        "rows": rows
    })


def update_all_symbols_sync(api, symbols):
    """Оновлює тіки та M5 свічки для всіх валютних пар у межах ОДНОГО потоку (0 навантаження на пам'ять)"""
    for sym in symbols:
        try:
            ticks = api.get_realtime_ticks(sym, 5)
            if not ticks:
                continue
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
                if len(hist) > 40:
                    STATE["candles"][sym] = hist[-40:]
            else:
                cur = hist[-1]
                cur["high"] = max(cur["high"], price)
                cur["low"] = min(cur["low"], price)
                cur["close"] = price
        except Exception:
            continue


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
                    logging.info("CONNECTED TO POCKET OPTION (88%+ FOREX RADAR)")
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

        # Скануємо ТІЛЬКИ відкриті валютні пари з виплатою >= 88% (захист від спам-фільтра Pocket Option)
        top_88_syms = sorted([
            s for s, info in STATE["assets"].items()
            if info.get("is_open") and info.get("payout", 0) >= 88
        ])

        if not top_88_syms:
            await asyncio.sleep(2)
            continue

        # 1. Одноразова легка підписка на пари 88%+ (без повторного спаму в кожному циклі!)
        sub_set = STATE.setdefault("subscribed", set())
        for sym in top_88_syms:
            if not api.check_connect():
                STATE["connected"] = False
                break
            if sym not in sub_set:
                try:
                    await asyncio.to_thread(api.subscribe, sym, 60)
                    sub_set.add(sym)
                    await asyncio.sleep(0.15)
                except Exception:
                    pass

        if not STATE["connected"]:
            continue

        # 2. Зчитуємо свічки лише для пар 88%+ одним легким потоком
        await asyncio.to_thread(update_all_symbols_sync, api, top_88_syms)
        await asyncio.sleep(1.0)

async def on_startup(app):
    app["worker"] = asyncio.create_task(market_worker())


async def main():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/status", handle_status)
    app.on_startup.append(on_startup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("ALL-OTC 92% ENGINE RUNNING ON PORT %s", PORT)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
