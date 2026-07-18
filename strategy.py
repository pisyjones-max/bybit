"""
Сигнальная стратегия: RSI + EMA + объёмный фильтр + RSI-дивергенция.

Всё в этом модуле — ЧИСТЫЕ функции: они не трогают базу данных, Bybit API
или любое глобальное изменяемое состояние движка. Единственный вход —
списки чисел (цены/объёмы/история RSI), единственный выход — dict с
сигналом. Это единственный источник истины для торговой логики: и
live-движок (engine.py, через тонкую обёртку generate_signal), и
бэктестер (backtest.py) вызывают ИМЕННО generate_signal_from_series.
Так гарантированно исключено расхождение между тем, что было проверено
на истории, и тем, что реально торгуется.
"""
import numpy as np
import pandas as pd

# ── Параметры стратегии (общие для live и backtest) ────────────────
MAX_POINTS = 200          # размер скользящего окна истории (баров), которое видит стратегия
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
VOL_SHOCK_MULT = 1.8
VOL_OK_MULT = 1.3


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
    """
    Отношение объёма последнего бара к среднему объёму предыдущих 20 баров.

    ВАЖНО (см. также docstring backtest.py): раньше live-движок брал сюда
    поле volume24h тикера (скользящий 24-часовой объём между тик-снимками),
    а бэктест — объём конкретной исторической свечи; это были РАЗНЫЕ по
    смыслу величины и являлось известным источником расхождения live/bt.
    После перехода live-движка на агрегированные OHLCV-свечи (get_kline)
    оба пути передают сюда одно и то же — объём завершившегося бара,
    поэтому это расхождение больше не существует.
    """
    if len(volumes) < 10:
        return 1.0
    avg = np.mean(list(volumes)[-21:-1])
    return float(volumes[-1]) / avg if avg > 0 else 1.0


def rsi_divergence(prices: list, rsis: list) -> bool:
    if len(prices) < 5 or len(rsis) < 5:
        return False
    p, r = list(prices), list(rsis)
    return (p[-1] < p[-3]) and (r[-1] > r[-3])


def generate_signal_from_series(prices: list, volumes: list, rsi_hist: list,
                                 rsi_entry_threshold: int) -> dict:
    """
    ЧИСТАЯ функция сигнала — не трогает никакое глобальное состояние.
    prices/volumes — цены закрытия и объёмы последовательных баров (или
    тиков, в исторических условиях запуска), rsi_hist — история уже
    посчитанных значений RSI за предыдущие бары (нужна для дивергенции).
    """
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
    diverg = rsi_divergence(prices, rsi_hist)

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
