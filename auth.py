from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user

from models import db, User, ApiCredential, UserConfig
from engine import invalidate_user_session

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        if not email or not password:
            flash("Заполни email и пароль", "error")
            return render_template("register.html")
        if password != password2:
            flash("Пароли не совпадают", "error")
            return render_template("register.html")
        if User.query.filter_by(email=email).first():
            flash("Пользователь с таким email уже существует", "error")
            return render_template("register.html")

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()  # получить user.id

        # Пустые ключи по умолчанию — пользователь впишет свои в настройках
        cred = ApiCredential(user_id=user.id)
        cred.set_keys("", "")
        db.session.add(cred)

        cfg = UserConfig(user_id=user.id, is_active=False)
        db.session.add(cfg)

        db.session.commit()

        login_user(user)
        flash("Регистрация успешна. Добавь свои API-ключи Bybit в настройках.", "success")
        return redirect(url_for("auth.settings"))

    return render_template("register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("Неверный email или пароль", "error")

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    cfg = current_user.config
    cred = current_user.credential

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_keys":
            api_key = request.form.get("api_key", "").strip()
            api_secret = request.form.get("api_secret", "").strip()
            if api_key and api_secret:
                cred.set_keys(api_key, api_secret)
                invalidate_user_session(current_user.id)
                db.session.commit()
                flash("API-ключи обновлены", "success")

        elif action == "update_config":
            cfg.buy_usdt = float(request.form.get("buy_usdt", cfg.buy_usdt))
            cfg.rsi_entry = int(request.form.get("rsi_entry", cfg.rsi_entry))
            cfg.take_profit_pct = float(request.form.get("take_profit_pct", cfg.take_profit_pct))
            cfg.stop_loss_pct = float(request.form.get("stop_loss_pct", cfg.stop_loss_pct))
            cfg.trailing_pct = float(request.form.get("trailing_pct", cfg.trailing_pct))
            cfg.max_open_positions = int(request.form.get("max_open_positions", cfg.max_open_positions))
            cfg.max_daily_loss_usd = float(request.form.get("max_daily_loss_usd", cfg.max_daily_loss_usd))
            cfg.cooldown_bars = int(request.form.get("cooldown_bars", cfg.cooldown_bars))
            cfg.is_active = request.form.get("is_active") == "on"
            cfg.is_dry_run = request.form.get("is_dry_run") == "on"
            db.session.commit()
            flash("Настройки сохранены", "success")

        return redirect(url_for("auth.settings"))

    has_keys = bool(cred and cred.api_key_enc and decrypt_ok(cred))
    return render_template("settings.html", cfg=cfg, has_keys=has_keys)


def decrypt_ok(cred: ApiCredential) -> bool:
    try:
        k, s = cred.get_keys()
        return bool(k and s)
    except Exception:
        return False
