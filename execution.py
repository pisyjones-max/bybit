"""
Всё общение с Bybit API живёт здесь: авторизованные сессии пользователей,
единая точка вызова с rate-limit/backoff (_bybit_call), идемпотентная
отправка ордеров (orderLinkId + сверка реального статуса при сетевом
сбое) и чтение баланса/остатков монет. engine.py вызывает функции этого
модуля, но сам никогда не обращается к pybit напрямую.
"""
import logging
import os
import random
import threading
import time
import uuid

from collections import deque

from pybit.unified_trading import HTTP

log = logging.getLogger("EXECUTION")

# ── Публичная сессия (без ключей) — тикеры/свечи, общие для всех ──
public_session = HTTP(testnet=False)

# ── Кэш авторизованных сессий по user_id, чтобы не пересоздавать HTTP каждый тик
_user_sessions: dict[int, HTTP] = {}


def get_user_session(user_id: int, api_key: str, api_secret: str) -> HTTP:
    if user_id not in _user_sessions:
        _user_sessions[user_id] = HTTP(testnet=False, api_key=api_key, api_secret=api_secret)
    return _user_sessions[user_id]


def invalidate_user_session(user_id: int):
    """Вызывать при смене API-ключей пользователем."""
    _user_sessions.pop(user_id, None)


# ───────────────────────────────────────────────────────────────
#  RATE-LIMIT / RETRY: единая точка вызова любого Bybit-эндпоинта
# ───────────────────────────────────────────────────────────────
# Все пользователи торгуют разными ключами, но публичные данные (тикеры,
# свечи) и общий IP всё равно делят один лимит запросов к Bybit. При росте
# числа активных аккаунтов/символов без этого движок рано или поздно
# поймает retCode rate-limit и просто перестанет торговать для всех —
# тихо и незаметно.
BYBIT_MAX_RPS = float(os.getenv("BYBIT_MAX_RPS", "8"))
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_WINDOW: deque = deque()
_RATE_LIMIT_RETCODES = {10006, 10018}  # rate limit / too many visits (Bybit v5)


def _rate_limit_gate():
    """Sliding-window ограничитель: не даёт превысить BYBIT_MAX_RPS запросов/сек ко всем Bybit-эндпоинтам суммарно."""
    with _RATE_LIMIT_LOCK:
        now = time.monotonic()
        while _RATE_LIMIT_WINDOW and now - _RATE_LIMIT_WINDOW[0] > 1.0:
            _RATE_LIMIT_WINDOW.popleft()
        if len(_RATE_LIMIT_WINDOW) >= BYBIT_MAX_RPS:
            sleep_for = 1.0 - (now - _RATE_LIMIT_WINDOW[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
        _RATE_LIMIT_WINDOW.append(time.monotonic())


def _bybit_call(fn, *args, _user_id=None, _what: str = "", _max_retries: int = 3, **kwargs) -> dict:
    """
    Прогоняет ЛЮБОЙ вызов pybit через общий rate-limit гейт и делает
    экспоненциальный backoff-retry при сетевых сбоях или rate-limit
    ответе биржи — вместо того чтобы падать с исключением и молча
    пропускать тик для пользователя.
    """
    last_exc = None
    resp = None
    for attempt in range(_max_retries):
        _rate_limit_gate()
        try:
            resp = fn(*args, **kwargs)
            ret_code = resp.get("retCode", 0) if isinstance(resp, dict) else 0
            ret_msg = str(resp.get("retMsg", "")) if isinstance(resp, dict) else ""
            if ret_code in _RATE_LIMIT_RETCODES or "too many" in ret_msg.lower():
                backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                log.warning(f"[user {_user_id}] rate-limit на {_what}, backoff {backoff:.1f}с "
                            f"(попытка {attempt + 1}/{_max_retries})")
                time.sleep(backoff)
                continue
            return resp
        except Exception as e:
            last_exc = e
            backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
            log.warning(f"[user {_user_id}] исключение на {_what}: {e} — retry через {backoff:.1f}с "
                        f"({attempt + 1}/{_max_retries})")
            time.sleep(backoff)
    if resp is not None:
        return resp  # последний ответ биржи (даже rate-limited) — вызывающий код сам решит, что делать
    raise last_exc if last_exc else RuntimeError(f"{_what}: исчерпаны попытки без ответа биржи")


# ───────────────────────────────────────────────────────────────
#  РЫНОЧНЫЕ ДАННЫЕ (публичный эндпоинт, ключ не нужен)
# ───────────────────────────────────────────────────────────────
def fetch_recent_klines(symbol: str, interval_min: str, limit: int = 3) -> list:
    """
    Последние `limit` свечей символа (сырые списки Bybit: [start, open,
    high, low, close, volume, turnover]), от старых к новым. Используется
    live-движком для опроса рынка — в отличие от backtest.fetch_klines,
    здесь не нужна пагинация, только "хвост" последних баров.
    """
    r = _bybit_call(public_session.get_kline, _what=f"kline_{symbol}",
                     category="spot", symbol=symbol, interval=interval_min, limit=limit)
    rows = r.get("result", {}).get("list", [])
    return sorted(rows, key=lambda row: int(row[0]))


# ───────────────────────────────────────────────────────────────
#  ОРДЕРА
# ───────────────────────────────────────────────────────────────
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


def _query_order_by_link_id(session: HTTP, user_id: int, symbol: str, link_id: str):
    """
    Проверяет реальный статус ордера по orderLinkId у биржи. Нужна, когда
    сеть оборвалась ПОСЛЕ отправки, но ДО получения ответа — в этот момент
    неизвестно, исполнился ордер или нет, а слепой повтор рискует
    продублировать сделку.
    """
    try:
        r = _bybit_call(session.get_order_history, _user_id=user_id, _what="order_history",
                         category="spot", symbol=symbol, orderLinkId=link_id)
        rows = (r or {}).get("result", {}).get("list", [])
        for o in rows:
            if o.get("orderStatus") in ("Filled", "PartiallyFilled"):
                avg = float(o.get("avgPrice") or 0)
                qty = float(o.get("cumExecQty") or 0)
                if qty > 0:
                    return avg, qty
    except Exception as e:
        log.error(f"[user {user_id}] query_order_by_link_id {symbol}: {e}")
    return None


def _submit_order_idempotent(session: HTTP, user_id: int, symbol: str, side: str,
                              order_type: str, qty_str: str, ref_price: float,
                              extra: dict | None = None) -> tuple[bool, float, float]:
    """
    Отправляет ордер с уникальным orderLinkId (Bybit сам отклонит повтор с
    тем же id как дубликат — защита от повторной отправки той же сделки).
    Если после отправки случилось сетевое исключение — НЕ считаем это
    автоматическим провалом: сначала спрашиваем у биржи реальный статус
    именно этого orderLinkId, и только если ордера там нет — считаем сделку
    несостоявшейся.
    """
    link_id = f"{side.lower()}-{user_id}-{symbol}-{uuid.uuid4().hex[:12]}"
    params = dict(category="spot", symbol=symbol, side=side, orderType=order_type,
                  qty=qty_str, orderLinkId=link_id)
    if extra:
        params.update(extra)

    r = None
    try:
        r = _bybit_call(session.place_order, _user_id=user_id, _what=f"{side} {symbol}", **params)
    except Exception as e:
        log.error(f"[user {user_id}] {side} {symbol} сетевая ошибка при отправке, "
                  f"проверяю реальный статус по orderLinkId: {e}")

    if r is not None and r.get("retCode") == 0:
        return True, ref_price, float(qty_str)

    # Не получили однозначный успех — либо сеть оборвалась, либо биржа
    # ответила "такой orderLinkId уже был" (наш же более ранний повтор).
    # В обоих случаях сверяемся с реальным статусом, а не гадаем.
    status = _query_order_by_link_id(session, user_id, symbol, link_id)
    if status:
        avg_price, filled_qty = status
        return True, (avg_price or ref_price), filled_qty

    if r is not None:
        log.error(f"[user {user_id}] {side} {symbol} FAIL: {r.get('retMsg')}")
    return False, 0.0, 0.0


def _place_buy(session: HTTP, user_id: int, symbol: str, price: float, usdt: float) -> tuple[bool, float, float]:
    qty_str = _qty_str(price, usdt)
    ok, fill_price, qty = _submit_order_idempotent(session, user_id, symbol, "Buy", "Market", qty_str, price)
    if ok:
        return True, fill_price, qty
    log.warning(f"[user {user_id}] Market BUY не прошёл {symbol} → пробую Limit")
    return _submit_order_idempotent(session, user_id, symbol, "Buy", "Limit", qty_str, price,
                                     extra={"price": str(round(price, 6)), "timeInForce": "GTC"})


def _get_coin_qty(session: HTTP, user_id: int, symbol: str) -> float:
    coin = symbol.replace("USDT", "")
    try:
        b = _bybit_call(session.get_wallet_balance, _user_id=user_id, _what="wallet_balance",
                         accountType="UNIFIED", coin=coin)
        return float(b["result"]["list"][0]["coin"][0]["walletBalance"])
    except Exception as e:
        log.error(f"[user {user_id}] coin_qty {symbol}: {e}")
        return 0.0


def _place_sell(session: HTTP, user_id: int, symbol: str, cur_price: float) -> tuple[bool, float, float]:
    raw_qty = _get_coin_qty(session, user_id, symbol)
    if raw_qty < 1e-6:
        return False, 0.0, 0.0
    qty_str = _safe_sell_qty(cur_price, raw_qty)
    return _submit_order_idempotent(session, user_id, symbol, "Sell", "Market", qty_str, cur_price)


def execute_buy(session: HTTP, user_id: int, symbol: str, price: float, usdt: float,
                 dry_run: bool) -> tuple[bool, float, float]:
    """
    dry_run=True: НИКАКОГО реального запроса на биржу. Симулируем полное
    исполнение маркет-ордера по текущей цене бара — так можно безопасно
    проверять стратегию и настройки на живом рынке, не рискуя деньгами.
    """
    if dry_run:
        qty = usdt / price
        return True, price, qty
    return _place_buy(session, user_id, symbol, price, usdt)


def execute_sell(session: HTTP, user_id: int, symbol: str, cur_price: float,
                  dry_run: bool, dry_qty: float) -> tuple[bool, float, float]:
    """dry_run=True: продаём симулированное количество (из pos.qty), без обращения к бирже."""
    if dry_run:
        if dry_qty < 1e-9:
            return False, 0.0, 0.0
        return True, cur_price, dry_qty
    return _place_sell(session, user_id, symbol, cur_price)


def get_usdt_balance(session: HTTP, user_id: int) -> float:
    try:
        b = _bybit_call(session.get_wallet_balance, _user_id=user_id, _what="usdt_balance",
                         accountType="UNIFIED", coin="USDT")
        return float(b["result"]["list"][0]["coin"][0]["walletBalance"])
    except Exception as e:
        log.error(f"[user {user_id}] balance error: {e}")
        return 0.0
