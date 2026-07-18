"""
Фоновый торговый движок — тонкий оркестратор.

Вся торговая логика вынесена в отдельные модули:
  - strategy.py  — сигнал (RSI/EMA/дивергенция/объёмный фильтр), чистые функции
  - risk.py      — лимиты входа (_can_buy) и условия выхода (_check_exit)
  - execution.py — всё общение с Bybit API (сессии, rate-limit, идемпотентные ордера)

engine.py отвечает только за: сбор рыночных данных (OHLCV-свечи через
get_kline, общие для всех пользователей — публичный эндпоинт, ключ не
нужен) и цикл "на каждом тике пройтись по активным пользователям и
свести сигнал + риск + исполнение вместе" (_tick → _process_user).

Ордера на покупку/продажу выполняются персональной Bybit-сессией каждого
пользователя, с его собственными ключами — деньги и позиции у каждого
свои и не пересекаются.

Свечи vs тики: раньше движок опрашивал тикер раз в 3 секунды (сырая
последняя цена + скользящий 24-часовой объём volume24h). Сейчас — опрос
завершившихся OHLCV-баров через get_kline. В историю (`histories`)
попадает ТОЛЬКО полностью закрытая свеча: у Bybit последняя запись в
ответе kline может быть ещё формирующейся, и её использование было бы
подглядыванием в будущее относительно того, как ведёт себя бэктест (там
все бары по определению уже закрыты). Это также устраняет расхождение
live/backtest по объёмному фильтру, описанное в docstring backtest.py:
раньше live использовал volume24h тикера, а бэктест — объём бара; теперь
оба пути используют объём одного и того же завершившегося бара.

Этот модуль крутится в ОДНОМ фоновом потоке процесса. Если ты
разворачиваешь приложение на нескольких воркерах/процессах — заведи
отдельный процесс-воркер для движка (см. README), иначе один и тот же
сигнал будет обработан несколько раз и ордера продублируются.
"""
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

import pandas as pd

import execution
import risk
import strategy
from execution import invalidate_user_session  # noqa: F401  (реэкспорт для auth.py)
from models import db, User, UserConfig, Position, Trade

log = logging.getLogger("ENGINE")

SYMBOLS = ["DOGEUSDT", "NEARUSDT", "SOLUSDT", "AVAXUSDT", "ETHUSDT", "BTCUSDT"]
MAX_POINTS = strategy.MAX_POINTS

# ── Таймфрейм свечи и частота опроса ────────────────────────────────
# "1" = 1-минутные свечи (по умолчанию). Опрашивать чаще, чем закрывается
# бар, бессмысленно — POLL_SECONDS лишь достаточно мал, чтобы не пропустить
# закрытие бара надолго, а не совпадает с самим таймфреймом.
CANDLE_INTERVAL_MIN = os.getenv("CANDLE_INTERVAL_MIN", "1")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
_INTERVAL_MS = {"1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000,
                 "30": 1_800_000, "60": 3_600_000, "120": 7_200_000, "240": 14_400_000}

# ── Общие рыночные данные (не привязаны к пользователю) ────────────
histories = {
    s: {"time": deque(maxlen=MAX_POINTS), "open": deque(maxlen=MAX_POINTS),
        "high": deque(maxlen=MAX_POINTS), "low": deque(maxlen=MAX_POINTS),
        "price": deque(maxlen=MAX_POINTS), "volume": deque(maxlen=MAX_POINTS)}
    for s in SYMBOLS
}
rsi_history = {s: deque(maxlen=MAX_POINTS) for s in SYMBOLS}
# Таймстамп открытия последней уже добавленной в историю свечи по символу —
# чтобы при повторном опросе не добавить один и тот же закрытый бар дважды.
_last_candle_open_ms: dict[str, int] = {s: 0 for s in SYMBOLS}

# ── Живая лента событий и статус для UI (по user_id) ───────────────
live_status: dict[int, list[str]] = {}
tape_events: deque = deque(maxlen=40)
last_balance: dict[int, float] = {}   # кэш баланса USDT по user_id, обновляется движком


def generate_signal(symbol: str, rsi_entry_threshold: int) -> dict:
    """Тонкая live-обёртка: берёт данные из глобальных deque движка и зовёт чистую функцию стратегии."""
    h = histories[symbol]
    prices = list(h["price"])
    volumes = list(h["volume"])
    return strategy.generate_signal_from_series(prices, volumes, list(rsi_history[symbol]), rsi_entry_threshold)


# ───────────────────────────────────────────────────────────────
#  ТОРГОВЛЯ ДЛЯ ОДНОГО ПОЛЬЗОВАТЕЛЯ
# ───────────────────────────────────────────────────────────────
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
    session = execution.get_user_session(user.id, api_key, api_secret)

    if dry_run and not (api_key and api_secret):
        # Симуляция без реальных ключей: даём виртуальный баланс, чтобы можно
        # было проверить стратегию ДО того, как пользователь вообще ввёл
        # ключи Bybit. Как только ключи появятся — баланс станет настоящим.
        usdt_bal = 10_000.0
    else:
        usdt_bal = execution.get_usdt_balance(session, user.id)
    last_balance[user.id] = usdt_bal

    today_loss = risk._today_realized_loss_usd(user.id)

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
        ex_reason, ex_desc = risk._check_exit(cfg, pos, cur_price)
        if ex_reason:
            ok, fill_price, raw_qty = execution.execute_sell(session, user.id, symbol, cur_price,
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
            can, reason = risk._can_buy(cfg, pos, open_count, usdt_bal, today_loss)
            if can:
                ok, fill_price, qty = execution.execute_buy(session, user.id, symbol, cur_price,
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


# ───────────────────────────────────────────────────────────────
#  СБОР РЫНОЧНЫХ ДАННЫХ (свечи, общие для всех пользователей)
# ───────────────────────────────────────────────────────────────
def _poll_candles():
    """
    Опрашивает последние бары по каждому символу и добавляет в историю
    ТОЛЬКО новые уже ЗАКРЫТЫЕ свечи (см. docstring модуля про lookahead).
    """
    interval_ms = _INTERVAL_MS.get(CANDLE_INTERVAL_MIN, 60_000)
    now_ms = int(time.time() * 1000)

    for s in SYMBOLS:
        try:
            rows = execution.fetch_recent_klines(s, CANDLE_INTERVAL_MIN, limit=3)
        except Exception as e:
            log.error(f"kline {s}: {e}")
            continue

        for row in rows:
            open_ms = int(row[0])
            close_ms = open_ms + interval_ms
            if close_ms > now_ms:
                continue  # свеча ещё формируется — не используем (lookahead)
            if open_ms <= _last_candle_open_ms[s]:
                continue  # этот бар уже был добавлен на прошлом опросе

            o, hi, lo, c, v = (float(row[1]), float(row[2]), float(row[3]),
                               float(row[4]), float(row[5]))
            h = histories[s]
            h["time"].append(pd.to_datetime(close_ms, unit="ms"))
            h["open"].append(o)
            h["high"].append(hi)
            h["low"].append(lo)
            h["price"].append(c)
            h["volume"].append(v)
            _last_candle_open_ms[s] = open_ms

            prices = list(h["price"])
            if len(prices) >= strategy.EMA_SLOW + 2:
                rsi_history[s].append(strategy.calc_rsi(prices, strategy.RSI_PERIOD))


def _tick(app):
    tape_events.append("🛰 SCANNING MARKETS...")
    _poll_candles()

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
            time.sleep(POLL_SECONDS)

    t = threading.Thread(target=_loop, daemon=True, name="trading-engine")
    t.start()
    return t
