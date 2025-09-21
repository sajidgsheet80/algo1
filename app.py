from fyers_apiv3 import fyersModel
from flask import Flask, request, render_template_string, jsonify, redirect
import threading
import webbrowser
import pandas as pd
from functools import reduce
import time
from collections import defaultdict, deque
import os
# ---- Credentials ----
client_id = "VMS68P9EK0-100"
secret_key = "ZJ0CFWZEL1"
redirect_uri = "https://algo1.onrender.com//callback"
grant_type = "authorization_code"
response_type = "code"
state = "sample"

# Step 1: Create session
appSession = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    response_type=response_type,
    grant_type=grant_type,
    state=state
)

# Flask app
app = Flask(__name__)
access_token_global = None
fyers = None
strike_prices = []
atm_strike = None
auto_refresh = False

# To store rolling 5-min history for LTP, Volume + OI Change
history = defaultdict(lambda: {
    "CE_LTP": deque(maxlen=300),
    "PE_LTP": deque(maxlen=300),
    "CE_VOL": deque(maxlen=300),
    "PE_VOL": deque(maxlen=300),
    "CE_OI": deque(maxlen=300),
    "PE_OI": deque(maxlen=300)
})

# HTML template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>NIFTY50 Option Chain</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f0f2f5; padding: 20px; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: center; }
        th { background-color: #4CAF50; color: white; }
        button { margin: 5px; padding: 10px; }
        .green { background-color: green; color: white; }
        .blue { background-color: blue; color: white; }
        .red { background-color: red; color: white; }
    </style>
</head>
<body>
    <h2>NIFTY50 Option Chain with 5-min Trends</h2>
    <div>
        <button onclick="login()" class="green">Login</button>
        <button onclick="fetchOnce()">Fetch Once</button>
        <button onclick="startAuto()" class="blue">Start Auto Refresh</button>
        <button onclick="stopAuto()" class="red">Stop Auto Refresh</button>
    </div>
    <br>
    <table id="optionTable">
        <thead>
            <tr>
            {% for col in columns %}
                <th>{{col}}</th>
            {% endfor %}
            </tr>
        </thead>
        <tbody>
            {% for row in rows %}
                <tr>
                {% for value in row %}
                    <td>{{value}}</td>
                {% endfor %}
                </tr>
            {% endfor %}
        </tbody>
    </table>

<script>
function login() {
    window.open("/login", "_blank");
}
function fetchOnce() {
    fetch("/fetch").then(res => res.json()).then(data => updateTable(data));
}
function startAuto() {
    fetch("/start_auto");
}
function stopAuto() {
    fetch("/stop_auto");
}
function updateTable(data) {
    const tbody = document.querySelector("#optionTable tbody");
    tbody.innerHTML = "";
    data.forEach(row => {
        const tr = document.createElement("tr");
        row.forEach(cell => {
            const td = document.createElement("td");
            td.innerText = cell;
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
}
setInterval(() => {
    fetch("/fetch").then(res => res.json()).then(data => updateTable(data));
}, 5000); // auto-refresh every 5 sec
</script>
</body>
</html>
"""

@app.route("/callback")
def callback():
    global access_token_global, fyers
    auth_code = request.args.get("auth_code")
    if auth_code:
        appSession.set_token(auth_code)
        token_response = appSession.generate_token()
        access_token_global = token_response.get("access_token")
        fyers = fyersModel.FyersModel(
            client_id=client_id,
            token=access_token_global,
            is_async=False,
            log_path=""
        )
        return "<h2>✅ Authentication Successful!</h2><p>You may now close this tab.</p>"
    return "❌ Authentication failed. Please retry."

@app.route("/login")
def login():
    login_url = appSession.generate_authcode()
    webbrowser.open(login_url, new=1)
    return redirect("/")

def fetch_option_chain_data():
    global fyers, strike_prices
    if fyers is None:
        return []
    try:
        data = {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 10, "timestamp": ""}
        response = fyers.optionchain(data=data)
        if "data" not in response or "optionsChain" not in response["data"]:
            return []
        options_data = response["data"]["optionsChain"]
        df = pd.DataFrame(options_data)

        pivots = []
        for field, tag in [
            ("ltp", "LTP"), ("volume", "VOLUME"),
            ("open_interest", "OI"), ("chng_in_oi", "OI_CHANGE")
        ]:
            if field in df.columns:
                piv = df.pivot_table(
                    index="strike_price",
                    columns="option_type",
                    values=field,
                    aggfunc="first"
                ).reset_index()
                if "CE" in piv.columns:
                    piv = piv.rename(columns={"CE": f"{tag}_CE"})
                if "PE" in piv.columns:
                    piv = piv.rename(columns={"PE": f"{tag}_PE"})
                pivots.append(piv)

        result = reduce(lambda l, r: pd.merge(l, r, on="strike_price", how="outer"), pivots)
        result = result.sort_values(by="strike_price").reset_index(drop=True)

        if not strike_prices:
            strike_prices.extend(result["strike_price"].tolist())

        rows = []
        for row in result.itertuples():
            now = time.time()
            if hasattr(row, "LTP_CE"):
                history[row.strike_price]["CE_LTP"].append((now, row.LTP_CE))
            if hasattr(row, "LTP_PE"):
                history[row.strike_price]["PE_LTP"].append((now, row.LTP_PE))
            if hasattr(row, "VOLUME_CE"):
                history[row.strike_price]["CE_VOL"].append((now, row.VOLUME_CE))
            if hasattr(row, "VOLUME_PE"):
                history[row.strike_price]["PE_VOL"].append((now, row.VOLUME_PE))
            if hasattr(row, "OI_CHANGE_CE"):
                history[row.strike_price]["CE_OI"].append((now, row.OI_CHANGE_CE))
            if hasattr(row, "OI_CHANGE_PE"):
                history[row.strike_price]["PE_OI"].append((now, row.OI_CHANGE_PE))

            def check_5min(hist):
                if len(hist) < 2:
                    return "No"
                old_time, old_val = hist[0]
                new_time, new_val = hist[-1]
                if new_time - old_time >= 300 and new_val > old_val:
                    return "Yes"
                return "No"

            values = (
                row.strike_price,
                getattr(row, "LTP_CE", "-"),
                getattr(row, "LTP_PE", "-"),
                getattr(row, "VOLUME_CE", "-"),
                getattr(row, "VOLUME_PE", "-"),
                getattr(row, "OI_CE", "-"),
                getattr(row, "OI_PE", "-"),
                check_5min(history[row.strike_price]["CE_LTP"]),
                check_5min(history[row.strike_price]["PE_LTP"]),
                check_5min(history[row.strike_price]["CE_VOL"]),
                check_5min(history[row.strike_price]["PE_VOL"]),
                check_5min(history[row.strike_price]["CE_OI"]),
                check_5min(history[row.strike_price]["PE_OI"])
            )
            rows.append(values)
        return rows
    except:
        return []

@app.route("/fetch")
def fetch():
    return jsonify(fetch_option_chain_data())

@app.route("/start_auto")
def start_auto():
    global auto_refresh
    auto_refresh = True
    return "Auto refresh started"

@app.route("/stop_auto")
def stop_auto():
    global auto_refresh
    auto_refresh = False
    return "Auto refresh stopped"

@app.route("/")
def index():
    columns = (
        "Strike Price", "CE LTP", "PE LTP",
        "CE Volume", "PE Volume", "CE OI", "PE OI",
        "CE LTP 5min ↑", "PE LTP 5min ↑",
        "CE Vol 5min ↑", "PE Vol 5min ↑",
        "CE OI 5min ↑", "PE OI 5min ↑"
    )
    rows = fetch_option_chain_data()
    return render_template_string(HTML_TEMPLATE, columns=columns, rows=rows)



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)




