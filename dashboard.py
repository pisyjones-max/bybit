import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output
from flask_login import current_user

import engine
from models import db, Position, Trade

C = {
    "bg": "#0b0e11", "card": "#141920", "bdr": "#1e2936",
    "gold": "#f0b90b", "green": "#00ff9d", "red": "#ff4d4d",
    "gray": "#6b7280", "blue": "#38bdf8", "purp": "#c084fc",
}

SIG_C = {"BUY": C["green"], "WEAK_BUY": "#86efac", "SELL": C["red"], "NEUTRAL": C["gray"]}
SIG_T = {"BUY": "🟢 BUY", "WEAK_BUY": "🟡 WEAK", "SELL": "🔴 SELL", "NEUTRAL": "⬜"}


def _card_style(extra=None):
    s = {"backgroundColor": C["card"], "border": f"1px solid {C['bdr']}",
         "borderRadius": "10px", "padding": "10px 14px"}
    if extra:
        s.update(extra)
    return s


def create_dash_app(flask_server):
    app = Dash(
        __name__,
        server=flask_server,
        url_base_pathname="/dashboard/",
        suppress_callback_exceptions=True,
        title="NOVATION Dashboard",
    )

    app.layout = html.Div(
        style={"backgroundColor": C["bg"], "color": "#eaecef", "padding": "16px",
               "fontFamily": "'Segoe UI', Arial, sans-serif", "minHeight": "100vh"},
        children=[
            html.Div([
                html.A("⚙ Настройки", href="/settings", style={
                    "color": C["gold"], "textDecoration": "none", "fontSize": "13px"}),
                html.Span("  ·  ", style={"color": C["gray"]}),
                html.A("Выйти", href="/logout", style={
                    "color": C["gray"], "textDecoration": "none", "fontSize": "13px"}),
            ], style={"textAlign": "right", "marginBottom": "10px"}),

            html.Div(id="user-banner", style={"marginBottom": "12px"}),
            html.Div(id="scanner", style={
                "display": "grid", "gridTemplateColumns": "repeat(3, 1fr)",
                "gap": "8px", "marginBottom": "12px"}),
            dcc.Graph(id="main-chart", config={"displayModeBar": False}),
            html.Div(id="trade-log-box", style={
                "fontSize": "12px", "lineHeight": "1.9", "fontFamily": "monospace",
                "marginTop": "12px"}),

            dcc.Interval(id="ui-tick", interval=3000, n_intervals=0),
        ]
    )

    @app.callback(
        Output("user-banner", "children"),
        Output("scanner", "children"),
        Output("main-chart", "figure"),
        Output("trade-log-box", "children"),
        Input("ui-tick", "n_intervals"),
    )
    def refresh(_n):
        if not current_user.is_authenticated:
            return "Не авторизован", [], go.Figure(), []

        cfg = current_user.config
        active_txt = "🟢 бот активен" if cfg.is_active else "⏸ бот выключен (включи в настройках)"
        banner = html.Div(
            f"{current_user.email} — {active_txt}",
            style={"color": C["gold"] if cfg.is_active else C["gray"], "fontWeight": "700"}
        )

        positions = {p.symbol: p for p in Position.query.filter_by(user_id=current_user.id).all()}

        cards = []
        for symbol in engine.SYMBOLS:
            h = engine.histories[symbol]
            if not h["price"]:
                cards.append(html.Div(symbol.replace("USDT", ""), style=_card_style({"textAlign": "center"})))
                continue

            sd = engine.generate_signal(symbol, cfg.rsi_entry)
            cur_p = list(h["price"])[-1]
            pos = positions.get(symbol)

            extra = {"textAlign": "center", "lineHeight": "1.7"}
            if sd["signal"] == "BUY":
                extra["backgroundColor"] = "#0d2018"
            elif pos and pos.state == "HOLDING":
                extra["backgroundColor"] = "#0d1f15"
            else:
                extra["backgroundColor"] = C["card"]

            pnl_el = ""
            if pos and pos.state == "HOLDING" and pos.entry_price:
                pnl_pct = (cur_p - pos.entry_price) / pos.entry_price * 100
                pnl_el = html.Div(f"P&L {pnl_pct:+.2f}%", style={
                    "fontSize": "11px", "fontWeight": "700",
                    "color": C["green"] if pnl_pct >= 0 else C["red"]})

            state_txt = "● В СДЕЛКЕ" if (pos and pos.state == "HOLDING") else "· ПОИСК"

            cards.append(html.Div([
                html.Div(symbol.replace("USDT", ""), style={"fontSize": "14px", "fontWeight": "900"}),
                html.Div(f"${cur_p:,.4f}", style={"fontSize": "13px", "fontWeight": "700"}),
                html.Div(f"RSI {sd['rsi']}", style={"fontSize": "11px", "color": C["gray"]}),
                html.Div(SIG_T.get(sd["signal"], ""), style={
                    "color": SIG_C.get(sd["signal"], C["gray"]), "fontSize": "11px", "fontWeight": "800"}),
                pnl_el,
                html.Div(state_txt, style={"fontSize": "9px", "color": C["gray"]}),
            ], style=_card_style(extra)))

        # график по первому символу с данными (упрощённо; можно добавить dropdown)
        fig = go.Figure()
        symbol = engine.SYMBOLS[2]  # SOLUSDT по умолчанию
        h = engine.histories[symbol]
        if len(h["price"]) > 3:
            fig.add_trace(go.Scatter(x=list(h["time"]), y=list(h["price"]),
                                      name=symbol, line=dict(color=C["gold"], width=2)))
        fig.update_layout(template="plotly_dark", plot_bgcolor="rgba(0,0,0,0)",
                          paper_bgcolor="rgba(0,0,0,0)", height=360,
                          margin=dict(l=10, r=60, t=20, b=30))

        trades = (Trade.query.filter_by(user_id=current_user.id)
                  .order_by(Trade.time.desc()).limit(10).all())
        rows = []
        for t in trades:
            pnl_str = f" | {t.pnl:+.4f}$" if t.pnl is not None else ""
            clr = C["green"] if (t.side == "BUY" or (t.pnl or 0) >= 0) else C["red"]
            rows.append(html.Div(
                f"{t.time.strftime('%H:%M:%S')} {t.side} {t.symbol.replace('USDT','')} ${t.price:.5f}{pnl_str}",
                style={"color": clr, "borderBottom": f"1px solid {C['bdr']}", "paddingBottom": "2px"}))
        if not rows:
            rows = [html.Div("Сделок пока нет", style={"color": C["gray"], "fontStyle": "italic"})]

        return banner, cards, fig, rows

    return app
