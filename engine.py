"""
Фоновый торговый движок.

Важно: рыночные данные (цена/объём) — общие для всех пользователей и
запрашиваются один раз за тик (публичный эндпоинт, ключ не нужен).
А вот ордера на покупку/продажу выполняются персональным Bybit-сессией
каждого пользователя, с его собственными ключами — деньги и позиции
у каждого свои и не пересекаются.

Этот модуль крутится в ОДНОМ фоновом потоке процесса. Если ты
разворачиваешь приложение на нескольких воркерах/процессах — заведи
отдельный процесс-воркер для движка (см. README), иначе один и тот же
сигнал будет обработан несколько раз и ордера продублируются.
"""
import logging
import threading
import time
from collections import deque
from datetime import datetime, date, time as dtime

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

from models import db, User, UserConfig, Position, Trade

log = logging.getLogger("ENGINE")

SYMBOLS = ["DOGEUSDT", "NEARUSDT", "SOLUSDT", "AVAXUSDT", "ETHUSDT", "BTCUSDT"]
MAX_POINTS = 200
TICK_SECONDS = 3
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
VOL_SHOCK_MULT = 1.8
VOL_OK_MULT = 1.3

# ── Общие рыночные данные (не привязаны к пользователю) ────────────
histories = {
    s: {"time": deque(maxlen=MAX_POINTS), "price": deque(maxlen=MAX_POINTS),
        "volume": deque(maxlen=MAX_POINTS)}
    for s in SYMBOLS
}
rsi_history = {s: deque(maxlen=MAX_POINTS) for s in SYMBOLS}

# ── Публичная сессия (без ключей) для тикеров ──────────────────────
_public = HTTP(testnet=False)

# ── Кэш авторизованных сессий по user_id, чтобы не пересоздавать HTTP каждый тик
_user_sessions: dict[int, HTTP] = {}
_engine_lock = threading.Lock()

# ── Живая лента событий и статус для UI (по user_id) ───────────────
live_status: dict[int, list[str]] = {}
tape_events: deque = deque(maxlen=40)
last_balance: dict[int, float] = {}   # кэш баланса USDT по user_id, обновляется движком


def _get_user_session(user_id: int, api_key: str, api_secret: str) -> HTTP:
    if user_id not in _user_sessions:
        _user_sessions[user_id] = HTTP(testnet=False, api_key=api_key, api_secret=api_secret)
    return _user_sessions[user_id]


def invalidate_user_session(user_id: int):
    """Вызывать при смене API-ключей пользователем."""
    _user_sessions.pop(user_id, None)


# ───────────────────────────────────────────────────────────────
#  ИНДИКАТОРЫ (общие, считаются один раз на символ за тик)
# ───────────────────────────────────────────────────────────────
def calc_rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    s = pd.Series(prices)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    val = (100 - 100 / (1 + rs)).iloc[-1]
    return round(float(val), 1) if pd.notna(val) else 50.0


def calc_ema(prices: list, period: int) -> list:
    if len(prices) < period:
        return [None] * len(prices)
    return pd.Series(prices).ewm(span=period, adjust=False).mean().tolist()


def vol_ratio(volumes: list) -> float:
    if len(volumes) < 10:
        return 1.0
    avg = np.mean(list(volumes)[-21:-1])
    return float(volumes[-1]) / avg if avg > 0 else 1.0


def rsi_divergence(prices: list, rsis: list) -> bool:
    if len(prices) < 5 or len(rsis) < 5:
        return False
    p, r = list(prices), list(rsis)
    return (p[-1] < p[-3]) and (r[-1] > r[-3])


def generate_signal(symbol: str, rsi_entry_threshold: int) -> dict:
    """Сигнал зависит от порога RSI пользователя, остальное общее."""
    h = histories[symbol]
    prices = list(h["price"])
    volumes = list(h["volume"])

    result = {"signal": "NEUTRAL", "rsi": 50.0, "vol_ratio": 1.0,
              "vol_shock": False, "divergence": False, "desc": "мало данных"}

    if len(prices) < EMA_SLOW + 2:
        return result

    rsi = calc_rsi(prices, RSI_PERIOD)
    ema_f = calc_ema(prices, EMA_FAST)
    ema_s = calc_ema(prices, EMA_SLOW)

    vr = vol_ratio(volumes)
    e_fast = ema_f[-1] or 0.0
    e_slow = ema_s[-1] or 0.0
    shock = vr >= VOL_SHOCK_MULT
    vol_ok = vr >= VOL_OK_MULT
    up = e_fast > e_slow
    diverg = rsi_divergence(prices, list(rsi_history[symbol]))

    result.update({"rsi": rsi, "vol_ratio": vr, "vol_shock": shock, "divergence": diverg})

    if rsi < rsi_entry_threshold and up and (vol_ok or diverg):
        tags = "EMA↑" + (f" VOL×{vr:.1f}" if vol_ok else "") + (" DIV" if diverg else "")
        result.update({"signal": "BUY", "desc": f"RSI {rsi} + {tags}"})
    elif rsi < rsi_entry_threshold and up:
        result.update({"signal": "WEAK_BUY", "desc": f"RSI {rsi} + EMA↑"})
    elif rsi > (100 - rsi_entry_threshold) and not up:
        result.update({"signal": "SELL", "desc": f"RSI {rsi} + EMA↓"})
    else:
        result.update({"signal": "NEUTRAL", "desc": f"RSI {rsi}"})

    return result


def _qty_str(price: float, usdt: float) -> str:
    qty = usdt / price
    if price > 10_000: return f"{qty:.5f}"
    elif price > 1_000: return f"{qty:.4f}"
    elif price > 100: return f"{qty:.3f}"
    elif price > 1: return f"{qty:.2f}"
    else: return f"{qty:.0f}"


def _safe_sell_qty(price: float, raw_qty: float) -> str:
    if price > 10_000: return f"{raw_qty:.5f}"
    elif price > 1_000: return f"{raw_qty:.4f}"
    elif price > 100: return f"{raw_qty:.3f}"
    elif price > 1: return f"{raw_qty:.2f}"
    else: return f"{raw_qty:.0f}"


# ───────────────────────────────────────────────────────────────
#  ТОРГОВЛЯ ДЛЯ ОДНОГО ПОЛЬЗОВАТЕЛЯ
# ───────────────────────────────────────────────────────────────
def _today_realized_loss_usd(user_id: int) -> float:
    """
    Суммарный реализованный убыток пользователя за СЕГОДНЯ по всем монетам
    сразу (портфельно), а не по одной конкретной позиции.

    Источник правды — журнал сделок Trade, а не мутируемое поле в Position:
    так лимит переживает рестарт процесса и не может рассинхронизироваться
    между несколькими открытыми позициями одного пользователя.
    """
    day_start = datetime.combine(date.today(), dtime.min)
    rows = (db.session.query(Trade.pnl)
            .filter(Trade.user_id == user_id, Trade.time >= day_start, Trade.pnl < 0)
            .all())
    return abs(sum(r[0] for r in rows)) if rows else 0.0


def _can_buy(cfg: UserConfig, pos: Position, open_count: int, bal: float, today_loss: float) -> tuple[bool, str]:
    if pos.state == "HOLDING":
        return False, "уже в сделке"
    if pos.cooldown > 0:
        return False, f"cooldown {pos.cooldown}т"
    if open_count >= cfg.max_open_positions:
        return False, "лимит позиций"
    if today_loss >= cfg.max_daily_loss_usd:
        return False, "дневной лимит (по всему счёту)"
    if bal < cfg.buy_usdt:
        return False, "мало USDT"
    return True, "OK"


def _check_exit(cfg: UserConfig, pos: Position, price: float) -> tuple[str, str]:
    if pos.state != "HOLDING":
        return "", ""
    entry = pos.entry_price
    peak = pos.peak_price
    sl = entry * (1 - cfg.stop_loss_pct / 100)
    tp = entry * (1 + cfg.take_profit_pct / 100)
    trail = peak * (1 - cfg.trailing_pct / 100)

    if price > peak:
        pos.peak_price = price

    if price <= sl:
        return "SL", f"${sl:.5f}"
    if price >= tp:
        return "TP", f"${tp:.5f}"
    if peak > entry * 1.01 and price <= trail:
        return "TRAILING", f"${trail:.5f}"
    return "", ""


def _place_buy(session: HTTP, user_id: int, symbol: str, price: float, usdt: float) -> tuple[bool, float, float]:
    qty_str = _qty_str(price, usdt)
    try:
        r = session.place_order(category="spot", symbol=symbol, side="Buy",
                                 orderType="Market", qty=qty_str)
        if r["retCode"] == 0:
            return True, price, float(qty_str)
        log.warning(f"[user {user_id}] Market BUY fail {symbol}: {r['retMsg']} → trying Limit")
        r2 = session.place_order(category="spot", symbol=symbol, side="Buy",
                                  orderType="Limit", qty=qty_str,
                                  price=str(round(price, 6)), timeInForce="GTC")
        if r2["retCode"] == 0:
            return True, price, float(qty_str)
        log.error(f"[user {user_id}] BUY FAIL {symbol}: {r2['retMsg']}")
    except Exception as e:
        log.error(f"[user {user_id}] place_buy exception {symbol}: {e}")
    return False, 0.0, 0.0


def _get_coin_qty(session: HTTP, user_id: int, symbol: str) -> float:
    coin = symbol.replace("USDT", "")
    try:
        b = session.get_wallet_balance(accountType="UNIFIED", coin=coin)
        return float(b["result"]["list"][0]["coin"][0]["walletBalance"])
    except Exception as e:
        log.error(f"[user {user_id}] coin_qty {symbol}: {e}")
        return 0.0


def _place_sell(session: HTTP, user_id: int, symbol: str, cur_price: float) -> tuple[bool, float, float]:
    raw_qty = _get_coin_qty(session, user_id, symbol)
    if raw_qty < 1e-6:
        return False, 0.0, 0.0
    qty_str = _safe_sell_qty(cur_price, raw_qty)
    try:
        r = session.place_order(category="spot", symbol=symbol, side="Sell",
                                 orderType="Market", qty=qty_str)
        if r["retCode"] == 0:
            return True, cur_price, raw_qty
        log.error(f"[user {user_id}] SELL FAIL {symbol}: {r['retMsg']}")
    except Exception as e:
        log.error(f"[user {user_id}] place_sell exception {symbol}: {e}")
    return False, 0.0, 0.0


def _execute_buy(session: HTTP, user_id: int, symbol: str, price: float, usdt: float,
                  dry_run: bool) -> tuple[bool, float, float]:
    """
    dry_run=True: НИКАКОГО реального запроса на биржу. Симулируем полное
    исполнение маркет-ордера по текущей цене тика — так можно безопасно
    проверять стратегию и настройки на живом рынке, не рискуя деньгами.
    """
    if dry_run:
        qty = usdt / price
        return True, price, qty
    return _place_buy(session, user_id, symbol, price, usdt)


def _execute_sell(session: HTTP, user_id: int, symbol: str, cur_price: float,
                   dry_run: bool, dry_qty: float) -> tuple[bool, float, float]:
    """dry_run=True: продаём симулированное количество (из pos.qty), без обращения к бирже."""
    if dry_run:
        if dry_qty < 1e-9:
            return False, 0.0, 0.0
        return True, cur_price, dry_qty
    return _place_sell(session, user_id, symbol, cur_price)


def _get_usdt_balance(session: HTTP, user_id: int) -> float:
    try:
        b = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        return float(b["result"]["list"][0]["coin"][0]["walletBalance"])
    except Exception as e:
        log.error(f"[user {user_id}] balance error: {e}")
        return 0.0


def _process_user(app, user: User):
    cfg = user.config
    cred = user.credential
    if not cfg or not cred or not cfg.is_active:
        return

    try:
        api_key, api_secret = cred.get_keys()
    except Exception as e:
        log.error(f"[user {user.id}] не удалось расшифровать ключи: {e}")
        return

    dry_run = cfg.is_dry_run
    session = _get_user_session(user.id, api_key, api_secret)

    if dry_run and not (api_key and api_secret):
        # Симуляция без реальных ключей: даём виртуальный баланс, чтобы можно
        # было проверить стратегию ДО того, как пользователь вообще ввёл
        # ключи Bybit. Как только ключи появятся — баланс станет настоящим.
        usdt_bal = 10_000.0
    else:
        usdt_bal = _get_usdt_balance(session, user.id)
    last_balance[user.id] = usdt_bal

    today_loss = _today_realized_loss_usd(user.id)

    positions_by_symbol = {p.symbol: p for p in user.positions}
    open_count = sum(1 for p in positions_by_symbol.values() if p.state == "HOLDING")
    msgs = live_status.setdefault(user.id, [])
    msgs.clear()
    if dry_run:
        msgs.append("🧪 DRY-RUN: реальные ордера НЕ отправляются, это симуляция")

    for symbol in SYMBOLS:
        if not histories[symbol]["price"]:
            continue
        cur_price = list(histories[symbol]["price"])[-1]

        pos = positions_by_symbol.get(symbol)
        if pos is None:
            pos = Position(user_id=user.id, symbol=symbol,
                           state="WAITING", entry_price=0.0, qty=0.0,
                           peak_price=0.0, entry_time="", cooldown=0,
                           daily_loss=0.0, last_trade_day="")
            db.session.add(pos)
            positions_by_symbol[symbol] = pos

        if pos.cooldown > 0:
            pos.cooldown -= 1

        # выход
        ex_reason, ex_desc = _check_exit(cfg, pos, cur_price)
        if ex_reason:
            ok, fill_price, raw_qty = _execute_sell(session, user.id, symbol, cur_price,
                                                      dry_run, pos.qty)
            if ok:
                pnl = (fill_price - pos.entry_price) * raw_qty
                if pnl < 0:
                    today_loss += abs(pnl)
                trade = Trade(user_id=user.id, symbol=symbol, side=f"SELL({ex_reason})",
                               price=fill_price, qty=raw_qty, pnl=pnl, is_dry_run=dry_run)
                db.session.add(trade)
                pos.state = "WAITING"
                pos.entry_price = 0.0
                pos.qty = 0.0
                pos.peak_price = 0.0
                pos.entry_time = ""
                pos.cooldown = cfg.cooldown_bars
                open_count -= 1
                tag = "🧪" if dry_run else "💰"
                msgs.append(f"{tag} SELL {symbol} [{ex_reason}] {ex_desc} PnL={pnl:+.4f}$")
            continue

        # вход
        sd = generate_signal(symbol, cfg.rsi_entry)
        if sd["signal"] == "BUY":
            can, reason = _can_buy(cfg, pos, open_count, usdt_bal, today_loss)
            if can:
                ok, fill_price, qty = _execute_buy(session, user.id, symbol, cur_price,
                                                     cfg.buy_usdt, dry_run)
                if ok:
                    pos.state = "HOLDING"
                    pos.entry_price = fill_price
                    pos.qty = qty
                    pos.peak_price = fill_price
                    pos.entry_time = datetime.now().isoformat()
                    pos.cooldown = 0
                    trade = Trade(user_id=user.id, symbol=symbol, side="BUY",
                                  price=fill_price, qty=qty, pnl=None, is_dry_run=dry_run)
                    db.session.add(trade)
                    usdt_bal -= cfg.buy_usdt
                    last_balance[user.id] = usdt_bal
                    open_count += 1
                    tag = "🧪" if dry_run else "🟢"
                    msgs.append(f"{tag} BUY {symbol} @ ${fill_price:.5f} — {sd['desc']}")
            else:
                msgs.append(f"🚫 {symbol} заблокирован: {reason}")

    db.session.commit()


def _tick(app):
    tape_events.append("🛰 SCANNING MARKETS...")
    for s in SYMBOLS:
        try:
            t = _public.get_tickers(category="spot", symbol=s)
            d = t["result"]["list"][0]
            histories[s]["time"].append(pd.Timestamp.now())
            histories[s]["price"].append(float(d["lastPrice"]))
            histories[s]["volume"].append(float(d["volume24h"]))
            prices = list(histories[s]["price"])
            if len(prices) >= EMA_SLOW + 2:
                rsi_history[s].append(calc_rsi(prices, RSI_PERIOD))
        except Exception as e:
            log.error(f"ticker {s}: {e}")

    with app.app_context():
        users = User.query.join(UserConfig).filter(UserConfig.is_active.is_(True)).all()
        for user in users:
            try:
                _process_user(app, user)
            except Exception as e:
                log.error(f"[user {user.id}] processing error: {e}")
                db.session.rollback()


def start_engine(app):
    """Запускает фоновый цикл движка. Вызывать один раз при старте процесса."""
    def _loop():
        log.info("🚀 Engine loop started")
        while True:
            try:
                _tick(app)
            except Exception as e:
                log.error(f"tick error: {e}")
            time.sleep(TICK_SECONDS)

    t = threading.Thread(target=_loop, daemon=True, name="trading-engine")
    t.start()
    return t
