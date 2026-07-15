import os
import logging

from flask import Flask, redirect, url_for, request
from flask_login import LoginManager, login_required, current_user
from dotenv import load_dotenv

load_dotenv()  # должен выполниться ДО импорта models (там читается MASTER_KEY)

from models import db, User  # noqa: E402
from auth import auth_bp  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

server = Flask(__name__)
server.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "change-me-in-.env")
server.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///novation.db")
server.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(server)
server.register_blueprint(auth_bp)

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.init_app(server)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@server.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("auth.login"))


@server.route("/dashboard")
@login_required
def dashboard():
    return redirect("/dashboard/")


# Защищаем сам Dash-роут: любой запрос к /dashboard/... требует логина
@server.before_request
def _protect_dash():
    if request.path.startswith("/dashboard/") and not current_user.is_authenticated:
        return redirect(url_for("auth.login"))


with server.app_context():
    db.create_all()

# Dash app + background engine imported after db is ready
from dashboard import create_dash_app  # noqa: E402
import engine  # noqa: E402

dash_app = create_dash_app(server)

# Только ОДИН процесс должен запускать движок — см. README про --workers 1
if os.getenv("RUN_ENGINE", "1") == "1":
    engine.start_engine(server)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    server.run(debug=False, host="0.0.0.0", port=port)
