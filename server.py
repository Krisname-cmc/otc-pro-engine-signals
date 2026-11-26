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


def calculate_rsi(closes, period=7):
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


def calculate_ema_and_atr(highs, lows, closes, period=12):
    if len(closes) < period + 2:
        return None, None, None
    k = 2 / (period + 1)
    ema_vals = [closes[0]]
    for p in closes[1:]:
        ema_vals.append(p * k + ema_vals[-1] * (1 - k))

    trs = []
    for i in range(-period, 0):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    atr_val = statistics.mean(trs)
    return ema_vals[-1], ema_vals[-3], atr_val


def calculate_stochastic(highs, lows, closes, k_period=8, smooth_k=2, smooth_d=2):
    if len(closes) < k_period + smooth_k + smooth_d:
        return None, None, None
    raw_k = []
    for i in range(k_period - 1, len(closes)):
        l_min = min(lows[i - k_period + 1:i + 1])
        h_max = max(highs[i - k_period + 1:i + 1])
        raw_k.append(50.0 if h_max == l_min else ((closes[i] - l_min) / (h_max - l_min)) * 100.0)

    k_line = [statistics.mean(raw_k[i - smooth_k + 1:i + 1]) for i in range(smooth_k - 1, len(raw_k))]
    if len(k_line) < smooth_d + 1:
        return None, None, None
    d_now = statistics.mean(k_line[-smooth_d:])
    return k_line[-1], k_line[-2], d_now


def analyze_pair_m1(sym, candles, recent_ticks):
    """M1 Вхід по тренду на відкаті: EMA(12) + ATR(12) + Stochastic(8,2,2) + RSI(7) -> 1m / 2m / 5m"""
    if not candles or len(candles) < 17:
        return {
            "status": "WARMING",
            "collected": len(candles),
            "needed": 17,
            "price": round(recent_ticks[-1], 5) if recent_ticks else None
        }

    opens = [float(x["open"]) for x in candles]
    highs = [float(x["high"]) for x in candles]
    lows = [float(x["low"]) for x in candles]
    closes = [float(x["close"]) for x in candles]

    ema_now, ema_prev, atr_val = calculate_ema_and_atr(highs, lows, closes, period=15)
    k_now, k_prev, d_now = calculate_stochastic(highs, lows, closes, k_period=6, smooth_k=2, smooth_d=2)
    rsi6 = calculate_rsi(closes, period=6)

    if ema_now is None or k_now is None or rsi6 is None or atr_val <= 0:
        return {"status": "SCAN", "price": closes[-1]}

    cur_bucket = candles[-1]["time"]

    # Фільтр стоячого ринку ATR
    if (highs[-1] - lows[-1]) < atr_val * 0.2:
        return {"status": "READY", "price": closes[-1], "rsi": round(rsi6, 1), "cci": round(k_now, 0), "cur_bucket": cur_bucket}

    # Тренд визначаємо по плавній EMA(15), щоб короткий відкат не перебивав напрямок
    uptrend = (ema_now > ema_prev) and (closes[-1] >= ema_now * 0.9995)
    downtrend = (ema_now < ema_prev) and (closes[-1] <= ema_now * 1.0005)

    pullback_buy = (k_prev <= 48) and (k_now > k_prev)
    pullback_sell = (k_prev >= 52) and (k_now < k_prev)

    # 3. Відкат у бік тренду по Stochastic
    pullback_buy = (k_prev <= 42) and (k_now > k_prev) and (k_now > d_now)
    pullback_sell = (k_prev >= 58) and (k_now < k_prev) and (k_now < d_now)

    tick_up = len(recent_ticks) >= 2 and recent_ticks[-1] > recent_ticks[-2]
    tick_down = len(recent_ticks) >= 2 and recent_ticks[-1] < recent_ticks[-2]

    sec_in_1m = int(time.time()) % 60
    now_str = datetime.now(KYIV_TZ).strftime("%H:%M")

    def pick_exp_m1(sec_val, rsi_val, is_buy):
        impulse = (rsi_val - 50) if is_buy else (50 - rsi_val)
        if sec_val <= 20 and impulse >= 8:
            return "1m"
        if sec_val <= 42:
            return "2m"
        return "5m"

    if uptrend and pullback_buy and (50 <= rsi7 <= 68) and (closes[-1] > opens[-1]) and tick_up:
        rec_exp = pick_exp_m1(sec_in_1m, rsi7, True)
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

    elif downtrend and pullback_sell and (32 <= rsi7 <= 50) and (closes[-1] < opens[-1]) and tick_down:
        rec_exp = pick_exp_m1(sec_in_1m, rsi7, False)
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
        "rsi": round(rsi6, 1),
        "cci": round(k_now, 0),
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
