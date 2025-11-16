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
import logging
import os

# ============================================================
# LOGGING SETUP (CRITICAL for IDS)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, 'access.log')

# Clear the log on startup
try:
    with open(LOG_FILE, 'w'): 
        pass
except IOError:
    print(f"Could not clear log file: {LOG_FILE}")

# Set up the file handler
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setLevel(logging.INFO)

# Set up the custom formatter to capture client IP (remote_addr)
formatter = logging.Formatter('%(asctime)s - IP: %(remote_addr)s - %(message)s')
file_handler.setFormatter(formatter)

# Configure the default Flask logger used by Dash
log = logging.getLogger('werkzeug')
log.setLevel(logging.INFO)
# Remove default handlers to prevent duplicate logging
log.handlers = []
log.addHandler(file_handler)
# ============================================================


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
DASH_PORT = 8060 # NOTE: This should match the port in run1.py

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
# Analyzer Receiver
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

# 3D DIAL STYLE
dial_panel_style = {
    "background": "#0a0a0a",
    "borderRadius": "20px",
    "padding": "15px 10px 5px 10px",
    "boxShadow": f"5px 5px 10px #050505, -5px -5px 10px #1a1a1a, 0 0 10px {ACCENT_DIM}",
    "border": f"1px solid {ACCENT_DIM}",
    "textAlign": "center",
    "width": "200px",
}
inc_dec_button_style = {
    'color': ACCENT, 'background': '#222', 'border': f'1px solid {ACCENT_DIM}',
    'borderRadius': '5px', 'width': '25px', 'height': '25px', 'fontSize': '16px',
    'fontWeight': 'bold', 'cursor': 'pointer', 'padding': '0', 'lineHeight': '25px',
    'textAlign': 'center'
}
table_cell_style = {
    "fontFamily": "'Roboto Mono', monospace", "fontSize": "12px", "textAlign": "left",
    "color": TEXT, "backgroundColor": "transparent", "border": "none", "padding": "8px"
}
table_header_style = {
    "fontFamily": "'Roboto Mono', monospace", "fontSize": "13px", "fontWeight": "700",
    "textAlign": "left", "color": ACCENT, "backgroundColor": "#1a1a1a",
    "borderBottom": f"2px solid {ACCENT}", "padding": "10px 8px"
}
table_data_conditional = [ {'if': {'row_index': 'odd'}, 'backgroundColor': '#111111'} ]

# ============================================================
# LAYOUT
# ============================================================
app.layout = html.Div(
    style={"background": BACKGROUND, "color": TEXT, "padding": "10px", "display": "flex", "flexDirection": "column", "fontFamily": "'Roboto Mono', monospace", "minHeight": "100vh"},
    children=[
        html.H2("Electronic Warfare — Live Web Dashboard (SDN Control)", style={"textAlign": "center", "color": ACCENT, "textShadow": f"0 0 15px {ACCENT}", "fontWeight": "700", "letterSpacing": "2px"}),
        dcc.Interval(id='interval-component', interval=2*1000, n_intervals=0), # 2 second interval
        html.Div(
            style={"display": "flex", "flexDirection": "row", "gap": "10px", "marginTop": "10px", "flex": 1},
            children=[
                # --- LEFT COLUMN (Controls & Status) ---
                html.Div(
                    style={**panel_style, "flex": "0 0 250px", "display": "flex", "flexDirection": "column", "gap": "20px", "padding": "15px"},
                    children=[
                        # --- Modes ---
                        html.Div(
                            [
                                html.Div("Modes:", style={"color": ACCENT, "marginBottom": "5px", "fontSize": "16px", "fontWeight": "700"}),
                                html.Div(id='mode-buttons', children=[
                                    html.Button(
                                        name, id={'type': 'mode-button', 'index': tag}, n_clicks=0,
                                        style=button_style(locked=tag==SELECTED_MODE['mode'] and SELECTED_MODE['locked'])
                                    ) for tag, name in modes
                                ]),
                                html.Div(id='mode-status', style={"marginTop": "10px", "fontSize": "14px", "color": TEXT}),
                            ],
                            style={**dial_panel_style, "width": "auto"}
                        ),
                        # --- Power Range ---
                        html.Div(
                            [
                                html.Div("Power Range (dBm):", style={"color": ACCENT, "marginBottom": "5px", "fontSize": "16px", "fontWeight": "700"}),
                                dcc.RangeSlider(
                                    id='power_range_slider', min=-100, max=0, step=5, value=[-80, -20],
                                    tooltip={"placement": "bottom", "always_visible": True},
                                    marks={i: {'label': str(i), 'style': {'color': TEXT}} for i in range(-100, 1, 20)},
                                    className="dark-slider"
                                ),
                            ],
                            style={**dial_panel_style, "width": "auto"}
                        ),
                        # --- Noise Sigma ---
                        html.Div(
                            [
                                html.Div("Smoothing Sigma:", style={"color": ACCENT, "marginBottom": "5px", "fontSize": "16px", "fontWeight": "700"}),
                                dcc.Slider(
                                    id='noise_sigma_slider', min=0.1, max=5.0, step=0.1, value=1.0,
                                    tooltip={"placement": "bottom", "always_visible": True, "template": "{value:.1f}"},
                                    marks={0.1: '0.1', 1.0: '1.0', 2.0: '2.0', 3.0: '3.0', 4.0: '4.0', 5.0: '5.0'},
                                    className="dark-slider"
                                ),
                            ],
                            style={**dial_panel_style, "width": "auto"}
                        ),
                        # --- Status Text ---
                        html.Div(id='analyzer_text', style={"color": TEXT, "fontWeight": "700", "padding": "5px"}),
                        html.Div(id='pox_text', style={"color": TEXT, "fontWeight": "700", "padding": "5px"}),

                        # --- POX Flow Table (SDN Rules) ---
                        html.Div(
                            [
                                html.H4("SDN Flow Rules", style={"color": ACCENT, "marginTop": "0"}),
                                dash_table.DataTable(
                                    id='pox_flow_table',
                                    columns=[
                                        {"name": "Rule", "id": "rule"},
                                        {"name": "Status", "id": "status"},
                                    ],
                                    data=[], # Data populated by callback
                                    style_header=table_header_style,
                                    style_cell=table_cell_style,
                                    style_data_conditional=table_data_conditional,
                                    style_table={'overflowX': 'auto', 'border': '1px solid #333'},
                                ),
                            ],
                            style={**panel_style, "marginTop": "auto", "padding": "10px", "flexGrow": 1}
                        )
                    ]
                ),

                # --- CENTER COLUMN (Main Graphs) ---
                html.Div(
                    style={"flex": 1, "display": "flex", "flexDirection": "column", "gap": "10px"},
                    children=[
                        # Power Spectrum
                        dcc.Graph(id='spectrum', style={**panel_style, "flex": "0 0 45%"}),
                        # Waterfall Plot
                        dcc.Graph(id='waterfall', style={**panel_style, "flex": "1 1 auto"}),
                    ]
                ),

                # --- RIGHT COLUMN (Events Log) ---
                html.Div(
                    style={**panel_style, "flex": "0 0 350px", "display": "flex", "flexDirection": "column"},
                    children=[
                        html.H4("EW Event Log", style={"color": ACCENT, "marginBottom": "5px", "marginTop": "0"}),
                        dash_table.DataTable(
                            id='events_table',
                            columns=[
                                {"name": "Time", "id": "timestamp"},
                                {"name": "Type", "id": "type"},
                                {"name": "Mean Power", "id": "mean"},
                                {"name": "Sim Mode", "id": "sim_mode"},
                            ],
                            data=list(EVENTS)[:60],
                            style_header=table_header_style,
                            style_cell=table_cell_style,
                            style_data_conditional=table_data_conditional,
                            style_table={'overflowY': 'auto', 'height': '900px', 'border': '1px solid #333'},
                        ),
                    ]
                ),
            ]
        ),
    ]
)

# ============================================================
# CALLBACK: Mode Button Handler
# ============================================================
@app.callback(
    Output('mode-status', 'children'),
    Output('mode-buttons', 'children'),
    Input({'type': 'mode-button', 'index': ALL}, 'n_clicks'),
    State({'type': 'mode-button', 'index': ALL}, 'id'),
    State('mode-buttons', 'children'),
    State('mode-status', 'children'),
)
def handle_mode_change(n_clicks, button_ids, button_children, current_status):
    ctx = callback_context
    if not ctx.triggered or all(c == 0 for c in n_clicks):
        # Initial load: Re-render buttons based on current state
        new_buttons = [
            html.Button(
                name, id={'type': 'mode-button', 'index': tag}, n_clicks=n_clicks[i] if n_clicks else 0,
                style=button_style(locked=tag==SELECTED_MODE['mode'] and SELECTED_MODE['locked'])
            ) for i, (tag, name) in enumerate(modes)
        ]
        status_text = f"Current Mode: {SELECTED_MODE['mode']} ({'Locked' if SELECTED_MODE['locked'] else 'Unlocked'})"
        return status_text, new_buttons

    button_id = ctx.triggered[0]['prop_id'].split('.')[0]
    triggered_id = json.loads(button_id)
    new_mode = triggered_id['index']

    if new_mode == SELECTED_MODE['mode'] and SELECTED_MODE['locked']:
        # Unlock button was clicked
        SELECTED_MODE['locked'] = False
        status_text = f"Current Mode: {new_mode} (Unlocked by Dashboard)"
        print(f"[Dashboard] Unlocked simulator mode.")
    else:
        # New mode selected or same mode selected when unlocked
        send_mode_to_simulator(new_mode)
        status_text = f"Mode Locked: {new_mode} (Locked by Dashboard)"

    # Re-render buttons
    new_buttons = [
        html.Button(
            name, id={'type': 'mode-button', 'index': tag}, n_clicks=n_clicks[i],
            style=button_style(locked=tag==SELECTED_MODE['mode'] and SELECTED_MODE['locked'])
        ) for i, (tag, name) in enumerate(modes)
    ]

    return status_text, new_buttons

# ============================================================
# CALLBACK: Update Graphs, Tables, and Status (The main loop)
# *** FIX APPLIED HERE: Synchronized 6 Outputs ***
# ============================================================
@app.callback(
    # Outputs MUST match the components in the error message EXACTLY (6 outputs)
    [
        Output('spectrum', 'figure'),
        Output('waterfall', 'figure'),
        Output('events_table', 'data'),
        Output('analyzer_text', 'children'),
        Output('pox_text', 'children'),
    ],
    [
        Input('interval-component', 'n_intervals')
    ],
    [
        State('power_range_slider', 'value'), # Needed for graph axis limits
        State('noise_sigma_slider', 'value'), # Needed for smoothing
    ]
)
def update_graph_data(n_intervals, power_range, noise_sigma):
    # This function is the primary data processor, run every 2 seconds by the interval.
    
    if not LATEST_POWER:
        # If no data has arrived yet, return no_update for all 6 outputs
        return no_update, no_update, no_update, no_update, no_update, no_update

    # 1. Get State Variables
    vmin, vmax = power_range
    sigma = noise_sigma

    # 2. Process Power Spectrum (Line Graph)
    avg_power = np.mean(LATEST_POWER, axis=0) if LATEST_POWER else np.zeros(100)
    fig_s = go.Figure(go.Scatter(y=avg_power, mode='lines', line=dict(color=ACCENT, width=3)))

    fig_s.update_layout(title="Average Power Spectrum",
                        yaxis_range=[vmin, vmax],
                        paper_bgcolor=BACKGROUND, plot_bgcolor=PANEL_BG, font_color=TEXT,
                        margin=dict(l=20, r=20, t=40, b=20))
    fig_s.update_xaxes(showgrid=False, zeroline=False)

    # 3. Process Waterfall (Heatmap)
    arr_raw = np.array(list(WATERFALL), dtype=float)
    # Apply Gaussian smoothing
    arr_sm = separable_gaussian_2d(arr_raw, sigma_x=sigma, sigma_y=1)

    # Apply min/max clipping and gamma correction for better visual contrast
    arr_clip = np.clip(arr_sm, vmin, vmax)
    arr_norm = (arr_clip - vmin) / (vmax - vmin + 1e-12) # Normalize to 0-1
    arr_gamma = np.power(arr_norm, 0.9)

    fig_w = go.Figure(go.Heatmap(z=arr_gamma, colorscale=[
        [0.0,"#000022"],[0.3,"#0044aa"],[0.5,"#39FF14"],
        [0.65,"#ffff00"],[0.9,"#ff2200"]
    ], showscale=False))
    fig_w.update_layout(title="Waterfall (Linked to Knobs)",
                        paper_bgcolor=BACKGROUND, plot_bgcolor=BACKGROUND, font_color=TEXT)
    fig_w.update_yaxes(autorange="reversed")

    # 4. Status Text Updates
    analyzer_text = "Analyzer: ✅ Connected" if ANALYZER_STATUS["connected"] else "Analyzer: ❌ Disconnected"
    pox_text = f"POX Controller: {POX_STATUS.get('controller','?')} | Uptime: {POX_STATUS.get('uptime',0)}s"

    # 5. Return all 6 Outputs
    return (
        fig_s, # Output('spectrum', 'figure')
        fig_w, # Output('waterfall', 'figure')
        list(EVENTS)[:60], # Output('events_table', 'data')
        POX_STATUS.get('flow_rules', []), # Output('pox_flow_table', 'data')
        analyzer_text, # Output('analyzer_text', 'children')
        pox_text # Output('pox_text', 'children')
    )


# ============================================================
# THREADS
# ============================================================
threading.Thread(target=analyzer_receiver, daemon=True).start()
threading.Thread(target=poll_pox_status, daemon=True).start()

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    ip = get_host_ip()
    print(f"[Dashboard] Host IP detected: {ip}")
    print(f"[Dashboard] Listening for analyzer data on {HOST_DASH}:{LISTEN_PORT}")

    # This is normally run by run1.py, but for standalone test, it's here
    print(f"\n--- Starting Dash Server on http://{ip}:{DASH_PORT} ---")
    try:
        app.run_server(host=ip, port=DASH_PORT, debug=False)
    except Exception as e:
        print(f"\n[FATAL] Dash server failed to start: {e}")