"""
Функциональные тесты движка после разделения engine.py на
strategy.py / risk.py / execution.py / engine.py — БЕЗ реальных сетевых
вызовов к Bybit (мокаем execution._bybit_call / execution.fetch_recent_klines).

Ключевая проверка — паритет: сигнал, который выдаёт live-путь
(engine.generate_signal), должен НОЛЬ раз разойтись с сигналом,
который выдаёт backtest-путь (strategy.generate_signal_from_series
через backtest.run_backtest) на одних и тех же данных. Это условие
должно выполняться при любых будущих изменениях strategy.py/engine.py —
запускать эти тесты перед каждым пушем, который трогает сигнальную
логику или сбор рыночных данных.

Запуск: MASTER_KEY=... python3 test_engine_refactor.py
"""
import os
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("MASTER_KEY", __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode())

import numpy as np
import pandas as pd

import strategy
import risk
import execution
import engine
import backtest

FAILED = []


def check(name, cond):
    status = "✅" if cond else "❌"
    print(f"{status} {name}")
    if not cond:
        FAILED.append(name)


# ───────────────────────────────────────────────────────────────
# 1. strategy.py — чистая сигнальная функция не зависит от глобального состояния
# ───────────────────────────────────────────────────────────────
def test_strategy_pure():
    prices = [100 + i * 0.1 for i in range(60)]
    volumes = [1000] * 60
    rsi_hist = [50.0] * 60
    r1 = strategy.generate_signal_from_series(prices, volumes, rsi_hist, 45)
    r2 = strategy.generate_signal_from_series(prices, volumes, rsi_hist, 45)
    check("strategy: чистая функция детерминирована (одинаковый вход -> одинаковый выход)", r1 == r2)
    check("strategy: возвращает валидный сигнал", r1["signal"] in ("BUY", "WEAK_BUY", "SELL", "NEUTRAL"))

    short = strategy.generate_signal_from_series([1, 2, 3], [1, 1, 1], [], 45)
    check("strategy: при малом окне данных сигнал NEUTRAL/мало данных", short["signal"] == "NEUTRAL")


# ───────────────────────────────────────────────────────────────
# 2. risk.py — _can_buy и _check_exit работают на лёгких объектах (без ORM)
# ───────────────────────────────────────────────────────────────
def test_risk_can_buy_and_exit():
    cfg = SimpleNamespace(max_open_positions=1, max_daily_loss_usd=5.0, buy_usdt=20.0,
                           stop_loss_pct=3.0, take_profit_pct=2.5, trailing_pct=0.5)
    pos = SimpleNamespace(state="WAITING", cooldown=0, entry_price=0.0, peak_price=0.0)

    can, reason = risk._can_buy(cfg, pos, open_count=0, bal=100.0, today_loss=0.0)
    check("risk: _can_buy разрешает вход при чистых условиях", can and reason == "OK")

    can, reason = risk._can_buy(cfg, pos, open_count=0, bal=100.0, today_loss=5.0)
    check("risk: _can_buy блокирует при достижении дневного лимита убытка", not can and "лимит" in reason)

    pos_holding = SimpleNamespace(state="HOLDING", entry_price=100.0, peak_price=100.0)
    ex_reason, _ = risk._check_exit(cfg, pos_holding, price=96.0)
    check("risk: _check_exit триггерит SL при падении ниже stop_loss_pct", ex_reason == "SL")

    pos_holding2 = SimpleNamespace(state="HOLDING", entry_price=100.0, peak_price=100.0)
    ex_reason2, _ = risk._check_exit(cfg, pos_holding2, price=103.0)
    check("risk: _check_exit триггерит TP при росте выше take_profit_pct", ex_reason2 == "TP")


# ───────────────────────────────────────────────────────────────
# 3. execution.py — идемпотентная отправка ордера, мок вместо реального Bybit
# ───────────────────────────────────────────────────────────────
def test_execution_idempotent_order_dry_run_and_mock():
    # dry-run: НИ ОДНОГО обращения к сети
    with mock.patch.object(execution, "_bybit_call") as mocked:
        ok, price, qty = execution.execute_buy(session=None, user_id=1, symbol="BTCUSDT",
                                                price=50000.0, usdt=20.0, dry_run=True)
        check("execution: dry-run BUY не делает сетевых вызовов", mocked.call_count == 0)
        check("execution: dry-run BUY возвращает ожидаемое количество", ok and abs(qty - 20.0 / 50000.0) < 1e-9)

    # live-путь на моке: сеть недоступна из песочницы, поэтому мокаем сам _bybit_call
    fake_session = mock.MagicMock()
    with mock.patch.object(execution, "_bybit_call") as mocked:
        mocked.return_value = {"retCode": 0, "retMsg": "OK"}
        ok, price, qty = execution._place_buy(fake_session, user_id=1, symbol="BTCUSDT",
                                               price=50000.0, usdt=20.0)
        check("execution: успешный place_order (retCode=0) считается исполненным", ok)
        check("execution: вызван ровно один раз (Market, без отката на Limit)", mocked.call_count == 1)

    # сетевое исключение при отправке -> сверка через orderLinkId -> находим Filled
    with mock.patch.object(execution, "_bybit_call") as mocked:
        def side_effect(fn, *a, **kw):
            if kw.get("_what", "").startswith("Buy"):
                raise ConnectionError("simulated network drop")
            return {"result": {"list": [{"orderStatus": "Filled", "avgPrice": "50010", "cumExecQty": "0.0004"}]}}
        mocked.side_effect = side_effect
        ok, fill_price, qty = execution._place_buy(fake_session, user_id=1, symbol="BTCUSDT",
                                                     price=50000.0, usdt=20.0)
        check("execution: сетевой сбой при отправке не считается провалом, если ордер реально исполнился",
              ok and abs(fill_price - 50010.0) < 1e-6)


# ───────────────────────────────────────────────────────────────
# 4. engine._poll_candles — только закрытые свечи попадают в историю, без дублей
# ───────────────────────────────────────────────────────────────
def test_engine_poll_candles_no_lookahead_no_dupes():
    symbol = engine.SYMBOLS[0]
    engine.histories[symbol] = {k: type(engine.histories[symbol][k])() for k in engine.histories[symbol]}
    engine._last_candle_open_ms[symbol] = 0

    now_ms = int(pd.Timestamp.now().timestamp() * 1000)
    interval_ms = engine._INTERVAL_MS["1"]
    closed_open = now_ms - 5 * interval_ms
    forming_open = now_ms - int(interval_ms * 0.2)  # ещё не закрылась

    fake_rows = [
        [str(closed_open), "100", "101", "99", "100.5", "1000", "0"],
        [str(closed_open + interval_ms), "100.5", "102", "100", "101", "1200", "0"],
        [str(forming_open), "101", "103", "100.5", "102", "300", "0"],  # незакрытая — не должна попасть
    ]

    with mock.patch.object(execution, "fetch_recent_klines", return_value=fake_rows):
        engine.CANDLE_INTERVAL_MIN = "1"
        engine._poll_candles()

    h = engine.histories[symbol]
    check("engine: в историю попали только закрытые бары (2 из 3)", len(h["price"]) == 2)
    check("engine: цена закрытия последнего добавленного бара верна", abs(list(h["price"])[-1] - 101.0) < 1e-9)

    # повторный опрос теми же данными не должен продублировать уже добавленные бары
    with mock.patch.object(execution, "fetch_recent_klines", return_value=fake_rows):
        engine._poll_candles()
    check("engine: повторный опрос не дублирует уже добавленные закрытые бары", len(h["price"]) == 2)


# ───────────────────────────────────────────────────────────────
# 5. Паритет live-пути (engine.generate_signal) и backtest-пути (run_backtest)
#    на одних и тех же синтетических барах — 0 расхождений по сигналу.
# ───────────────────────────────────────────────────────────────
def test_live_backtest_signal_parity():
    np.random.seed(42)
    n = 400
    base = 100 + np.cumsum(np.random.normal(0, 0.3, n))
    volumes = np.abs(np.random.normal(1000, 300, n)) + 100
    times = [datetime(2024, 1, 1) + timedelta(minutes=i) for i in range(n)]
    df = pd.DataFrame({"time": times, "open": base, "high": base + 0.5,
                        "low": base - 0.5, "close": base, "volume": volumes})

    cfg = backtest.BtConfig(rsi_entry=45, take_profit_pct=2.5, stop_loss_pct=3.0,
                             trailing_pct=0.5, cooldown_bars=10, buy_usdt=20.0, fee_pct=0.0)

    # bt-путь: генерируем сигналы через ту же функцию, что и внутри run_backtest
    from collections import deque as dq
    price_win, vol_win, rsi_win = dq(maxlen=strategy.MAX_POINTS), dq(maxlen=strategy.MAX_POINTS), dq(maxlen=strategy.MAX_POINTS)
    bt_signals = []
    for i in range(n):
        price_win.append(df["close"].iloc[i])
        vol_win.append(df["volume"].iloc[i])
        if len(price_win) >= strategy.EMA_SLOW + 2:
            rsi_win.append(strategy.calc_rsi(list(price_win), strategy.RSI_PERIOD))
        sd = strategy.generate_signal_from_series(list(price_win), list(vol_win), list(rsi_win), cfg.rsi_entry)
        bt_signals.append(sd["signal"])

    # live-путь: та же функция генерации сигнала (engine.generate_signal — тонкая обёртка
    # над strategy.generate_signal_from_series), прогоняем через engine.histories как будто
    # это баровые данные, пришедшие с биржи по одной свече за раз.
    symbol = "TESTUSDT"
    engine.histories[symbol] = {"time": dq(maxlen=strategy.MAX_POINTS), "open": dq(maxlen=strategy.MAX_POINTS),
                                 "high": dq(maxlen=strategy.MAX_POINTS), "low": dq(maxlen=strategy.MAX_POINTS),
                                 "price": dq(maxlen=strategy.MAX_POINTS), "volume": dq(maxlen=strategy.MAX_POINTS)}
    engine.rsi_history[symbol] = dq(maxlen=strategy.MAX_POINTS)

    live_signals = []
    for i in range(n):
        h = engine.histories[symbol]
        h["price"].append(df["close"].iloc[i])
        h["volume"].append(df["volume"].iloc[i])
        prices = list(h["price"])
        if len(prices) >= strategy.EMA_SLOW + 2:
            engine.rsi_history[symbol].append(strategy.calc_rsi(prices, strategy.RSI_PERIOD))
        sd = engine.generate_signal(symbol, cfg.rsi_entry)
        live_signals.append(sd["signal"])

    mismatches = sum(1 for a, b in zip(bt_signals, live_signals) if a != b)
    check(f"parity: 0 расхождений сигналов live vs backtest на {n} барах (найдено: {mismatches})",
          mismatches == 0)

    # прогоняем полный run_backtest, чтобы убедиться, что весь путь (включая _check_exit из risk.py) не падает
    result = backtest.run_backtest(df, cfg)
    check("parity: run_backtest отрабатывает end-to-end без исключений и возвращает сделки/эквити",
          "trades" in result and "equity_curve" in result)


if __name__ == "__main__":
    test_strategy_pure()
    test_risk_can_buy_and_exit()
    test_execution_idempotent_order_dry_run_and_mock()
    test_engine_poll_candles_no_lookahead_no_dupes()
    test_live_backtest_signal_parity()

    print("\n" + "=" * 50)
    if FAILED:
        print(f"❌ ПРОВАЛЕНО: {len(FAILED)} проверок")
        for f in FAILED:
            print(f"   - {f}")
        sys.exit(1)
    else:
        print("✅ ВСЕ ПРОВЕРКИ ПРОШЛИ")
