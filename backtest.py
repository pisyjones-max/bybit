"""
Векторизованный (бар-за-баром) бэктестер стратегии на исторических
данных Bybit spot.

Ключевой архитектурный принцип: бэктест использует ТУ ЖЕ САМУЮ сигнальную
функцию (engine.generate_signal_from_series) и ТУ ЖЕ САМУЮ логику выхода
(engine._check_exit), что и live-движок. Это не косметика — если бэктест
и live считают сигнал по-разному, бэктест ничего не говорит о том, что
реально будет торговаться, и все выводы из него бесполезны.

Также бэктест повторяет ограничение live-движка на размер скользящего
окна (MAX_POINTS свечей) — engine.py считает индикаторы не по всей
истории, а по последним 200 точкам (deque maxlen=200). Без этого
бэктест был бы "умнее" живого бота и давал бы завышенные результаты.

⚠️ Известное расхождение с live (см. README/анализ): live-движок берёт
объёмный фильтр (vol_ratio) из ПОЛЯ volume24h тикера — то есть сравнивает
скользящий 24-часовой объём между соседними 3-секундными тиками. У
исторических свечей (klines) такого поля нет — есть только объём
конкретного бара. Бэктест использует объём бара как ближайший доступный
аналог. Смысл этих двух величин РАЗНЫЙ, поэтому сигналы "vol_ok"/
"vol_shock" в бэктесте — не точная копия того, что видел live на тех же
датах, а лучшее доступное приближение. Это ограничение честности данных,
не баг бэктеста — и хороший повод в принципе пересмотреть vol24h как
источник объёмного фильтра в live-движке на объём бара, что вероятно
даже осмысленнее исходной идеи.

Использование:
    python3 backtest.py --symbol SOLUSDT --days 30 --interval 5 \
        --rsi 45 --tp 2.5 --sl 3 --trailing 0.5 --usdt 20 --fee 0.1

    python3 backtest.py --symbol BTCUSDT --days 14 --interval 15 --out trades.csv
"""
import argparse
import time
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

from engine import generate_signal_from_series, _check_exit, calc_rsi, RSI_PERIOD, MAX_POINTS


# ───────────────────────────────────────────────────────────────
#  ЗАГРУЗКА ИСТОРИЧЕСКИХ ДАННЫХ
# ───────────────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval_min: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Тянет исторические свечи Bybit spot с пагинацией назад по времени (лимит 1000 свечей за запрос)."""
    session = HTTP(testnet=False)
    rows = []
    cursor_end = end_ms
    while cursor_end > start_ms:
        r = session.get_kline(category="spot", symbol=symbol, interval=interval_min,
                               start=start_ms, end=cursor_end, limit=1000)
        batch = r.get("result", {}).get("list", [])
        if not batch:
            break
        rows.extend(batch)
        oldest_ts = int(batch[-1][0])
        if oldest_ts <= start_ms or oldest_ts >= cursor_end:
            break
        cursor_end = oldest_ts - 1
        time.sleep(0.15)  # уважаем публичный rate-limit Bybit при пагинации

    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "turnover"])
    df = df.astype({"time": "int64", "open": float, "high": float, "low": float,
                     "close": float, "volume": float})
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    return df[["time", "open", "high", "low", "close", "volume"]]


# ───────────────────────────────────────────────────────────────
#  СИМУЛЯЦИЯ
# ───────────────────────────────────────────────────────────────
@dataclass
class BtConfig:
    """Зеркало UserConfig — те же параметры, что пользователь крутит в /settings."""
    rsi_entry: int = 45
    take_profit_pct: float = 2.5
    stop_loss_pct: float = 3.0
    trailing_pct: float = 0.5
    cooldown_bars: int = 10
    buy_usdt: float = 20.0
    fee_pct: float = 0.1  # комиссия тейкера за одну сторону сделки, % (Bybit spot default ~0.1%)


def run_backtest(df: pd.DataFrame, cfg: BtConfig) -> dict:
    """
    Бар-за-баром симуляция БЕЗ lookahead bias: на баре i сигнал считается
    ТОЛЬКО по данным, известным на закрытии бара i, и тут же по этой цене
    исполняется — так же, как live-движок реагирует на только что пришедший
    тик. Никакие будущие бары не участвуют в решении на баре i.
    """
    prices = df["close"].tolist()
    volumes = df["volume"].tolist()
    times = df["time"].tolist()

    # Те же скользящие окна ограниченного размера, что и в live-движке —
    # НЕ вся история целиком, иначе бэктест "видит" больше, чем реальный бот.
    price_win: deque = deque(maxlen=MAX_POINTS)
    vol_win: deque = deque(maxlen=MAX_POINTS)
    rsi_win: deque = deque(maxlen=MAX_POINTS)

    trades = []
    equity_curve = []  # (time, realized_pnl_к_этому_моменту)

    pos = SimpleNamespace(state="WAITING", entry_price=0.0, peak_price=0.0, qty=0.0)
    cooldown = 0
    realized_pnl = 0.0
    fee_mult = cfg.fee_pct / 100.0

    for i in range(len(prices)):
        cur_price = prices[i]
        price_win.append(cur_price)
        vol_win.append(volumes[i])

        if len(price_win) >= 23:  # EMA_SLOW(21) + 2, зеркалит условие в generate_signal_from_series
            rsi_win.append(calc_rsi(list(price_win), RSI_PERIOD))

        if cooldown > 0:
            cooldown -= 1

        # выход — идентичная live-движку функция _check_exit
        if pos.state == "HOLDING":
            ex_reason, _ = _check_exit(cfg, pos, cur_price)
            if ex_reason:
                gross = (cur_price - pos.entry_price) * pos.qty
                fee = (pos.entry_price * pos.qty + cur_price * pos.qty) * fee_mult
                pnl = gross - fee
                realized_pnl += pnl
                trades.append({"time": times[i], "side": f"SELL({ex_reason})",
                                "price": cur_price, "qty": pos.qty, "pnl": pnl})
                pos.state, pos.entry_price, pos.qty, pos.peak_price = "WAITING", 0.0, 0.0, 0.0
                cooldown = cfg.cooldown_bars
                equity_curve.append((times[i], realized_pnl))
                continue

        # вход — идентичная live-движку сигнальная функция
        if pos.state == "WAITING" and cooldown == 0:
            sd = generate_signal_from_series(list(price_win), list(vol_win), list(rsi_win), cfg.rsi_entry)
            if sd["signal"] == "BUY":
                qty = cfg.buy_usdt / cur_price
                pos.state, pos.entry_price, pos.qty, pos.peak_price = "HOLDING", cur_price, qty, cur_price
                trades.append({"time": times[i], "side": "BUY", "price": cur_price, "qty": qty, "pnl": None})

        equity_curve.append((times[i], realized_pnl))

    # незакрытая на конец периода позиция — не считаем прибылью/убытком,
    # честно помечаем отдельно, чтобы метрики не искажались "хотелками"
    open_unrealized = None
    if pos.state == "HOLDING" and prices:
        open_unrealized = (prices[-1] - pos.entry_price) * pos.qty

    return {"trades": trades, "equity_curve": equity_curve, "realized_pnl": realized_pnl,
            "still_open": pos.state == "HOLDING", "open_unrealized_pnl": open_unrealized}


# ───────────────────────────────────────────────────────────────
#  МЕТРИКИ
# ───────────────────────────────────────────────────────────────
def compute_metrics(result: dict, starting_equity: float) -> dict:
    closed = [t for t in result["trades"] if t["pnl"] is not None]
    n = len(closed)
    if n == 0:
        return {"сделок": 0,
                "примечание": "За период не было ни одной закрытой сделки — либо стратегия "
                               "слишком консервативна для этих настроек/периода, либо мало данных."}

    pnls = np.array([t["pnl"] for t in closed])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    win_rate = len(wins) / n * 100
    total_pnl = float(pnls.sum())
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    profit_factor = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else None

    eq = np.array([starting_equity + pnl for _, pnl in result["equity_curve"]])
    running_max = np.maximum.accumulate(eq)
    drawdown = eq - running_max
    max_dd_usd = float(drawdown.min()) if len(drawdown) else 0.0
    max_dd_pct = float((drawdown / running_max).min() * 100) if len(running_max) and running_max.max() > 0 else 0.0

    returns = pnls / starting_equity
    sharpe = float(returns.mean() / returns.std() * np.sqrt(n)) if returns.std() > 0 and n > 1 else 0.0

    return {
        "сделок закрыто": n,
        "win_rate_%": round(win_rate, 1),
        "итоговый_PnL_usd": round(total_pnl, 4),
        "итоговый_PnL_%_от_депозита": round(total_pnl / starting_equity * 100, 2),
        "средняя_прибыльная_usd": round(avg_win, 4),
        "средний_убыток_usd": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else "∞ (убыточных сделок не было)",
        "макс_просадка_usd": round(max_dd_usd, 4),
        "макс_просадка_%": round(max_dd_pct, 2),
        "sharpe_по_серии_сделок": round(sharpe, 2),
        "незакрытая_позиция_на_конец_периода": result["still_open"],
    }


# ───────────────────────────────────────────────────────────────
#  CLI
# ───────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Бэктест RSI+EMA стратегии на исторических данных Bybit spot")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--interval", default="5", help="Таймфрейм свечи в минутах Bybit: 1,3,5,15,30,60,120,240,D,...")
    p.add_argument("--rsi", type=int, default=45)
    p.add_argument("--tp", type=float, default=2.5)
    p.add_argument("--sl", type=float, default=3.0)
    p.add_argument("--trailing", type=float, default=0.5)
    p.add_argument("--cooldown", type=int, default=10)
    p.add_argument("--usdt", type=float, default=20.0)
    p.add_argument("--fee", type=float, default=0.1, help="Комиссия тейкера за сторону сделки, %%")
    p.add_argument("--equity", type=float, default=500.0, help="Виртуальный стартовый капитал для расчёта просадки/Sharpe в %%")
    p.add_argument("--out", default=None, help="Путь для сохранения журнала сделок в CSV")
    args = p.parse_args()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 24 * 60 * 60 * 1000

    print(f"📥 Загружаю {args.symbol}, {args.days}д, таймфрейм {args.interval}м ...")
    df = fetch_klines(args.symbol, args.interval, start_ms, end_ms)
    if df.empty:
        print("❌ Не удалось получить исторические данные (пусто). Проверь символ или сеть.")
        return
    print(f"✅ Получено {len(df)} свечей: {df['time'].iloc[0]} → {df['time'].iloc[-1]}")

    cfg = BtConfig(rsi_entry=args.rsi, take_profit_pct=args.tp, stop_loss_pct=args.sl,
                    trailing_pct=args.trailing, cooldown_bars=args.cooldown,
                    buy_usdt=args.usdt, fee_pct=args.fee)

    result = run_backtest(df, cfg)
    metrics = compute_metrics(result, args.equity)

    print("\n" + "=" * 56)
    print(f"РЕЗУЛЬТАТ БЭКТЕСТА: {args.symbol}  {args.days}д  TF={args.interval}м  "
          f"RSI<{args.rsi} TP={args.tp}% SL={args.sl}% TR={args.trailing}%")
    print("=" * 56)
    for k, v in metrics.items():
        print(f"{k:>40}: {v}")

    if result["still_open"]:
        print(f"\n⚠️  На конец периода осталась ОТКРЫТАЯ позиция, нереализованный PnL "
              f"~{result['open_unrealized_pnl']:+.4f}$ — НЕ включён в метрики выше.")

    if args.out and result["trades"]:
        pd.DataFrame(result["trades"]).to_csv(args.out, index=False)
        print(f"\n💾 Журнал сделок сохранён: {args.out}")


if __name__ == "__main__":
    main()
