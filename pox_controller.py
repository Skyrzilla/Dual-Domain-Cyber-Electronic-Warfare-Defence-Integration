#!/usr/bin/env python3
import threading
import socket
import json
import time
import numpy as np
import requests
from collections import deque
from dash import Dash, html, dcc, Input, Output, State, ALL, callback_context, dash_table, no_update
import plotly.graph_objs as go
import platform, subprocess, flask

# ============================================================
# IP Configuration: Detect host IP automatically (Windows/Linux)
# ============================================================
def get_host_ip():
    """Detect local IPv4 address using ipconfig (Windows) or hostname -I (Linux)."""
    try:
        if platform.system().lower().startswith("win"):
            result = subprocess.check_output("ipconfig", shell=True).decode(errors="ignore")
            for line in result.splitlines():
                if "IPv4 Address" in line or "IPv4-Adresse" in line:
                    ip = line.split(":")[-1].strip()
                    if not ip.startswith("169.254."):  # skip link-local
                        return ip
        else:
            result = subprocess.check_output("hostname -I", shell=True).decode().split()
            for ip in result:
                if ip and not ip.startswith("169.254."):
                    return ip
    except Exception:
        pass
    return "127.0.0.1"

HOST_IP = get_host_ip()
HOST_ALLOWED = HOST_IP  # Only this machine can change modes
print(f"[Dashboard] Host IP detected: {HOST_IP}")

# ============================================================
# CONFIGURATION
# ============================================================
HOST_DASH = "127.0.0.1"
LISTEN_PORT = 7000
SIM_CTRL_HOST = "127.0.0.1"
SIM_CTRL_PORT = 5555
POX_URL = "http://127.0.0.1:8088/status"
DASH_PORT = 8060

# ============================================================
# THEME COLORS
# ============================================================
BACKGROUND = "#000000"
PANEL_BG = "#0d0d0d"
ACCENT = "#00BFFF"
ACCENT_RGBA = "rgba(0, 191, 255, 0.2)"
ACCENT_DIM = "rgba(0, 191, 255, 0.5)"
TEXT = "#e0e0e0"
TEXT_HEADER = "#ffffff"
LOCK_COLOR = "#39FF14"
LOCK_COLOR_DARK = "#33cc11"

# ============================================================
# STATE
# ============================================================
LATEST_POWER = deque(maxlen=10)
WATERFALL = deque(maxlen=300)
EVENTS = deque(maxlen=300)
SELECTED_MODE = {"mode": "NORMAL", "locked": False}
LOCKED_MODE_TYPE = None
ANALYZER_STATUS = {"connected": False}
POX_STATUS = {"uptime": 0, "controller": "POX", "last_mode": None, "flow_rules": []}
_last_analyzer_state = {"connected": False}

# ============================================================
# Gaussian Smoothing
# ============================================================
def gaussian_kernel1d(sigma, radius=None):
    if sigma <= 0: return np.array([1.0], dtype=float)
    if radius is None: radius = int(max(1, np.ceil(3 * sigma)))
    x = np.arange(-radius, radius + 1)
    w = np.exp(-(x**2) / (2 * sigma * sigma))
    w /= w.sum()
    return w

def separable_gaussian_2d(img, sigma_x=1.0, sigma_y=1.0):
    if img.size == 0: return img
    kx = gaussian_kernel1d(sigma_x); ky = gaussian_kernel1d(sigma_y)
    pad_x = len(kx)//2; pad_y = len(ky)//2
    img_p = np.pad(img, ((pad_y, pad_y), (pad_x, pad_x)), mode="edge")
    tmp = np.apply_along_axis(lambda m: np.convolve(m, kx, mode="valid"), axis=1, arr=img_p)
    out = np.apply_along_axis(lambda m: np.convolve(m, ky, mode="valid"), axis=0, arr=tmp)
    return out

# ============================================================
# Analyzer Receiver (no changes)
# ============================================================
def analyzer_receiver():
    global LOCKED_MODE_TYPE
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for _ in range(10):
        try:
            srv.bind((HOST_DASH, LISTEN_PORT))
            break
        except OSError as e:
            print(f"[Dashboard] bind failed ({e}), retrying..."); time.sleep(0.5)
    else:
        print("[Dashboard] Could not bind analyzer socket."); return
    srv.listen(2)
    print(f"[Dashboard] Listening for analyzer data on {HOST_DASH}:{LISTEN_PORT}")
    while True:
        try:
            conn, addr = srv.accept()
            ANALYZER_STATUS["connected"] = True
            if not _last_analyzer_state["connected"]:
                print(f"[Dashboard] Analyzer connected: {addr}")
                _last_analyzer_state["connected"] = True
                if SELECTED_MODE["locked"]:
                    print(f"[Dashboard] Re-locking simulator to mode: {SELECTED_MODE['mode']}")
                    send_mode_to_simulator(SELECTED_MODE['mode'])
            f = conn.makefile("r")
            for line in f:
                if not line: break
                try: pkt = json.loads(line)
                except Exception: continue
                power = np.array(pkt.get("power", []), dtype=float)
                if power.size > 0:
                    LATEST_POWER.append(power); WATERFALL.append(power)
                EVENTS.appendleft({
                    "timestamp": pkt.get("timestamp", time.strftime("%H:%M:%S")),
                    "type": pkt.get("type", "UNKNOWN"),
                    "mean": round(pkt.get("mean", 0), 3),
                    "peak": round(pkt.get("peak", 0), 3),
                    "sim_mode": pkt.get("sim_mode", SELECTED_MODE["mode"]),
                })
        except Exception as e:
            if _last_analyzer_state["connected"]:
                print("[Dashboard] Analyzer disconnected:", e)
            ANALYZER_STATUS["connected"] = False
            _last_analyzer_state["connected"] = False
            time.sleep(1)

# ============================================================
# HELPERS
# ============================================================
def send_mode_to_simulator(mode_name):
    SELECTED_MODE["mode"] = mode_name; SELECTED_MODE["locked"] = True
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(1)
        s.connect((SIM_CTRL_HOST, SIM_CTRL_PORT))
        msg = {"action": "LOCK", "mode": mode_name, "source": "dashboard"}
        s.sendall(json.dumps(msg).encode())
        try:
            resp = s.recv(2048)
            if resp: print(f"[Dashboard] Simulator ACK: {json.loads(resp.decode())}")
        except Exception: pass
        s.close()
        print(f"[Dashboard] Lock command sent to simulator: {mode_name}")
    except Exception as e:
        print(f"[Dashboard] Failed to send lock to simulator:", e)

def poll_pox_status():
    global POX_STATUS
    while True:
        try:
            r = requests.get(POX_URL, timeout=1)
            if r.status_code == 200: POX_STATUS.update(r.json())
        except Exception:
            pass
        time.sleep(0.3)

# ============================================================
# Dash Setup
# ============================================================
external_stylesheets = ['https://fonts.googleapis.com/css2?family=Roboto+Mono:wght@400;700&display=swap']
app = Dash(__name__, external_stylesheets=external_stylesheets, suppress_callback_exceptions=True)
app.title = "EW Web Dashboard"

# ============================================================
# DASH CONFIG
# ============================================================
external_stylesheets = [
    'https://fonts.googleapis.com/css2?family=Roboto+Mono:wght@400;700&display=swap'
]
app = Dash(__name__, external_stylesheets=external_stylesheets, suppress_callback_exceptions=True)
app.title = "EW Web Dashboard"

# ============================================================
# 3D BUTTON STYLE
# ============================================================
def button_style(locked=False):
    style = {
        "fontFamily": "'Roboto Mono', monospace",
        "fontSize": "14px",
        "fontWeight": "700",
        "color": TEXT_HEADER,
        "background": "#1a1a1a",
        "border": "1px solid #000",
        "borderBottom": "4px solid #000",
        "borderRadius": "8px",
        "padding": "10px 18px",
        "margin": "5px",
        "transition": "all 0.1s ease-out",
        "boxShadow": "0px 4px 0px #000, 0px 5px 10px rgba(0,0,0,0.5)",
        "cursor": "pointer"
    }
    if locked:
        style.update({
            "background": f"linear-gradient(45deg, {LOCK_COLOR}, {LOCK_COLOR_DARK})",
            "color": "#000000",
            "boxShadow": f"0px 0px 20px {LOCK_COLOR}, 0px 0px 10px {LOCK_COLOR} inset",
            "borderBottom": f"4px solid {LOCK_COLOR}",
            "transform": "translateY(2px)",
        })
    return style

modes = [
    ("NORMAL", "Normal"), ("JAMMING", "Jamming"), ("SPOOFING", "Spoofing"),
    ("INTERCEPT", "Intercept"), ("MITM", "Man-in-the-Middle"),
]

# ============================================================
# PANEL & TABLE STYLES
# ============================================================
panel_style = {
    "background": PANEL_BG,
    "borderRadius": "10px",
    "padding": "10px",
    "border": "1px solid #111",
    "boxShadow": f"0 0 15px {ACCENT_DIM}, 0 0 5px {ACCENT_DIM} inset"
}

header_panel_style = {
    **panel_style,
    "display": "flex",
    "justifyContent": "space-between",
    "alignItems": "center",
    "padding": "10px 15px",
    "marginBottom": "10px"
}

# ============================================================
# 3D DIAL STYLE (FIXED: "Extruded" / Popping-Out)
# ============================================================
dial_panel_style = {
    "background": "#0a0a0a", # Darker than panel_bg for contrast
    "borderRadius": "20px",
    "padding": "15px 10px 5px 10px",
    # This "extruded" shadow makes them pop out
    "boxShadow": f"5px 5px 10px #050505, -5px -5px 10px #1a1a1a, 0 0 10px {ACCENT_DIM}",
    "border": f"1px solid {ACCENT_DIM}", # Accent edge
    "textAlign": "center",
    "width": "200px", # Give sliders + buttons some room
}

# --- NEW: Style for the +/- buttons ---
inc_dec_button_style = {
    'color': ACCENT,
    'background': '#222',
    'border': f'1px solid {ACCENT_DIM}',
    'borderRadius': '5px',
    'width': '25px',
    'height': '25px',
    'fontSize': '16px',
    'fontWeight': 'bold',
    'cursor': 'pointer',
    'padding': '0',
    'lineHeight': '25px',
    'textAlign': 'center'
}


table_cell_style = {
    "fontFamily": "'Roboto Mono', monospace",
    "fontSize": "12px",
    "textAlign": "left",
    "color": TEXT,
    "backgroundColor": "transparent",
    "border": "none",
    "padding": "8px"
}

table_header_style = {
    "fontFamily": "'Roboto Mono', monospace",
    "fontSize": "13px",
    "fontWeight": "700",
    "textAlign": "left",
    "color": ACCENT, # Use Accent for header text
    "backgroundColor": "#1a1a1a",
    "borderBottom": f"2px solid {ACCENT}",
    "padding": "10px 8px"
}

table_data_conditional = [
    {'if': {'row_index': 'odd'}, 'backgroundColor': '#111111'}
]

# ============================================================
# LAYOUT (*** NEW 3-COLUMN LAYOUT ***)
# ============================================================
app.layout = html.Div(
    style={"background": BACKGROUND, "color": TEXT, "padding": "10px",
           "display": "flex", "flexDirection": "column", "fontFamily": "'Roboto Mono', monospace",
           "minHeight": "100vh"},
    children=[
        html.H2("Electronic Warfare — Live Web Dashboard (SDN Control)",
                style={"textAlign": "center", "color": ACCENT, "textShadow": f"0 0 15px {ACCENT}",
                       "fontWeight": "700", "letterSpacing": "2px"}),

        # --- NEW 3-COLUMN MAIN LAYOUT ---
        html.Div(
            style={"display": "flex", "flexDirection": "row", "gap": "10px", "marginTop": "10px", "flex": 1},
            children=[

                # --- LEFT COLUMN (Controls & Status) ---
                html.Div(
                    style={**panel_style, "flex": "0 0 250px", "display": "flex", 
                           "flexDirection": "column", "gap": "20px", "padding": "15px"},
                    children=[
                        # --- Modes ---
                        html.Div([
                            html.Div("Modes:", style={"fontWeight": "700", "color": TEXT_HEADER, "marginBottom": "6px", "textAlign": "center"}),
                            html.Div(
                                [html.Button(full, id={"type": "mode-btn", "index": code}, n_clicks=0, 
                                              # Make buttons full-width for vertical stacking
                                              style={'width': '95%', 'margin': '4px auto'}) 
                                 for code, full in modes],
                                style={"display": "flex", "flexDirection": "column", "alignItems": "center"} # Stack buttons
                            ),
                        ]),
                        
                        # --- Dials (Centering them in the column) ---
                        html.Div(children=[
                            html.Div("Center Frequency (MHz)", style={"textAlign": "center", "fontSize": "12px", "color": TEXT, "marginBottom": "5px"}),
                            html.Div(style={'display': 'flex', 'alignItems': 'center', 'justifyContent': 'space-around'}, children=[
                                html.Button('-', id='freq-down', style=inc_dec_button_style, n_clicks=0),
                                html.Div(style={'width': '110px'}, children=[
                                    dcc.Slider(id="freq_knob", min=900, max=2000, value=915, step=1,
                                               marks=None, tooltip={"placement": "bottom", "always_visible": False},
                                               className="slider_style")
                                ]),
                                html.Button('+', id='freq-up', style=inc_dec_button_style, n_clicks=0),
                            ]),
                            html.Div(id="freq_value", style={"textAlign": "center", "color": ACCENT, "fontWeight": "700", "marginTop": "10px"}),
                        ], style={**dial_panel_style, 'margin': '0 auto'}), # Center the dial panel
                        
                        html.Div(children=[
                            html.Div("Power Floor (dB)", style={"textAlign": "center", "fontSize": "12px", "color": TEXT, "marginBottom": "5px"}),
                            html.Div(style={'display': 'flex', 'alignItems': 'center', 'justifyContent': 'space-around'}, children=[
                                html.Button('-', id='db-down', style=inc_dec_button_style, n_clicks=0),
                                html.Div(style={'width': '110px'}, children=[
                                    dcc.Slider(id="db_knob", min=-120, max=0, value=-80, step=1,
                                               marks=None, tooltip={"placement": "bottom", "always_visible": False},
                                               className="slider_style")
                                ]),
                                html.Button('+', id='db-up', style=inc_dec_button_style, n_clicks=0),
                            ]),
                            html.Div(id="db_value", style={"textAlign": "center", "color": ACCENT, "fontWeight": "700", "marginTop": "10px"}),
                        ], style={**dial_panel_style, 'margin': '0 auto'}), # Center the dial panel
                        
                        html.Div(children=[
                            html.Div("Span (MHz)", style={"textAlign": "center", "fontSize": "12px", "color": TEXT, "marginBottom": "5px"}),
                            html.Div(style={'display': 'flex', 'alignItems': 'center', 'justifyContent': 'space-around'}, children=[
                                html.Button('-', id='span-down', style=inc_dec_button_style, n_clicks=0),
                                html.Div(style={'width': '110px'}, children=[
                                    dcc.Slider(id="span_knob", min=10, max=1000, value=100, step=10,
                                               marks=None, tooltip={"placement": "bottom", "always_visible": False},
                                               className="slider_style")
                                ]),
                                html.Button('+', id='span-up', style=inc_dec_button_style, n_clicks=0),
                            ]),
                            html.Div(id="span_value", style={"textAlign": "center", "color": ACCENT, "fontWeight": "7A00", "marginTop": "10px"}),
                        ], style={**dial_panel_style, 'margin': '0 auto'}), # Center the dial panel

                        # --- Status (Pushed to the bottom of the column) ---
                        html.Div(id="status_box", children=[
                            html.Div("Mode: NORMAL", id="mode_text"),
                            html.Div("Analyzer: waiting...", id="analyzer_text"),
                            html.Div("POX Controller: waiting...", id="pox_text"),
                        ], style={"textAlign": "left", "fontSize": "14px", "color": TEXT,
                                  "lineHeight": "1.8em", "marginTop": "auto"}),
                    ]
                ),

                # --- CENTER COLUMN (Plots) ---
                html.Div(
                    style={**panel_style, "flex": 3, "display": "flex", "flexDirection": "column"}, # Use flex: 3 for main area
                    children=[
                        dcc.Graph(id="spectrum", config={"scrollZoom": True}, style={"height": "45vh"}),
                        dcc.Graph(id="waterfall", config={"scrollZoom": True}, style={"height": "45vh"}),
                    ]
                ),

                # --- RIGHT COLUMN (Logs) ---
                html.Div(
                    style={**panel_style, "flex": 1.5, "display": "flex", "flexDirection": "column"}, # Use flex: 1.5
                    children=[
                        html.Div("Event Log", style={"fontWeight": "700", "color": TEXT_HEADER, "marginBottom": "6px"}),
                        dash_table.DataTable(
                            id="events_table",
                            columns=[
                                {"name": "Time", "id": "timestamp"},
                                {"name": "Type", "id": "type"},
                                {"name": "Mean", "id": "mean"},
                                {"name": "Peak", "id": "peak"},
                                {"name": "SimMode", "id": "sim_mode"},
                            ],
                            data=[], page_size=12, # Increased page size
                            # Height increased to fill the space left by the removed POX table
                            style_table={"overflowY": "auto", "height": "90vh"}, 
                            style_cell=table_cell_style,
                            style_header=table_header_style,
                            style_data_conditional=table_data_conditional
                        ),
                        # REMOVED: html.Hr, POX SDN Flow Rules Header, and POX Flow Table
                    ]
                ),
            ]
        ), # End 3-column layout

        dcc.Interval(id="ui_interval", interval=300, n_intervals=0)
    ],
)

# ============================================================
# CALLBACKS
# ============================================================

# --- NEW: Callbacks for + / - buttons ---

@app.callback(
    Output('freq_knob', 'value'),
    Input('freq-down', 'n_clicks'),
    Input('freq-up', 'n_clicks'),
    State('freq_knob', 'value'),
    State('freq_knob', 'min'),
    State('freq_knob', 'max'),
    prevent_initial_call=True
)
def update_freq_from_buttons(_, __, current_val, min_val, max_val):
    triggered_id = callback_context.triggered_id
    step = 5  # Set increment step for frequency
    
    if triggered_id == 'freq-down':
        new_val = max(min_val, current_val - step)
        return new_val
    elif triggered_id == 'freq-up':
        new_val = min(max_val, current_val + step)
        return new_val
    return no_update

@app.callback(
    Output('db_knob', 'value'),
    Input('db-down', 'n_clicks'),
    Input('db-up', 'n_clicks'),
    State('db_knob', 'value'),
    State('db_knob', 'min'),
    State('db_knob', 'max'),
    prevent_initial_call=True
)
def update_db_from_buttons(_, __, current_val, min_val, max_val):
    triggered_id = callback_context.triggered_id
    step = 1  # Set increment step for dB
    
    if triggered_id == 'db-down':
        new_val = max(min_val, current_val - step)
        return new_val
    elif triggered_id == 'db-up':
        new_val = min(max_val, current_val + step)
        return new_val
    return no_update

@app.callback(
    Output('span_knob', 'value'),
    Input('span-down', 'n_clicks'),
    Input('span-up', 'n_clicks'),
    State('span_knob', 'value'),
    State('span_knob', 'min'),
    State('span_knob', 'max'),
    prevent_initial_call=True
)
def update_span_from_buttons(_, __, current_val, min_val, max_val):
    triggered_id = callback_context.triggered_id
    step = 10  # Set increment step for span
    
    if triggered_id == 'span-down':
        new_val = max(min_val, current_val - step)
        return new_val
    elif triggered_id == 'span-up':
        new_val = min(max_val, current_val + step)
        return new_val
    return no_update

# --- Existing callback for slider display (no changes needed) ---
@app.callback(
    Output("freq_value", "children"),
    Output("db_value", "children"),
    Output("span_value", "children"),
    Input("freq_knob", "value"),
    Input("db_knob", "value"),
    Input("span_knob", "value"),
)
def knob_display(f, d, s):
    return f"{f:.2f} MHz", f"{d:.1f} dB", f"{s:.0f} MHz"

@app.callback(
    Output("mode_text", "children"),
    Output({"type": "mode-btn", "index": ALL}, "style"),
    Input({"type": "mode-btn", "index": ALL}, "n_clicks"),
    State({"type": "mode-btn", "index": ALL}, "id"),
)
def mode_lock(n_clicks_list, ids):
    global LOCKED_MODE_TYPE
    ctx = callback_context
    triggered_id_str = ctx.triggered[0]["prop_id"].split(".")[0]
    
    if not ctx.triggered or not triggered_id_str:
        mode_name = SELECTED_MODE["mode"]
    else:
        try:
            d = json.loads(triggered_id_str)
            mode_name = d["index"]
        except Exception:
            mode_name = "NORMAL"
    
    if ctx.triggered:
       client_ip = flask.request.remote_addr
       if client_ip == HOST_ALLOWED or client_ip == "127.0.0.1":
          LOCKED_MODE_TYPE = mode_name
          threading.Thread(target=send_mode_to_simulator, args=(mode_name,), daemon=True).start()
       else:
           print(f"[Dashboard] Unauthorized mode change attempt from {client_ip}")

    styles = []
    for button_id in ids:
        if button_id["index"] == SELECTED_MODE["mode"] and SELECTED_MODE["locked"]:
            styles.append(button_style(locked=True))
        else:
            styles.append(button_style(locked=False))
            
    mode_text = f"Mode: {SELECTED_MODE['mode']} (Locked)"
    return mode_text, styles


# ============================================================
# VISUALIZATION UPDATE (patched for remote clients)
# ============================================================
@app.callback(
    Output("spectrum", "figure"),
    Output("waterfall", "figure"),
    Output("events_table", "data"),
    # REMOVED: Output("pox_flow_table", "data"),
    Output("analyzer_text", "children"),
    Output("pox_text", "children"),
    Input("ui_interval", "n_intervals"),
    State("freq_knob", "value"),
    State("db_knob", "value"),
    State("span_knob", "value"),
)
def update_view(_, center_freq, db_floor, span):
    # >>> PATCH START: allow clients to see live graphs
    try:
        client_ip = flask.request.remote_addr
        if client_ip and client_ip != HOST_IP:
            # remote clients are allowed read-only updates
            pass
    except Exception:
        pass
    # >>> PATCH END

    if not LATEST_POWER:
        empty = go.Figure().update_layout(paper_bgcolor=BACKGROUND, plot_bgcolor=BACKGROUND,
                                          title="Spectrum (Waiting for data...)", font_color=TEXT)
        wf_empty = go.Figure().update_layout(paper_bgcolor=BACKGROUND, plot_bgcolor=BACKGROUND,
                                             title="Waterfall (Waiting for data...)", font_color=TEXT)
        analyzer_text = "Analyzer: ❌ Disconnected"
        pox_text = f"POX Controller: {POX_STATUS.get('controller','?')} | Uptime: {POX_STATUS.get('uptime',0)}s"
        # REMOVED: POX_STATUS.get('flow_rules',[]) from return
        return empty, wf_empty, [], analyzer_text, pox_text

    avg_frames = min(len(LATEST_POWER), 6)
    p = np.mean(np.array(LATEST_POWER)[-avg_frames:], axis=0)
    p = np.abs(p) / (np.max(p) + 1e-12)
    p_db = 20*np.log10(np.maximum(p,1e-12))

    if span is None or span <= 0: span = 100
    freqs = np.linspace(center_freq - span/2, center_freq + span/2, len(p_db))

    fig_s = go.Figure()
    fig_s.add_trace(go.Scatter(x=freqs, y=p_db, mode="lines",
                               line=dict(color=ACCENT, width=1.6),
                               fill="tozeroy", fillcolor=ACCENT_RGBA))
    fig_s.update_layout(title=f"Spectrum (Locked Mode: {SELECTED_MODE['mode']})",
                        paper_bgcolor=BACKGROUND, plot_bgcolor=BACKGROUND,
                        font_color=TEXT)

    arr = np.array(list(WATERFALL))
    if arr.size == 0:
        wf_empty = go.Figure().update_layout(paper_bgcolor=BACKGROUND, plot_bgcolor=BACKGROUND,
                                             title="Waterfall (Waiting for data...)", font_color=TEXT)
        # REMOVED: POX_STATUS.get('flow_rules', []) from return
        return fig_s, wf_empty, list(EVENTS)[:60], \
               "Analyzer: ✅ Connected", f"POX Controller: {POX_STATUS.get('controller','?')}"

    arr = np.abs(arr)
    arr /= (np.max(arr, axis=1, keepdims=True) + 1e-12)
    arr_db = 20*np.log10(np.maximum(arr,1e-12))
    arr_sm = separable_gaussian_2d(arr_db, 0.8, 1.0)
    vmin, vmax = db_floor, 0
    arr_clip = np.clip(arr_sm, vmin, vmax)
    arr_norm = (arr_clip - vmin) / (vmax - vmin + 1e-12)
    arr_gamma = np.power(arr_norm, 0.9)

    fig_w = go.Figure(go.Heatmap(z=arr_gamma, colorscale=[
        [0.0,"#000022"],[0.3,"#0044aa"],[0.5,"#39FF14"],
        [0.65,"#ffff00"],[0.9,"#ff2200"]
    ], showscale=False))
    fig_w.update_layout(title="Waterfall (Linked to Knobs)",
                        paper_bgcolor=BACKGROUND, plot_bgcolor=BACKGROUND, font_color=TEXT)
    fig_w.update_yaxes(autorange="reversed")

    analyzer_text = "Analyzer: ✅ Connected" if ANALYZER_STATUS["connected"] else "Analyzer: ❌ Disconnected"
    pox_text = f"POX Controller: {POX_STATUS.get('controller','?')} | Uptime: {POX_STATUS.get('uptime',0)}s"
    # REMOVED: POX_STATUS.get('flow_rules', []) from return
    return fig_s, fig_w, list(EVENTS)[:60], analyzer_text, pox_text

# ============================================================
# THREADS
# ============================================================
threading.Thread(target=analyzer_receiver, daemon=True).start()
threading.Thread(target=poll_pox_status, daemon=True).start()

# ============================================================
# RUN SERVER
# ============================================================
if __name__ == "__main__":
    print(f"[INFO] EW Dashboard running — open http://{HOST_IP}:{DASH_PORT}")
    print("[INFO] Only this host can change modes; clients are view-only.")
    app.run(host="0.0.0.0", port=DASH_PORT, debug=False)