"""
Риск-менеджмент: лимиты на вход в сделку и условия выхода из позиции.

Ничего в этом модуле не обращается к Bybit API — только к БД (журнал
сделок Trade) и к объектам конфигурации/позиции, переданным вызывающим
кодом. Это позволяет backtest.py использовать ровно ту же функцию выхода
(_check_exit), что и live-движок, подсовывая вместо ORM-объекта Position
лёгкий SimpleNamespace с теми же полями (state/entry_price/peak_price).
"""
from datetime import datetime, date, time as dtime

from models import db, Trade


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


def _can_buy(cfg, pos, open_count: int, bal: float, today_loss: float) -> tuple[bool, str]:
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


def _check_exit(cfg, pos, price: float) -> tuple[str, str]:
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
