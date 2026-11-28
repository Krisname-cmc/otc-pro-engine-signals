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

FIAT_AND_METALS = {
    "EUR", "USD", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
    "BRL", "CLP", "CNH", "COP", "IDR", "INR", "MXN", "PHP",
    "THB", "VND", "SGD", "HKD", "ZAR", "TRY", "PLN", "SEK",
    "NOK", "DKK", "HUF", "CZK", "ILS", "KRW", "MYR", "TWD",
    "XAU", "XAG", "XPT", "XPD"
}

STATE = {
    "api": None,
    "assets": {},
    "candles": {},
    "ticks": {},
    "last_signals": {},
    "connected": False
}


def calculate_demarker(highs, lows, period=5):
    """Розрахунок індикатора DeMarker (шкала від 0.00 до 1.00)"""
    if len(highs) < period + 2:
        return None
    de_max, de_min = [], []
    for i in range(-period, 0):
        diff_h = highs[i] - highs[i - 1]
        diff_l = lows[i - 1] - lows[i]
        de_max.append(diff_h if diff_h > 0 else 0.0)
        de_min.append(diff_l if diff_l > 0 else 0.0)

    sum_max = sum(de_max)
    sum_min = sum(de_min)
    denom = sum_max + sum_min
    if denom == 0:
        return 0.5
    return sum_max / denom


def analyze_pair_m1(sym, candles, recent_ticks):
    """M1 Відбій на відкритті свічки: DeMarker(5) + Тінь виснаження + ATR -> 15s / 30s / 1m"""
    if not candles or len(candles) < 12:
        return {
            "status": "WARMING",
            "collected": len(candles),
            "needed": 12,
            "price": round(recent_ticks[-1], 5) if recent_ticks else None
        }

    opens = [float(x["open"]) for x in candles]
    highs = [float(x["high"]) for x in candles]
    lows = [float(x["low"]) for x in candles]
    closes = [float(x["close"]) for x in candles]

    ema_now, ema_prev, atr_val = calculate_ema_and_atr(highs, lows, closes, period=10)
    dem5 = calculate_demarker(highs, lows, period=5)
    dem3 = calculate_demarker(highs, lows, period=3)

    if ema_now is None or dem5 is None or dem3 is None or atr_val <= 0:
        return {"status": "SCAN", "price": closes[-1]}

    cur_bucket = candles[-1]["time"]
    sec_in_1m = int(time.time()) % 60

    # Вхід тільки у перші 18 секунд нової свічки
    if sec_in_1m > 18:
        return {
            "status": "READY",
            "price": closes[-1],
            "rsi": round(dem5, 2),
            "cci": round(dem3, 2),
            "cur_bucket": cur_bucket
        }

    prev_range = max(highs[-2] - lows[-2], 1e-9)
    prev_lower_wick = (min(opens[-2], closes[-2]) - lows[-2]) / prev_range
    prev_upper_wick = (highs[-2] - max(opens[-2], closes[-2])) / prev_range

    strong_move = prev_range >= (atr_val * 0.35)

    tick_up = len(recent_ticks) >= 2 and recent_ticks[-1] > recent_ticks[-2]
    tick_down = len(recent_ticks) >= 2 and recent_ticks[-1] < recent_ticks[-2]

    # BUY: DeMarker(5) у зоні перепроданості (<= 0.18), є нижня тінь відкупу >= 30%, нова свічка росте
    buy_cond = strong_move and (dem5 <= 0.18) and (prev_lower_wick >= 0.30) and (closes[-1] >= opens[-1]) and tick_up

    # SELL: DeMarker(5) у зоні перекупленості (>= 0.82), є верхня тінь тиску >= 30%, нова свічка падає
    sell_cond = strong_move and (dem5 >= 0.82) and (prev_upper_wick >= 0.30) and (closes[-1] <= opens[-1]) and tick_down

    now_str = datetime.now(KYIV_TZ).strftime("%H:%M")

    def pick_exp_dem(dem_val, is_buy, sec_val):
        extreme = dem_val if is_buy else (1.0 - dem_val)
        if extreme <= 0.08 and sec_val <= 8:
            return "15s"
        if extreme <= 0.14 and sec_val <= 13:
            return "30s"
        return "1m"

    if buy_cond:
        rec_exp = pick_exp_dem(dem5, True, sec_in_1m)
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

    elif sell_cond:
        rec_exp = pick_exp_dem(dem5, False, sec_in_1m)
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
        "rsi": round(dem5, 2),
        "cci": round(dem3, 2),
        "cur_bucket": cur_bucket
    }

    # 2. Перевіряємо тінь відскоку на щойно закритій свічці (candles[-2])
    prev_range = max(highs[-2] - lows[-2], 1e-9)
    prev_lower_wick = (min(opens[-2], closes[-2]) - lows[-2]) / prev_range
    prev_upper_wick = (highs[-2] - max(opens[-2], closes[-2])) / prev_range

    # Відсікаємо мікро-свічки (менше 40% від середньої волатильності ATR)
    strong_move = prev_range >= (atr_val * 0.4)

    tick_up = len(recent_ticks) >= 2 and recent_ticks[-1] > recent_ticks[-2]
    tick_down = len(recent_ticks) >= 2 and recent_ticks[-1] < recent_ticks[-2]

    # BUY на відскоку: різке падіння (RSI4 <= 25), знизу утворилася тінь викупу >= 32%, нова свічка пішла вгору
    buy_cond = strong_move and (rsi4 <= 25) and (prev_lower_wick >= 0.32) and (closes[-1] >= opens[-1]) and tick_up

    # SELL на відскоку: різкий ріст (RSI4 >= 75), зверху утворилася тінь тиску >= 32%, нова свічка пішла вниз
    sell_cond = strong_move and (rsi4 >= 75) and (prev_upper_wick >= 0.32) and (closes[-1] <= opens[-1]) and tick_down

    now_str = datetime.now(KYIV_TZ).strftime("%H:%M")

    def pick_exp_bounce(rsi4_val, is_buy, sec_val):
        extreme = rsi4_val if is_buy else (100 - rsi4_val)
        if extreme <= 12 and sec_val <= 8:
            return "15s"
        if extreme <= 20 and sec_val <= 13:
            return "30s"
        return "1m"

    if buy_cond:
        rec_exp = pick_exp_bounce(rsi4, True, sec_in_1m)
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

    elif sell_cond:
        rec_exp = pick_exp_bounce(rsi4, False, sec_in_1m)
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
        "rsi": round(rsi4, 1),
        "cci": round(rsi6, 1),
        "cur_bucket": cur_bucket
    }

def parse_assets(raw_assets):
    result = {}
    if not isinstance(raw_assets, dict):
        return result
    for sym, item in raw_assets.items():
        s_str = str(sym)
        if not (isinstance(item, dict) and s_str.lower().endswith("_otc")):
            continue
        base_code = s_str[:-4].upper()
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
            res = analyze_pair_m1(sym, c_list, t_list)
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
    """Оновлює тіки та M1 свічки (60 сек) для всіх пар в одному потоці"""
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

            bucket = int(ts // 60) * 60
            hist = STATE["candles"].setdefault(sym, [])
            if not hist or hist[-1]["time"] < bucket:
                hist.append({"time": bucket, "open": price, "high": price, "low": price, "close": price})
                if len(hist) > 50:
                    STATE["candles"][sym] = hist[-50:]
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
                    logging.info("CONNECTED TO POCKET OPTION (M1 TREND 92% RADAR)")
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

        top_88_syms = sorted([
            s for s, info in STATE["assets"].items()
            if info.get("is_open") and info.get("payout", 0) >= 88
        ])

        if not top_88_syms:
            await asyncio.sleep(2)
            continue

        sub_set = STATE.setdefault("subscribed", set())
        for sym in top_88_syms:
            if not api.check_connect():
                STATE["connected"] = False
                break
            if sym not in sub_set:
                try:
                    await asyncio.to_thread(api.subscribe, sym, 60)
                    sub_set.add(sym)
                    await asyncio.sleep(0.12)
                except Exception:
                    pass

        if not STATE["connected"]:
            continue

        await asyncio.to_thread(update_all_symbols_sync, api, top_88_syms)
        await asyncio.sleep(0.8)


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
    logging.info("M1 RADAR 92% ENGINE RUNNING ON PORT %s", PORT)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
