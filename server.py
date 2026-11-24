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
    "connected": False
}


def ema(values, period):
    k = 2 / (period + 1)
    result = [values[0]]
    for price in values[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


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


def calculate_stochastic(highs, lows, closes, k_period=6, smooth_k=2, smooth_d=2):
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


def analyze_pair_m5(candles, recent_ticks):
    """Аналіз M5 для кожного рядка: EMA 2/4 + Stochastic(3,2,2) + RSI(3) + Експірація 5m / 10m / 15m"""
    if not candles or len(candles) < 4:
        return {
            "status": "WARMING",
            "collected": len(candles),
            "needed": 4,
            "price": round(recent_ticks[-1], 5) if recent_ticks else None
        }

    sec_in_5m = int(time.time()) % 300
    opens = [float(x["open"]) for x in candles]
    highs = [float(x["high"]) for x in candles]
    lows = [float(x["low"]) for x in candles]
    closes = [float(x["close"]) for x in candles]

    # 1. Тренд EMA 2 / EMA 4
    ema_fast = ema(closes, 2)
    ema_slow = ema(closes, min(4, len(closes)))

    uptrend = ema_fast[-1] > ema_slow[-1] and ema_slow[-1] >= ema_slow[-3]
    downtrend = ema_fast[-1] < ema_slow[-1] and ema_slow[-1] <= ema_slow[-3]
    trend_label = "UP" if uptrend else ("DOWN" if downtrend else "FLAT")

    # 2. Відкат Stochastic (3, 2, 2)
    k_now, k_prev, d_now = calculate_stochastic(highs, lows, closes, 3, 2, 2)
    rsi6 = calculate_rsi(closes, period=3)

    if k_now is None or rsi6 is None:
        return {"status": "FLAT", "price": closes[-1], "trend": trend_label}

    # Перевірка вікна свічки (перші 4 хвилини M5 свічки)
    if sec_in_5m < 3 or sec_in_5m > 240:
        return {
            "status": "WAIT",
            "price": closes[-1],
            "rsi": round(rsi6, 1),
            "stoch": round(k_now, 1),
            "trend": trend_label
        }

    pullback_buy = (k_prev <= 46) and (k_now > k_prev) and (k_now >= d_now)
    pullback_sell = (k_prev >= 54) and (k_now < k_prev) and (k_now <= d_now)

    curr_green = closes[-1] > opens[-1]
    curr_red = closes[-1] < opens[-1]
    tick_up = len(recent_ticks) >= 2 and recent_ticks[-1] >= recent_ticks[-2]
    tick_down = len(recent_ticks) >= 2 and recent_ticks[-1] <= recent_ticks[-2]

    def pick_exp(sec_val, rsi_val, is_call):
        strength = (rsi_val - 50) if is_call else (50 - rsi_val)
        if sec_val <= 75 and strength >= 6:
            return "5m"
        if sec_val <= 165:
            return "10m"
        return "15m"

    if uptrend and pullback_buy and (48 <= rsi6 <= 72) and curr_green and tick_up:
        rec_exp = pick_exp(sec_in_5m, rsi6, True)
        return {
            "status": "SIGNAL",
            "direction": "BUY",
            "rec_exp": rec_exp,
            "price": closes[-1],
            "rsi": round(rsi6, 1),
            "stoch": round(k_now, 1),
            "trend": "UP"
        }

    if downtrend and pullback_sell and (28 <= rsi6 <= 52) and curr_red and tick_down:
        rec_exp = pick_exp(sec_in_5m, rsi6, False)
        return {
            "status": "SIGNAL",
            "direction": "SELL",
            "rec_exp": rec_exp,
            "price": closes[-1],
            "rsi": round(rsi6, 1),
            "stoch": round(k_now, 1),
            "trend": "DOWN"
        }

    return {
        "status": "FLAT",
        "price": closes[-1],
        "rsi": round(rsi6, 1),
        "stoch": round(k_now, 1),
        "trend": trend_label
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
            res = analyze_pair_m5(c_list, t_list)
            rows.append({
                "symbol": sym,
                "payout": int(info["payout"]),
                **res
            })

    # Сортуємо так, щоб пари з активними сигналами (BUY / SELL) завжди піднімалися на самий верх списку!
    priority_order = {"SIGNAL": 0, "WAIT": 1, "FLAT": 2, "WARMING": 3}
    rows.sort(key=lambda r: (priority_order.get(r.get("status"), 9), r["symbol"]))

    return web.json_response({
        "connected": STATE["connected"],
        "time": datetime.now(KYIV_TZ).strftime("%H:%M:%S"),
        "rows": rows
    })


async def fetch_symbol_data(api, sym):
    """Паралельний збір M5 свічок для кожної 92% пари одночасно"""
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
                    logging.info("CONNECTED TO POCKET OPTION (PARALLEL 92% RADAR)")
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

        # Беремо ТІЛЬКИ відкриті пари з виплатою >= 92%
        high_payout_syms = [
            s for s, info in STATE["assets"].items()
            if info.get("is_open") and info.get("payout", 0) >= 92
        ]

        if high_payout_syms:
            # Опитуємо ВСІ 92% пари ОДНОЧАСНО (паралельно, а не в черзі!)
            await asyncio.gather(
                *(fetch_symbol_data(api, sym) for sym in high_payout_syms),
                return_exceptions=True
            )
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(1.5)


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
    logging.info("PARALLEL RADAR 92% RUNNING ON PORT %s", PORT)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
