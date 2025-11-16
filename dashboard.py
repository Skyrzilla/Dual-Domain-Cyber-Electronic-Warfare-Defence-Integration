import dash
from dash import dcc, html, Input, Output, State
import pandas as pd
import os
import subprocess
import threading
import time
import random
import atexit
import traceback
import math
import plotly.graph_objects as go
import re

# --- Imports for Logging, IP Finding, and JSON ---
import logging
import socket
from flask import request
import json
# ---

# --- Get the absolute base directory ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_PATH = "E:\\FactoryDigitalTwin"
DATA_PATH = os.path.join(BASE_PATH, "data", "sensor_data.csv")

# --- === THIS IS THE UPGRADED LOGGING FIX === ---
# This stops the 'KeyError: remote_addr' crash.

LOG_FILE = os.path.join(BASE_DIR, 'access.log')
try:
    with open(LOG_FILE, 'w'): # Clear the log on startup
        pass
except IOError:
    print(f"Could not clear log file: {LOG_FILE}")

# 1. Set up the file handler
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setLevel(logging.INFO)

# 2. Set up the custom formatter
# This formatter REQUIRES 'remote_addr'
formatter = logging.Formatter('%(asctime)s - IP: %(remote_addr)s - %(message)s')
file_handler.setFormatter(formatter)

# 3. Create our DEDICATED logger and add the handler
access_logger = logging.getLogger('AccessLogger')
access_logger.setLevel(logging.INFO)
access_logger.addHandler(file_handler)
access_logger.propagate = False # IMPORTANT: Stop it from bubbling up

# 4. We ALSO get the default werkzeug logger to SILENCE it.
# This stops it from logging "POST /_dash-update-component" and crashing.
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR) # Only log actual errors
# --- === END OF LOGGING FIX === ---


try:
    subprocess.Popen(["python", "E:\\FactoryDigitalTwin\\ML_Model\\train_model.py"])
    subprocess.Popen(["python", "E:\\FactoryDigitalTwin\\ML_Model\\predict_failure.py"])
    subprocess.Popen(["python", "E:\\FactoryDigitalTwin\\scripts\\generate_data.py"])
    subprocess.Popen(["python", "E:\\FactoryDigitalTwin\\ML_Model\\accuracy.py"])
except Exception as e:
    print(" Error launching background scripts:", e)
    traceback.print_exc()

def cleanup():
    if os.path.exists(DATA_PATH):
        os.remove(DATA_PATH)

atexit.register(cleanup)


machines_by_set = {
    "Set 1 – High Wave Band": ["Robotic Arm", "Automated Guided Vehicle (AGV)", "CNC Machine", "Inspection Robot"],
    "Set 2 – High Data": ["Compressor", "Conveyor Belt", "Temperature Sensor", "Pressure Gauge", "PLC"],
    "Set 3 – Special Signal": ["Boiler", "Cooling Unit", "Emergency Stop", "Vibration Sensor"]
}
all_machines = [m for lst in machines_by_set.values() for m in lst]
decagram_machines = sorted(all_machines, key=lambda x: -len(x))[:3] + all_machines[:6]
sensor_history = {sensor: [] for sensor in ["Temperature", "Pressure", "Vibration", "Latency(ms)", "SignalStrength(dBm)", "Bandwidth(Mbps)"]}
animation_position = {machine: 0 for machine in decagram_machines}
wave_position = 0

tech_comparison = {
    "Latency (ms)": {"4G": 50, "WiFi": 30, "5G": 5},
    "Power Usage (W)": {"4G": 5, "WiFi": 10, "5G": 3},
    "Bandwidth (Mbps)": {"4G": 50, "WiFi": 100, "5G": 1000},
}


app = dash.Dash(__name__)
app.title = "Factory Digital Twin"

# --- UPDATED: This hook now uses our DEDICATED logger ---
@app.server.before_request
def log_request_info():
    try:
        full_path = request.full_path
        user_agent = request.headers.get('User-Agent', 'N/A')
        
        # We pass the 'remote_addr' in the 'extra' dict
        # which our custom formatter is expecting.
        access_logger.info(
            f"Request: {full_path} | User-Agent: {user_agent}", 
            extra={'remote_addr': request.remote_addr}
        )
    except Exception:
        # Don't crash the app if logging fails
        pass
# --- END: LOG HOOK ---


app.layout = html.Div([
    # (Your full layout is correct and unchanged)
    html.Div([
        html.H1("Factory Digital Twin Dashboard", style={
            'color': '#fff',
            'textAlign': 'center',
            'margin': '0',
            'padding': '30px 0',
            'fontFamily': 'Segoe UI',
            'fontWeight': '700',
            'letterSpacing': '2px'
        })
    ], style={
        'background': 'linear-gradient(90deg, #1f1c2c 0%, #928dab 100%)',
        'boxShadow': '0 4px 12px rgba(0,0,0,0.3)',
        'borderBottom': '2px solid #444'
    }),

    html.Div(
        dcc.Tabs(id="tabs", value="block-diagram", children=[
            dcc.Tab(label=" Block Diagram", value="block-diagram", style={
                'padding': '14px',
                'color': '#fff',
                'backgroundColor': '#2c2c54',
                'fontWeight': 'bold'
            }, selected_style={
                'backgroundColor': '#40407a',
                'color': '#fff',
                'borderBottom': '3px solid #33d9b2'
            }),
            dcc.Tab(label="Sensor Data", value="sensor-data", style={
                'padding': '14px',
                'color': '#fff',
                'backgroundColor': '#2c2c54',
                'fontWeight': 'bold'
            }, selected_style={
                'backgroundColor': '#40407a',
                'color': '#fff',
                'borderBottom': '3px solid #33d9b2'
            }),
            dcc.Tab(label=" Tech Comparison", value="tech-comparison", style={
                'padding': '14px',
                'color': '#fff',
                'backgroundColor': '#2c2c54',
                'fontWeight': 'bold'
            }, selected_style={
                'backgroundColor': '#40407a',
                'color': '#fff',
                'borderBottom': '3px solid #33d9b2'
            }),
        ]),
        style={
            "margin": "0 60px",
            "border": "1px solid #555",
            "borderRadius": "8px",
            "overflow": "hidden",
            "boxShadow": "0 2px 6px rgba(0,0,0,0.2)"
        }
    ),

    html.Div(id="tab-content", style={"padding": "30px", "color": "white"}),
    dcc.Interval(id="interval", interval=1000, n_intervals=0),
    dcc.Store(id="clicked-machine", data="")
], style={
    "backgroundColor": "#121212",
    "minHeight": "100vh",
    "margin": "0",
    "fontFamily": "Segoe UI, sans-serif"
})


def fallback_sensor_data():
    """
    This function is started as a thread by run.py.
    It generates data if the CSV is missing.
    """
    while True:
        if not os.path.exists(DATA_PATH):
            data_list = []
            for machine in all_machines:
                temp = round(random.uniform(50, 120), 2)
                pressure = round(random.uniform(5, 25), 2)
                vibration = round(random.uniform(0, 15), 2)
                latency = round(random.uniform(1, 50), 2)
                bandwidth = round(random.uniform(100, 2000), 2)
                signal = round(random.uniform(-100, -70), 2)
                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                data_list.append([timestamp, machine, "SetX", temp, pressure, vibration, latency, bandwidth, signal])
                for s, val in zip(sensor_history.keys(), [temp, pressure, vibration, latency, bandwidth, signal]):
                    sensor_history[s].append(val)
                    if len(sensor_history[s]) > 20:
                        sensor_history[s].pop(0)
            df = pd.DataFrame(data_list, columns=[
                "Timestamp", "Machine", "SetType",
                "Temperature", "Pressure", "Vibration",
                "Latency(ms)", "Bandwidth(Mbps)", "SignalStrength(dBm)"
            ])
            df.to_csv(DATA_PATH, mode='a', header=not os.path.exists(DATA_PATH), index=False)
        time.sleep(5)


def get_latest_data(machine, df):
    # (This function is correct and unchanged)
    latest = df[df["Machine"] == machine].sort_values("Timestamp").tail(1)
    if latest.empty:
        return ["No data"] * 4
    row = latest.iloc[0]
    return [
        f"{float(row['Temperature']):.1f} °C",
        f"{float(row['Pressure']):.1f} bar",
        f"{float(row['Vibration']):.1f} g",
        f"{float(row['Bandwidth(Mbps)']):.1f} Mbps"
    ]


def render_block_diagram(df, clicked_machine):
    # (This function is correct and unchanged)
    global wave_position
    width, height = 1000, 700
    center_x, center_y = width // 2, height // 2 - 50
    radius = 280
    base_y = center_y + 300
    transmitter_y = center_y
    wave_position = (wave_position + 0.04) % 1
    elements = []
    angle_step = 2 * math.pi / len(decagram_machines)

    for idx, machine in enumerate(decagram_machines):
        angle = idx * angle_step
        x = center_x + radius * math.cos(angle)
        y = center_y + radius * math.sin(angle)
        temp, pressure, vibration, bandwidth = get_latest_data(machine, df)

        animation_position[machine] = (animation_position[machine] + 0.03) % 1
        dot_x = center_x * animation_position[machine] + x * (1 - animation_position[machine])
        dot_y = center_y * animation_position[machine] + y * (1 - animation_position[machine])
        tooltip = f"{machine}<br>{temp}<br>{pressure}<br>{vibration}<br>{bandwidth}"

        elements.extend([
            html.Div(machine, id={'type': 'machine-block', 'index': machine}, n_clicks=0, style={
                "position": "absolute", "left": f"{x}px", "top": f"{y}px",
                "transform": "translate(-50%, -50%)", "fontWeight": "bold",
                "padding": "6px 10px", "border": "2px solid #fff", "borderRadius": "8px",
                "backgroundColor": "#0d47a1", "color": "white", "cursor": "pointer",
                "textAlign": "center", "zIndex": 2, "whiteSpace": "nowrap"
            }),
            html.Div(title=tooltip, style={
                "position": "absolute", "left": f"{dot_x}px", "top": f"{dot_y}px",
                "width": "10px", "height": "10px", "backgroundColor": "blue",
                "borderRadius": "50%", "zIndex": 3
            })
        ])

        if machine == clicked_machine:
            elements.append(html.Div([
                html.Div(f" {machine}", style={"fontWeight": "bold", "marginBottom": "6px"}),
                html.Div(f" Temp: {temp}"),
                html.Div(f" Pressure: {pressure}"),
                html.Div(f" Vibration: {vibration}"),
                html.Div(f" Bandwidth: {bandwidth}")
            ], style={
                "position": "absolute", "left": f"{x+70}px", "top": f"{y}px",
                "transform": "translateY(-50%)", "backgroundColor": "#1e1e1e",
                "color": "white", "padding": "10px", "borderRadius": "8px",
                "border": "1px solid white", "zIndex": 10
            }))

    wave_y = transmitter_y + wave_position * 340

    elements.append(html.Div("5G Transmitter", style={
        "position": "absolute", "left": f"{center_x}px", "top": f"{transmitter_y}px",
        "transform": "translate(-50%, -50%)", "fontWeight": "bold",
        "padding": "10px", "border": "2px solid #000", "borderRadius": "10px",
        "backgroundColor": "#fff3e0", "color": "black",  
        "textAlign": "center", "zIndex": 2
    }))

    elements.append(html.Div(style={
        "position": "absolute", "left": f"{center_x}px", "top": f"{wave_y}px",
        "width": "12px", "height": "12px", "backgroundColor": "green",
        "borderRadius": "50%", "zIndex": 3
        }))

    elements.append(html.Div("Base Station (Digital Twin)", style={
        "position": "absolute", "left": f"{center_x}px", "top": f"{transmitter_y + 340}px",
        "transform": "translate(-50%, -50%)", "fontWeight": "bold",
        "padding": "10px", "border": "2px solid #000", "borderRadius": "10px",
        "backgroundColor": "#ede7f6", "color": "black",  
        "textAlign": "center", "zIndex": 2
    }))

    return html.Div(elements, style={"position": "relative", "width": f"{width}px", "height": f"{height}px", "margin": "0 auto"})


# --- "BOMB-PROOF" CALLBACK ---
# (This is the robust version from our previous discussion)
@app.callback(
    Output("clicked-machine", "data"),
    [Input({'type': 'machine-block', 'index': dash.dependencies.ALL}, 'n_clicks')],
    prevent_initial_call=True 
)
def update_click_data(n_clicks_list):
    ctx = dash.callback_context
    
    if not ctx.triggered:
        return dash.no_update

    click_value = ctx.triggered[0].get('value')
    
    if not click_value or click_value == 0:
        return dash.no_update

    try:
        prop_id_str = ctx.triggered[0]['prop_id']
        
        if not prop_id_str or '.n_clicks' not in prop_id_str:
            return dash.no_update

        button_id_json = prop_id_str.split('.')[0]
        id_dict = json.loads(button_id_json)
        
        if id_dict.get('type') == 'machine-block' and id_dict.get('index'):
            return id_dict.get('index')
        else:
            return dash.no_update
            
    except Exception as e:
        print(f"[dashboard.py] CRITICAL ERROR in update_click_data: {e}")
        print(f"Triggering prop_id was: {ctx.triggered[0].get('prop_id')}")
        return dash.no_update 

    return dash.no_update
# --- END OF CALLBACK ---


@app.callback(Output("tab-content", "children"), [Input("tabs", "value"), Input("interval", "n_intervals"), Input("clicked-machine", "data")])
def update_dashboard(tab, n, clicked_machine):
    # (This function is correct and unchanged)
    if not os.path.exists(DATA_PATH):
        return html.Div("Data not found.")
    try:
        df = pd.read_csv(DATA_PATH)
        recent = df.tail(20)
        for sensor in sensor_history.keys():
            if sensor in recent.columns:
                sensor_history[sensor] = list(recent[sensor])
    except Exception as e:
        if os.path.exists(DATA_PATH) and os.path.getsize(DATA_PATH) == 0:
             return html.Div("Generating data...")
        return html.Div(f"CSV Read Error: {str(e)}")

    if tab == "block-diagram":
        return render_block_diagram(df, clicked_machine)

    elif tab == "sensor-data":
        fig1 = go.Figure()
        for sensor in ["Temperature", "Pressure", "Vibration", "Latency(ms)", "SignalStrength(dBm)"]:
            if sensor in sensor_history and sensor_history[sensor]:
                fig1.add_trace(go.Scatter(
                    y=sensor_history[sensor],
                    mode="lines+markers",
                    name=sensor
                ))
        fig1.update_layout(
            title="Sensor Data (Temp, Pressure, Vibration, Latency, Signal Strength)",
            xaxis_title="Time",
            yaxis_title="Sensor Value",
            paper_bgcolor='rgb(23,23,23)',
            plot_bgcolor='rgb(23,23,23)',
            font=dict(color="white")
        )

        fig2 = go.Figure()
        if "Bandwidth(Mbps)" in sensor_history and sensor_history["Bandwidth(Mbps)"]:
            fig2.add_trace(go.Scatter(
                y=sensor_history["Bandwidth(Mbps)"],
                mode="lines+markers",
                name="Bandwidth"
            ))
        fig2.update_layout(
            title="Bandwidth",
            xaxis_title="Time",
            yaxis_title="Mbps",
            paper_bgcolor='rgb(23,23,23)',
            plot_bgcolor='rgb(23,23,23)',
            font=dict(color="white")
        )

        return html.Div([
            dcc.Graph(figure=fig1),
            dcc.Graph(figure=fig2)
        ])

    elif tab == "tech-comparison":
        fig = go.Figure()
        for metric, values in tech_comparison.items():
            fig.add_trace(go.Bar(
                x=["4G", "WiFi", "5G"],
                y=[values["4G"], values["WiFi"], values["5G"]],
                name=metric
            ))
        fig.update_layout(
            barmode="group",
            title="4G vs WiFi vs 5G Comparison",
            xaxis_title="Technology",
            yaxis_title="Value",
            font=dict(color="white"),
            paper_bgcolor='rgb(23,23,23)',
            plot_bgcolor='rgb(23,23,23)'
        )
        return dcc.Graph(figure=fig)


# --- Replaced ipconfig parse with robust socket method ---
def get_wifi_ipv4():
    """Finds the local WiFi IPv4 address."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to a public DNS server (doesn't send data)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        return ip
    except Exception as e:
        print(f"[dashboard.py] Error finding IP: {e}")
        return '127.0.0.1'  # Fallback to loopback
    finally:
        if s:
            s.close()
# --- END UPDATE ---

# (your entire dashboard.py remains unchanged)

# --- ADDED START: optional helper (can be imported by other modules) ---
def count_unique_clients_from_accesslog(access_log_path, recent_seconds=300):
    """Utility: count distinct client IPs in the access.log."""
    import time, re
    ips = set()
    try:
        with open(access_log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-1000:]
            for line in lines:
                m = re.search(r"IP:\s*(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    ips.add(m.group(1))
    except Exception:
        pass
    return len(ips)
# --- ADDED END ---


if __name__ == "__main__":
    ip = get_wifi_ipv4()
    if not ip:
        print("Warning: Could not find local IP. Defaulting to 127.0.0.1")
        ip = "127.0.0.1"
    
    # --- UPDATED: Run on port 8051 to match user's attacks ---
    print(f"--- Starting Dash Server for testing on http://{ip}:8051 ---")
    # Start the fallback data generator only when testing
    threading.Thread(target=fallback_sensor_data, daemon=True).start()
    app.run(host=ip, port=8051, debug=True)
