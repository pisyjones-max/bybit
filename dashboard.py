import base64

import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output, State
from flask_login import current_user

import engine
from models import db, Position, Trade

C = {
    "bg": "#05070a", "card": "#12161c", "bdr": "#1e2936",
    "gold": "#f0b90b", "green": "#00ff9d", "red": "#ff4d4d",
    "gray": "#6b7280", "blue": "#38bdf8", "purp": "#c084fc", "dim": "#1e2936",
}
SIG_C = {"BUY": C["green"], "WEAK_BUY": "#86efac", "SELL": C["red"], "NEUTRAL": C["gray"]}
SIG_T = {"BUY": "🟢 BUY", "WEAK_BUY": "🟡 WEAK", "SELL": "🔴 SELL", "NEUTRAL": "⬜"}

GLOBAL_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700;900&family=JetBrains+Mono:wght@400;600;700&display=swap');

* { box-sizing: border-box; }

body {
    background:
        radial-gradient(circle at 12% -10%, rgba(240,185,11,.08), transparent 40%),
        radial-gradient(circle at 90% 110%, rgba(56,189,248,.07), transparent 45%),
        repeating-linear-gradient(0deg, rgba(255,255,255,.022) 0px, rgba(255,255,255,.022) 1px, transparent 1px, transparent 34px),
        repeating-linear-gradient(90deg, rgba(255,255,255,.022) 0px, rgba(255,255,255,.022) 1px, transparent 1px, transparent 34px),
        #05070a !important;
}

@keyframes pulse-green {
    0%   { box-shadow: 0 0 0 0 rgba(0,255,157,0.7), 0 0 22px rgba(0,255,157,.25); }
    70%  { box-shadow: 0 0 0 16px rgba(0,255,157,0), 0 0 22px rgba(0,255,157,.25); }
    100% { box-shadow: 0 0 0 0 rgba(0,255,157,0), 0 0 22px rgba(0,255,157,.25); }
}
@keyframes pulse-purp {
    0%   { box-shadow: 0 0 0 0 rgba(192,132,252,0.8), 0 0 22px rgba(192,132,252,.3); }
    70%  { box-shadow: 0 0 0 20px rgba(192,132,252,0), 0 0 22px rgba(192,132,252,.3); }
    100% { box-shadow: 0 0 0 0 rgba(192,132,252,0), 0 0 22px rgba(192,132,252,.3); }
}
@keyframes tape-scroll {
    0%   { transform: translateX(0); }
    100% { transform: translateX(-50%); }
}
@keyframes live-blink {
    0%, 100% { opacity: 1; box-shadow: 0 0 8px 2px rgba(0,255,157,.7); }
    50%      { opacity: .35; box-shadow: 0 0 2px 0 rgba(0,255,157,.3); }
}
@keyframes logo-glow {
    0%, 100% { text-shadow: 0 0 14px rgba(240,185,11,.55), 0 0 28px rgba(240,185,11,.2); }
    50%      { text-shadow: 0 0 20px rgba(240,185,11,.8), 0 0 40px rgba(240,185,11,.35); }
}
@keyframes rise-fade {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0); }
}

.pulse-buy  { animation: pulse-green 1.2s ease-out infinite; border: 2px solid #00ff9d !important; }
.pulse-purp { animation: pulse-purp  1.0s ease-out infinite; border: 2px solid #c084fc !important; }
.tape-wrap  { overflow: hidden; white-space: nowrap; }
.tape-inner { display: inline-block; animation: tape-scroll 28s linear infinite; }

.nova-logo { animation: logo-glow 3.2s ease-in-out infinite; }
.live-dot  { width: 7px; height: 7px; border-radius: 50%; background: #00ff9d;
             display: inline-block; margin-right: 6px; animation: live-blink 1.6s ease-in-out infinite; }

.nova-card {
    backdrop-filter: blur(10px) saturate(130%);
    -webkit-backdrop-filter: blur(10px) saturate(130%);
    transition: transform .2s cubic-bezier(.16,1,.3,1), box-shadow .2s ease, background-color .3s ease, border-color .2s ease;
}
.nova-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 14px 32px rgba(0,0,0,.45), 0 0 0 1px rgba(255,255,255,.04) inset;
    border-color: rgba(240,185,11,.25) !important;
}
.nova-card, .nova-card * { font-variant-numeric: tabular-nums; }

.scanner-grid > div { animation: rise-fade .35s cubic-bezier(.16,1,.3,1) both; }

@keyframes shimmer { 0% { background-position: -300px 0; } 100% { background-position: 300px 0; } }
.skeleton {
    background: linear-gradient(90deg, #12161c 25%, #1c232c 37%, #12161c 63%);
    background-size: 400px 100%;
    animation: shimmer 1.4s ease-in-out infinite;
    border-radius: 8px;
    color: transparent !important;
}

:focus-visible { outline: 2px solid #f0b90b; outline-offset: 2px; border-radius: 4px; }
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
}

.rc-slider-track { background: linear-gradient(90deg, #f0b90b, #ffd23f) !important; height: 5px !important; }
.rc-slider-rail  { background: #1e2936 !important; height: 5px !important; }
.rc-slider-handle {
    border: 2px solid #f0b90b !important; background: #0d1218 !important;
    box-shadow: 0 0 10px rgba(240,185,11,.6) !important; width: 16px !important; height: 16px !important;
    margin-top: -6px !important;
}
.rc-slider-handle:hover, .rc-slider-handle-dragging {
    border-color: #ffd23f !important; box-shadow: 0 0 16px rgba(240,185,11,.9) !important;
}
.rc-slider-mark-text { color: #6b7280 !important; font-family: 'JetBrains Mono', monospace !important; font-size: 10px !important; }
.rc-slider-tooltip-inner {
    background: #f0b90b !important; color: #0b0e11 !important; font-weight: 800 !important;
    font-family: 'JetBrains Mono', monospace !important; box-shadow: 0 4px 14px rgba(0,0,0,.4) !important;
}
.rc-slider-tooltip-arrow { border-top-color: #f0b90b !important; }

input[type="checkbox"] { accent-color: #f0b90b; width: 15px; height: 15px; cursor: pointer; }

#trade-log-box::-webkit-scrollbar { width: 6px; }
#trade-log-box::-webkit-scrollbar-track { background: transparent; }
#trade-log-box::-webkit-scrollbar-thumb { background: #1e2936; border-radius: 4px; }

.help-badge {
    display: inline-flex; align-items: center; gap: 5px; font-size: 11px;
    color: #f0b90b; border: 1px solid rgba(240,185,11,.35); padding: 4px 10px;
    border-radius: 20px; background: rgba(240,185,11,.06); text-decoration: none;
}
.help-badge:hover { background: rgba(240,185,11,.14); }
"""


def _make_sine_wav_b64(freq: float, duration: float = 0.22, volume: float = 0.4, rate: int = 22050) -> str:
    import struct, math
    n = int(rate * duration)
    s = [int(32767 * volume * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]
    ds = n * 2
    h = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + ds, b'WAVE', b'fmt ', 16,
                     1, 1, rate, rate * 2, 2, 16, b'data', ds)
    return base64.b64encode(h + struct.pack(f'<{n}h', *s)).decode()


_BUY_WAV = _make_sine_wav_b64(880, 0.20)
_SELL_WAV = _make_sine_wav_b64(523, 0.25)

# Отслеживаем последнюю увиденную сделку по каждому пользователю, чтобы понять,
# когда проигрывать звук (движок торгует в фоне независимо от того, кто смотрит).
_last_seen_trade_id: dict[int, int] = {}


def _card_style(extra=None):
    s = {"backgroundColor": C["card"], "border": f"1px solid {C['bdr']}",
         "borderRadius": "12px", "padding": "10px 14px",
         "boxShadow": "0 8px 24px rgba(0,0,0,.35)"}
    if extra:
        s.update(extra)
    return s


def _audio_el(elem_id, b64):
    return html.Audio(id=elem_id, src=f"data:audio/wav;base64,{b64}",
                       controls=False, autoPlay=False, style={"display": "none"})


_GLOW = {
    "white": "rgba(255,255,255,.25)", "#eaecef": "rgba(255,255,255,.25)",
    C["green"]: "rgba(0,255,157,.5)", C["blue"]: "rgba(56,189,248,.5)",
    C["gold"]: "rgba(240,185,11,.5)", C["purp"]: "rgba(192,132,252,.5)",
}


def _stat(label, elem_id, color="#eaecef"):
    glow = _GLOW.get(color, "rgba(255,255,255,.2)")
    return html.Div([
        html.Div(label, style={"fontSize": "10px", "color": C["gray"],
                               "textTransform": "uppercase", "letterSpacing": "1.5px",
                               "fontFamily": "'Space Grotesk', sans-serif", "fontWeight": "600"}),
        html.Div("—", id=elem_id, style={
            "color": color, "fontWeight": "800", "fontSize": "21px", "marginTop": "2px",
            "fontFamily": "'JetBrains Mono', monospace",
            "textShadow": f"0 0 14px {glow}"}),
    ], style={"textAlign": "center"})


def _vdiv():
    return html.Div(style={"width": "1px", "backgroundColor": C["bdr"], "margin": "0 4px"})


def _lbl(text):
    return html.Div(text, style={"fontSize": "11px", "color": C["gray"], "marginTop": "8px",
                                  "textTransform": "uppercase", "letterSpacing": "0.5px"})


def create_dash_app(flask_server):
    app = Dash(__name__, server=flask_server, url_base_pathname="/dashboard/",
               suppress_callback_exceptions=True, title="NOVATION Dashboard")

    app.index_string = f"""<!DOCTYPE html>
<html>
<head>
    {{%metas%}}<title>{{%title%}}</title>{{%favicon%}}{{%css%}}
    <style>{GLOBAL_CSS}</style>
</head>
<body>
    {{%app_entry%}}
    <footer>{{%config%}}{{%scripts%}}{{%renderer%}}</footer>
    <script>
    const _obs = new MutationObserver(ms => ms.forEach(m => {{
        const el = m.target;
        if (el.getAttribute('data-play') === '1') {{
            el.currentTime = 0;
            el.play().catch(()=>{{}});
            el.setAttribute('data-play','0');
        }}
    }}));
    document.addEventListener('DOMContentLoaded', () => {{
        ['snd-buy','snd-sell'].forEach(id => {{
            const el = document.getElementById(id);
            if (el) _obs.observe(el, {{attributes:true, attributeFilter:['data-play']}});
        }});
    }});
    </script>
</body>
</html>"""

    app.layout = html.Div(
        style={"backgroundColor": "transparent", "color": "#eaecef", "padding": "14px 18px",
               "fontFamily": "'Space Grotesk', 'Segoe UI', Arial, sans-serif",
               "minHeight": "100vh", "userSelect": "none"},
        children=[
            _audio_el("snd-buy", _BUY_WAV),
            _audio_el("snd-sell", _SELL_WAV),

            # ── ШАПКА ──
            html.Div([
                html.Div([
                    html.Div("⚡ NOVATION", className="nova-logo",
                             style={"fontSize": "26px", "fontWeight": "900", "color": C["gold"],
                                    "letterSpacing": "3px", "fontFamily": "'Space Grotesk', sans-serif"}),
                    html.Div(id="user-line", style={"fontSize": "11px", "color": C["gray"],
                                                      "marginTop": "2px"}),
                ], style={"flex": "1"}),
                html.Div([
                    _stat("СТАВКА", "hdr-stake", "white"),
                    _vdiv(),
                    _stat("ПРОФИТ", "hdr-profit", C["green"]),
                    _vdiv(),
                    _stat("БАЛАНС", "hdr-balance", C["blue"]),
                    _vdiv(),
                    _stat("ПОЗИЦИЙ", "hdr-positions", C["gold"]),
                    _vdiv(),
                    _stat("СДЕЛОК", "hdr-trades", C["purp"]),
                ], style={"display": "flex", "gap": "18px", "alignItems": "center",
                          "backgroundColor": C["card"], "padding": "10px 18px",
                          "borderRadius": "12px", "border": f"1px solid {C['bdr']}",
                          "boxShadow": "0 8px 24px rgba(0,0,0,.35)"}),
                html.Div([
                    html.A("❔ Как это работает", href="/help", target="_blank", className="help-badge"),
                    html.Div([
                        html.A("⚙ Настройки API-ключей", href="/settings", style={
                            "color": C["gold"], "textDecoration": "none", "fontSize": "12px"}),
                        html.Span(" · ", style={"color": C["gray"]}),
                        html.A("Выйти", href="/logout", style={
                            "color": C["gray"], "textDecoration": "none", "fontSize": "12px"}),
                    ], style={"marginTop": "8px"}),
                ], style={"marginLeft": "16px", "textAlign": "right"}),
            ], style={"display": "flex", "alignItems": "center",
                      "borderBottom": f"1px solid {C['bdr']}", "paddingBottom": "12px", "marginBottom": "12px"}),

            # ── ЛЕНТА ──
            html.Div([
                html.Div([
                    html.Span(className="live-dot"),
                    html.Span("LIVE FEED ▶", style={"color": C["gold"], "fontSize": "11px",
                                                      "fontFamily": "'JetBrains Mono', monospace",
                                                      "fontWeight": "700"}),
                ], style={"padding": "0 14px 0 0", "flexShrink": "0", "display": "flex", "alignItems": "center"}),
                html.Div(className="tape-wrap", style={"flex": "1", "overflow": "hidden"}, children=[
                    html.Div(id="live-tape", className="tape-inner",
                             style={"color": C["green"], "fontSize": "12px",
                                    "fontFamily": "'JetBrains Mono', monospace"})
                ])
            ], style={"display": "flex", "alignItems": "center", "backgroundColor": C["card"],
                      "borderRadius": "8px", "border": f"1px solid {C['bdr']}", "padding": "7px 14px",
                      "marginBottom": "12px", "height": "34px"}),

            # ── СКАНЕР ──
            html.Div(id="scanner", className="scanner-grid",
                      style={"display": "grid", "gridTemplateColumns": "repeat(3, 1fr)",
                             "gap": "8px", "marginBottom": "12px"}),

            # ── ОСНОВНОЙ БЛОК: график + панель ──
            html.Div([
                html.Div([
                    html.Div([
                        dcc.Dropdown(id="sym-sel",
                                     options=[{"label": s, "value": s} for s in engine.SYMBOLS],
                                     value="SOLUSDT", clearable=False,
                                     style={"width": "160px", "color": "#000", "fontSize": "13px"}),
                        html.Div(id="chart-price", style={"fontSize": "28px", "fontWeight": "900",
                                                            "color": C["gold"], "marginLeft": "16px",
                                                            "fontFamily": "'JetBrains Mono', monospace",
                                                            "textShadow": f"0 0 18px {_GLOW.get(C['gold'])}"}),
                        html.Div(id="chart-signal", style={"marginLeft": "16px", "fontSize": "13px",
                                                              "fontWeight": "700"}),
                    ], style={"display": "flex", "alignItems": "center", "marginBottom": "8px"}),
                    dcc.Graph(id="main-chart", config={"displayModeBar": False}),
                ], style=_card_style({"flex": "1"})),

                html.Div([
                    html.Div([
                        html.Div([
                            html.Span("⚙ ПАРАМЕТРЫ", style={"fontSize": "11px", "color": C["gray"],
                                                              "letterSpacing": "2px"}),
                            html.A("❔", href="/help#settings", target="_blank", title="Что означают эти настройки",
                                   style={"color": C["gold"], "marginLeft": "8px", "textDecoration": "none",
                                          "fontSize": "12px"}),
                        ], style={"marginBottom": "6px"}),

                        html.Div([
                            html.Span("Бот активен", style={"fontSize": "12px"}),
                            dcc.Checklist(id="chk-active", options=[{"label": "", "value": "on"}],
                                          value=["on"], style={"display": "inline-block", "marginLeft": "8px"}),
                        ], style={"marginBottom": "6px"}),

                        _lbl("Размер позиции (USDT)"),
                        dcc.Input(id="inp-usdt", type="number", min=2, step=0.5, value=20.0, style={
                            "width": "100%", "padding": "8px", "marginTop": "4px",
                            "backgroundColor": C["dim"], "color": "#eaecef",
                            "border": f"1px solid {C['bdr']}", "borderRadius": "6px",
                            "boxSizing": "border-box", "fontSize": "14px", "fontWeight": "600"}),

                        _lbl("RSI вход (порог ↓)"),
                        dcc.Slider(id="sl-rsi", min=20, max=60, step=1, value=45,
                                   marks={20: "20", 40: "40", 60: "60"},
                                   tooltip={"placement": "bottom", "always_visible": True}),

                        _lbl("Take Profit (%)"),
                        dcc.Slider(id="sl-tp", min=0.5, max=15, step=0.5, value=2.5,
                                   marks={1: "1", 5: "5", 10: "10", 15: "15"},
                                   tooltip={"placement": "bottom", "always_visible": True}),

                        _lbl("Stop Loss (%)"),
                        dcc.Slider(id="sl-sl", min=0.5, max=8, step=0.5, value=3.0,
                                   marks={1: "1", 4: "4", 8: "8"},
                                   tooltip={"placement": "bottom", "always_visible": True}),

                        _lbl("Макс. позиций"),
                        dcc.Slider(id="sl-maxpos", min=1, max=3, step=1, value=1,
                                   marks={1: "1", 2: "2", 3: "3"},
                                   tooltip={"placement": "bottom", "always_visible": True}),
                    ], style=_card_style({"marginBottom": "10px"})),

                    html.Div([
                        html.Div("📝 СДЕЛКИ", style={"fontSize": "11px", "color": C["gray"],
                                                        "marginBottom": "6px", "letterSpacing": "2px"}),
                        html.Div(id="trade-log-box", style={"fontSize": "12px", "lineHeight": "1.9",
                                                              "maxHeight": "220px", "overflowY": "auto",
                                                              "fontFamily": "monospace"})
                    ], style=_card_style()),
                ], style={"width": "260px", "marginLeft": "12px", "display": "flex", "flexDirection": "column"}),
            ], style={"display": "flex", "flex": "1"}),

            dcc.Interval(id="ui-tick", interval=3000, n_intervals=0),
            dcc.Interval(id="cfg-load-tick", interval=3000, n_intervals=0, max_intervals=1),
        ]
    )

    # ── Загрузка текущих настроек пользователя в контролы при открытии страницы ──
    @app.callback(
        Output("chk-active", "value"),
        Output("inp-usdt", "value"),
        Output("sl-rsi", "value"),
        Output("sl-tp", "value"),
        Output("sl-sl", "value"),
        Output("sl-maxpos", "value"),
        Input("cfg-load-tick", "n_intervals"),
    )
    def load_initial_config(_n):
        if not current_user.is_authenticated:
            return ["on"], 20.0, 45, 2.5, 3.0, 1
        cfg = current_user.config
        return (["on"] if cfg.is_active else [],
                cfg.buy_usdt, cfg.rsi_entry, cfg.take_profit_pct, cfg.stop_loss_pct, cfg.max_open_positions)

    # ── Сохранение параметров при изменении любого контрола ──
    @app.callback(
        Output("hdr-stake", "children"),  # используем как безобидный "выход" для запуска сохранения
        Input("chk-active", "value"),
        Input("inp-usdt", "value"),
        Input("sl-rsi", "value"),
        Input("sl-tp", "value"),
        Input("sl-sl", "value"),
        Input("sl-maxpos", "value"),
    )
    def save_config(active_val, usdt, rsi, tp, sl, maxpos):
        if not current_user.is_authenticated:
            return "—"
        cfg = current_user.config
        cfg.is_active = bool(active_val)
        if usdt is not None:
            cfg.buy_usdt = float(usdt)
        if rsi is not None:
            cfg.rsi_entry = int(rsi)
        if tp is not None:
            cfg.take_profit_pct = float(tp)
        if sl is not None:
            cfg.stop_loss_pct = float(sl)
        if maxpos is not None:
            cfg.max_open_positions = int(maxpos)
        db.session.commit()
        return f"{cfg.buy_usdt:.1f}$"

    # ── Основной цикл обновления интерфейса ──
    @app.callback(
        Output("user-line", "children"),
        Output("hdr-profit", "children"),
        Output("hdr-profit", "style"),
        Output("hdr-balance", "children"),
        Output("hdr-positions", "children"),
        Output("hdr-trades", "children"),
        Output("live-tape", "children"),
        Output("scanner", "children"),
        Output("main-chart", "figure"),
        Output("chart-price", "children"),
        Output("chart-signal", "children"),
        Output("trade-log-box", "children"),
        Output("snd-buy", "data-play"),
        Output("snd-sell", "data-play"),
        Input("ui-tick", "n_intervals"),
        Input("sym-sel", "value"),
    )
    def refresh(_n, symbol):
        if not current_user.is_authenticated:
            return "", "", {}, "", "", "", "", [], go.Figure(), "", "", [], "0", "0"

        cfg = current_user.config
        uid = current_user.id

        status = ('🧪 DRY-RUN (симуляция)' if cfg.is_dry_run and cfg.is_active
                   else '🟢 бот активен' if cfg.is_active else '⏸ бот выключен')
        user_line = f"{current_user.email} — {status}"

        positions = {p.symbol: p for p in Position.query.filter_by(user_id=uid).all()}
        open_count = sum(1 for p in positions.values() if p.state == "HOLDING")

        # ── звук при новой сделке (движок торгует в фоне сам) ──
        sound_buy, sound_sell = "0", "0"
        latest_trade = (Trade.query.filter_by(user_id=uid).order_by(Trade.id.desc()).first())
        prev_id = _last_seen_trade_id.get(uid)
        if latest_trade and latest_trade.id != prev_id:
            if prev_id is not None:  # не проигрывать звук при первой загрузке страницы
                if latest_trade.side == "BUY":
                    sound_buy = "1"
                else:
                    sound_sell = "1"
            _last_seen_trade_id[uid] = latest_trade.id

        # ── карточки сканера ──
        cards = []
        for s in engine.SYMBOLS:
            h = engine.histories[s]
            if not h["price"]:
                cards.append(html.Div(s.replace("USDT", ""), style=_card_style({"textAlign": "center"})))
                continue

            sd = engine.generate_signal(s, cfg.rsi_entry)
            cur_p = list(h["price"])[-1]
            pos = positions.get(s)
            sig = sd["signal"]

            extra = {"textAlign": "center", "lineHeight": "1.7", "transition": "all 0.3s"}
            card_cls = "nova-card"
            if sd["vol_shock"]:
                extra["backgroundColor"] = "#1a0d2e"
                card_cls += " pulse-purp"
            elif sig == "BUY":
                extra["backgroundColor"] = "#0d2018"
                card_cls += " pulse-buy"
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

            vol_badge = (html.Div(f"⚡ VOL ×{sd['vol_ratio']:.1f}", style={
                "fontSize": "10px", "color": C["purp"], "fontWeight": "700"}) if sd["vol_shock"] else "")

            state_txt = ("● В СДЕЛКЕ" if (pos and pos.state == "HOLDING")
                         else (f"⏳ {pos.cooldown}т" if pos and pos.cooldown > 0 else "· ПОИСК"))

            price_fmt = (f"${cur_p:,.6f}" if cur_p < 0.001 else f"${cur_p:,.4f}" if cur_p < 0.1
                         else f"${cur_p:,.3f}" if cur_p < 10 else f"${cur_p:,.2f}")

            sig_color = SIG_C.get(sig, C["gray"])
            cards.append(html.Div([
                html.Div(s.replace("USDT", ""), style={"fontSize": "14px", "fontWeight": "900",
                                                          "fontFamily": "'Space Grotesk', sans-serif",
                                                          "letterSpacing": "1px"}),
                html.Div(price_fmt, style={"fontSize": "13px", "fontWeight": "700",
                                            "fontFamily": "'JetBrains Mono', monospace",
                                            "textShadow": f"0 0 10px {_GLOW.get(C['gold'])}"}),
                html.Div(f"RSI {sd['rsi']}", style={"fontSize": "11px", "color": C["gray"],
                                                      "fontFamily": "'JetBrains Mono', monospace"}),
                html.Div(SIG_T.get(sig, ""), style={"color": sig_color,
                                                       "fontSize": "11px", "fontWeight": "800",
                                                       "textShadow": f"0 0 10px {_GLOW.get(sig_color, 'rgba(255,255,255,.2)')}"}),
                pnl_el, vol_badge,
                html.Div(state_txt, style={"fontSize": "9px",
                                            "color": C["gold"] if (pos and pos.state == "HOLDING") else C["gray"]}),
            ], className=card_cls, style=_card_style(extra)))

        # ── график выбранного символа ──
        h = engine.histories[symbol]
        fig = go.Figure()
        lp_text, sig_el = "", ""

        if len(h["price"]) > 3:
            prices = list(h["price"])
            times = list(h["time"])
            ema_f = engine.calc_ema(prices, engine.EMA_FAST)
            ema_s = engine.calc_ema(prices, engine.EMA_SLOW)

            fig.add_trace(go.Scatter(x=times, y=prices, name="Price",
                                      line=dict(color=C["gold"], width=2.5),
                                      hovertemplate="$%{y:,.6f}<extra></extra>"))
            fig.add_trace(go.Scatter(x=times, y=ema_f, name=f"EMA{engine.EMA_FAST}",
                                      line=dict(color=C["blue"], width=1.2, dash="dot")))
            fig.add_trace(go.Scatter(x=times, y=ema_s, name=f"EMA{engine.EMA_SLOW}",
                                      line=dict(color="#fb923c", width=1.2, dash="dot")))

            pos = positions.get(symbol)
            if pos and pos.state == "HOLDING":
                entry, peak = pos.entry_price, pos.peak_price
                sl_p = entry * (1 - cfg.stop_loss_pct / 100)
                tp_p = entry * (1 + cfg.take_profit_pct / 100)
                tr_p = peak * (1 - cfg.trailing_pct / 100)

                fig.add_hline(y=entry, line_color="white", line_dash="dash", line_width=1.2,
                              annotation_text="ENTRY", annotation_font_color="white")
                fig.add_hline(y=sl_p, line_color=C["red"], line_dash="dash", line_width=1.2,
                              annotation_text=f"SL {sl_p:.6f}", annotation_font_color=C["red"])
                fig.add_hline(y=tp_p, line_color=C["green"], line_dash="dash", line_width=1.2,
                              annotation_text=f"TP {tp_p:.6f}", annotation_font_color=C["green"])
                if peak > entry * 1.005:
                    fig.add_hline(y=tr_p, line_color=C["purp"], line_dash="dot", line_width=1,
                                  annotation_text=f"TRAIL {tr_p:.6f}", annotation_font_color=C["purp"])

            trades = Trade.query.filter_by(user_id=uid, symbol=symbol).all()
            buy_t = [t.time for t in trades if t.side == "BUY"]
            buy_p = [t.price for t in trades if t.side == "BUY"]
            sell_t = [t.time for t in trades if "SELL" in t.side]
            sell_p = [t.price for t in trades if "SELL" in t.side]

            if buy_t:
                fig.add_trace(go.Scatter(x=buy_t, y=buy_p, mode="markers", name="BUY",
                                          marker=dict(symbol="triangle-up", size=16, color=C["green"],
                                                      line=dict(width=1, color="white"))))
            if sell_t:
                fig.add_trace(go.Scatter(x=sell_t, y=sell_p, mode="markers", name="SELL",
                                          marker=dict(symbol="triangle-down", size=16, color=C["red"],
                                                      line=dict(width=1, color="white"))))

            lp = prices[-1]
            lp_text = (f"${lp:,.6f}" if lp < 0.001 else f"${lp:,.4f}" if lp < 0.1
                       else f"${lp:,.3f}" if lp < 10 else f"${lp:,.2f}")
            sd = engine.generate_signal(symbol, cfg.rsi_entry)
            sig_el = html.Span(f"{SIG_T.get(sd['signal'],'')} | {sd['desc']}",
                                style={"color": SIG_C.get(sd["signal"], C["gray"])})

        fig.update_layout(template="plotly_dark", plot_bgcolor="rgba(0,0,0,0)",
                          paper_bgcolor="rgba(0,0,0,0)", margin=dict(l=10, r=80, t=16, b=36), height=400,
                          legend=dict(orientation="h", y=1.1, x=0, font=dict(size=11)),
                          yaxis=dict(side="right", gridcolor="#1e2936", tickfont=dict(size=11)),
                          xaxis=dict(gridcolor="#1e2936"), hovermode="x unified",
                          font=dict(family="Segoe UI, Arial"))

        # ── журнал сделок ──
        trades = Trade.query.filter_by(user_id=uid).order_by(Trade.time.desc()).limit(10).all()
        rows = []
        for t in trades:
            pnl_str = f" | {t.pnl:+.4f}$" if t.pnl is not None else ""
            clr = C["green"] if (t.side == "BUY" or (t.pnl or 0) >= 0) else C["red"]
            rows.append(html.Div(
                f"{t.time.strftime('%H:%M:%S')} {t.side} {t.symbol.replace('USDT','')} ${t.price:.5f}{pnl_str}",
                style={"color": clr, "borderBottom": f"1px solid {C['bdr']}", "paddingBottom": "2px"}))
        if not rows:
            rows = [html.Div("Сделок пока нет", style={"color": C["gray"], "fontStyle": "italic"})]

        # ── лента: общие события сканирования + личные события пользователя ──
        personal = engine.live_status.get(uid, [])
        tape_all = list(engine.tape_events)[-8:] + personal[-8:]
        tape_str = "   ·   ".join(tape_all) if tape_all else "🛰 Ожидание данных..."
        tape_content = tape_str + "          " + tape_str

        # ── шапка ──
        session_pnl = db.session.query(db.func.coalesce(db.func.sum(Trade.pnl), 0.0)) \
            .filter(Trade.user_id == uid, Trade.pnl.isnot(None)).scalar()
        p_col = C["green"] if session_pnl >= 0 else C["red"]
        p_style = {"color": p_col, "fontWeight": "900", "fontSize": "20px"}
        balance = engine.last_balance.get(uid)
        balance_txt = f"${balance:,.2f}" if balance is not None else "—"
        trades_count = Trade.query.filter_by(user_id=uid).count()

        return (
            user_line,
            f"{session_pnl:+,.2f}$", p_style,
            balance_txt,
            f"{open_count} / {cfg.max_open_positions}",
            str(trades_count),
            tape_content, cards, fig, lp_text, sig_el, rows,
            sound_buy, sound_sell,
        )

    return app
