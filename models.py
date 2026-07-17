import logging
import os
from datetime import datetime

from cryptography.fernet import Fernet
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()
log = logging.getLogger("MIGRATIONS")

# ───────────────────────────────────────────────────────────────
#  ШИФРОВАНИЕ API-КЛЮЧЕЙ
# ───────────────────────────────────────────────────────────────
# MASTER_KEY должен быть постоянным base64 ключом Fernet.
# Сгенерировать один раз: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# и положить результат в переменную окружения MASTER_KEY.
_MASTER_KEY = os.getenv("MASTER_KEY")
if not _MASTER_KEY:
    raise RuntimeError(
        "MASTER_KEY не задан. Сгенерируй: "
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
        "и добавь в .env как MASTER_KEY=..."
    )
_fernet = Fernet(_MASTER_KEY.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()


# ───────────────────────────────────────────────────────────────
#  МОДЕЛИ
# ───────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    credential = db.relationship("ApiCredential", backref="user", uselist=False,
                                  cascade="all, delete-orphan")
    config = db.relationship("UserConfig", backref="user", uselist=False,
                              cascade="all, delete-orphan")
    positions = db.relationship("Position", backref="user", cascade="all, delete-orphan")
    trades = db.relationship("Trade", backref="user", cascade="all, delete-orphan")

    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


class ApiCredential(db.Model):
    """Bybit API ключи пользователя — хранятся зашифрованными."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    api_key_enc = db.Column(db.Text, nullable=False)
    api_secret_enc = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def set_keys(self, api_key: str, api_secret: str):
        self.api_key_enc = encrypt_secret(api_key)
        self.api_secret_enc = encrypt_secret(api_secret)

    def get_keys(self) -> tuple[str, str]:
        return decrypt_secret(self.api_key_enc), decrypt_secret(self.api_secret_enc)


class UserConfig(db.Model):
    """Персональные торговые параметры пользователя."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)

    is_active = db.Column(db.Boolean, default=False)   # бот торгует только если True
    is_dry_run = db.Column(db.Boolean, default=True)    # True = симуляция, ордера на биржу НЕ идут
    buy_usdt = db.Column(db.Float, default=20.0)
    rsi_entry = db.Column(db.Integer, default=45)
    take_profit_pct = db.Column(db.Float, default=2.5)
    stop_loss_pct = db.Column(db.Float, default=3.0)
    trailing_pct = db.Column(db.Float, default=0.5)
    max_open_positions = db.Column(db.Integer, default=1)
    max_daily_loss_usd = db.Column(db.Float, default=5.0)
    cooldown_bars = db.Column(db.Integer, default=10)


class Position(db.Model):
    """Текущее состояние позиции пользователя по конкретному символу."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    symbol = db.Column(db.String(20), nullable=False)

    state = db.Column(db.String(20), default="WAITING")  # WAITING | HOLDING
    entry_price = db.Column(db.Float, default=0.0)
    qty = db.Column(db.Float, default=0.0)
    peak_price = db.Column(db.Float, default=0.0)
    entry_time = db.Column(db.String(64), default="")
    cooldown = db.Column(db.Integer, default=0)
    daily_loss = db.Column(db.Float, default=0.0)
    last_trade_day = db.Column(db.String(20), default="")

    __table_args__ = (db.UniqueConstraint("user_id", "symbol", name="uq_user_symbol"),)


def run_light_migrations(engine):
    """
    Простая авто-миграция без Alembic: db.create_all() создаёт только
    отсутствующие ТАБЛИЦЫ, но не добавляет новые КОЛОНКИ в уже существующие
    таблицы. При обновлении кода на проде (уже есть instance/novation.db
    или Postgres с данными) — без этого приложение упадёт на первом же
    обращении к новой колонке. Вызывать один раз при старте, после create_all().
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    wanted_columns = {
        "user_config": {"is_dry_run": "BOOLEAN DEFAULT 1"},
        "trade": {"is_dry_run": "BOOLEAN DEFAULT 0 NOT NULL"},
    }
    with engine.begin() as conn:
        for table, columns in wanted_columns.items():
            if table not in inspector.get_table_names():
                continue  # таблицу create_all() ещё создаст с нуля — колонка уже будет в схеме
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_def in columns.items():
                if col_name not in existing:
                    log.info(f"[migration] добавляю колонку {table}.{col_name}")
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))


class Trade(db.Model):
    """Журнал сделок пользователя."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    time = db.Column(db.DateTime, default=datetime.utcnow)
    symbol = db.Column(db.String(20), nullable=False)
    side = db.Column(db.String(20), nullable=False)  # BUY | SELL(SL) | SELL(TP) | SELL(TRAILING)
    price = db.Column(db.Float, nullable=False)
    qty = db.Column(db.Float, nullable=False)
    pnl = db.Column(db.Float, nullable=True)
    is_dry_run = db.Column(db.Boolean, default=False, nullable=False)  # True = симулированная сделка, реальный ордер не отправлялся
