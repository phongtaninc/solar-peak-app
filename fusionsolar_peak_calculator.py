"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   FusionSolar  ·  Monthly On-Peak / Off-Peak Energy Calculator              ║
║   Thailand PEA TOU Tariff  ·  Streamlit Web App  ·  Multi-Plant Edition     ║
╚══════════════════════════════════════════════════════════════════════════════╝

HOW TO DEPLOY FREE ON STREAMLIT CLOUD (permanent URL, no ngrok needed)
───────────────────────────────────────────────────────────────────────
1. Create a free GitHub account at github.com
2. Create a new repository (e.g.  "solar-peak-app")
3. Upload TWO files into it:
      fusionsolar_peak_calculator.py   ← this file
      requirements.txt                 ← see contents below
4. Go to share.streamlit.io → Sign in with GitHub → "New app"
5. Select your repo, branch: main, file: fusionsolar_peak_calculator.py
6. Click Deploy → get a permanent URL like:
      https://your-name-solar-peak-app.streamlit.app

requirements.txt contents:
───────────────────────────
    streamlit
    requests
    pandas
    holidays
    openpyxl

TOU RULES (Thailand PEA, effective May 2023 / B.E. 2566)
──────────────────────────────────────────────────────────
    On-Peak  : Mon-Fri 09:00-22:00  (EXCLUDING public holidays)
    Off-Peak : Mon-Fri 22:00-09:00  +  Sat  +  Sun  +  Public Holidays (all day)
"""

# ── Standard library ──────────────────────────────────────────────────────────
import calendar
import time
from datetime import date, datetime, timezone, timedelta

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd
import requests
import streamlit as st

try:
    import holidays as holidays_lib
    HOLIDAYS_AVAILABLE = True
except ImportError:
    HOLIDAYS_AVAILABLE = False

# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

BASE_URL        = "https://sg5.fusionsolar.huawei.com"
USERNAME        = "chowenergyapi"
PASSWORD        = "chow12345"
REQUEST_TIMEOUT = 30
MAX_RETRIES     = 3
RETRY_DELAY     = 5
MAX_PLANTS      = 10   # number of plant input rows in the UI

# ── PEA Official Off-Peak Holidays 2026 (B.E. 2569) ──────────────────────────
# Source: PEA TOU Off-Peak Calendar 2569 (official document)
# Compensatory holidays are EXCLUDED per PEA rules.
PEA_OFFPEAK_HOLIDAYS_2026: set[date] = {
    date(2026,  1,  1),   # New Year's Day
    date(2026,  3,  3),   # Makha Bucha
    date(2026,  4,  6),   # Chakri Memorial Day
    date(2026,  4, 13),   # Songkran Day 1
    date(2026,  4, 14),   # Songkran Day 2
    date(2026,  4, 15),   # Songkran Day 3
    date(2026,  5,  1),   # Labour Day
    date(2026,  5,  4),   # Coronation Day
    date(2026,  6,  3),   # Queen Suthida's Birthday
    date(2026,  7, 28),   # King Vajiralongkorn's Birthday
    date(2026,  7, 29),   # Asalha Bucha
    date(2026,  7, 30),   # Buddhist Lent
    date(2026,  8, 12),   # Queen Mother's Birthday / Mother's Day
    date(2026, 10, 13),   # King Bhumibol Memorial Day
    date(2026, 10, 23),   # Chulalongkorn Day
    date(2026, 11, 10),   # Constitution Day (observed)
    date(2026, 12, 31),   # New Year's Eve
}

# ═════════════════════════════════════════════════════════════════════════════
# HOLIDAY & PEAK LOGIC
# ═════════════════════════════════════════════════════════════════════════════

def get_thai_holidays(year: int) -> set[date]:
    """Return set of Thai public holiday dates for the given year."""
    if year == 2026:
        return PEA_OFFPEAK_HOLIDAYS_2026
    if HOLIDAYS_AVAILABLE:
        th = holidays_lib.country_holidays("TH", years=year)
        return set(th.keys())
    return set()


def is_on_peak(dt_local: datetime, holiday_dates: set[date]) -> bool:
    """
    PEA TOU rule:
      On-Peak  = Mon-Fri, hour 9-21 (start of hour), NOT a public holiday
      Off-Peak = everything else
    """
    d       = dt_local.date()
    hour    = dt_local.hour    # 0-23
    weekday = d.weekday()      # 0=Mon, 6=Sun
    if weekday >= 5:           # Weekend
        return False
    if d in holiday_dates:     # Public holiday
        return False
    return 9 <= hour <= 21     # On-Peak hours


# ═════════════════════════════════════════════════════════════════════════════
# FUSIONSOLAR API HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _post(session: requests.Session, path: str, payload: dict) -> dict:
    """HTTP POST with retry. Raises RuntimeError on failure."""
    url = f"{BASE_URL}{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") is False:
                raise RuntimeError(
                    f"API failCode={data.get('failCode','?')}: "
                    f"{data.get('message','no message')}"
                )
            return data
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as exc:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            else:
                raise RuntimeError(
                    f"Network error after {MAX_RETRIES} attempts: {exc}"
                ) from exc


def api_login(session: requests.Session) -> str:
    """Login to FusionSolar and return the XSRF-TOKEN."""
    resp = session.post(
        f"{BASE_URL}/thirdData/login",
        json={"userName": USERNAME, "systemCode": PASSWORD},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(
            f"Login failed — failCode={body.get('failCode','?')}: "
            f"{body.get('message','Unknown error')}"
        )
    token = (resp.headers.get("xsrf-token")
             or resp.headers.get("XSRF-TOKEN")
             or resp.headers.get("Xsrf-Token"))
    if not token:
        raise RuntimeError(
            "Login OK but XSRF-TOKEN not found in response headers.\n"
            f"Headers received: {dict(resp.headers)}"
        )
    session.headers.update({"xsrf-token": token})
    return token


def api_logout(session: requests.Session, token: str) -> None:
    """Logout (best-effort, non-critical)."""
    try:
        session.post(
            f"{BASE_URL}/thirdData/logout",
            json={"xsrfToken": token},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        pass


def get_all_plants(session: requests.Session) -> list[dict]:
    """Return all plants accessible under the API account (handles pagination)."""
    plants, page = [], 1
    while True:
        data      = _post(session, "/thirdData/stations", {"pageNo": page})
        page_data = data.get("data", {})
        plants.extend(page_data.get("list", []))
        if page >= page_data.get("pageCount", 1):
            break
        page += 1
        time.sleep(0.3)
    return plants


def get_hourly_data_one_day(
    session: requests.Session,
    station_code: str,
    day: date,
) -> list[dict]:
    """
    Fetch hourly generation records for one plant on one day.
    Returns list of {collectTime_ms, kwh}.
    """
    dt_utc     = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    collect_ms = int(dt_utc.timestamp() * 1000)
    try:
        data = _post(
            session,
            "/thirdData/getKpiStationHour",
            {"stationCodes": station_code, "collectTime": collect_ms},
        )
    except RuntimeError:
        return []

    records = []
    for item in data.get("data", []):
        kpi     = item.get("dataItemMap", {})
        kwh_raw = kpi.get("inverter_power") or kpi.get("inverterYield")
        kwh     = float(kwh_raw) if kwh_raw is not None else 0.0
        records.append({
            "collectTime_ms": item.get("collectTime", collect_ms),
            "kwh": kwh,
        })
    return records


# ═════════════════════════════════════════════════════════════════════════════
# CORE CALCULATION — ONE PLANT
# ═════════════════════════════════════════════════════════════════════════════

def calculate_one_plant(
    session: requests.Session,
    plant: dict,
    year: int,
    month: int,
    holiday_dates: set[date],
    status_fn,
    progress_fn,
) -> dict:
    """
    Fetch hourly data for every day of year/month for one plant.
    Returns dict: plant_name, capacity_kw, on_peak_kwh, off_peak_kwh, total_kwh
    """
    plant_code    = plant["plantCode"]
    days_in_month = calendar.monthrange(year, month)[1]
    on_peak_kwh   = 0.0
    off_peak_kwh  = 0.0

    for day_num in range(1, days_in_month + 1):
        day = date(year, month, day_num)
        progress_fn(day_num / days_in_month)
        status_fn(
            f"⚡ {plant.get('plantName','?')} — "
            f"{day.strftime('%d %b %Y')} ({day_num}/{days_in_month})"
        )

        for rec in get_hourly_data_one_day(session, plant_code, day):
            # Convert UTC ms → Thailand local time (UTC+7, no DST)
            dt_utc   = datetime.fromtimestamp(
                rec["collectTime_ms"] / 1000, tz=timezone.utc
            )
            dt_local = (dt_utc + timedelta(hours=7)).replace(tzinfo=None)

            if is_on_peak(dt_local, holiday_dates):
                on_peak_kwh += rec["kwh"]
            else:
                off_peak_kwh += rec["kwh"]

        time.sleep(0.25)

    total = on_peak_kwh + off_peak_kwh
    return {
        "plant_name":   plant.get("plantName", "Unknown"),
        "capacity_kw":  plant.get("capacity", "N/A"),
        "on_peak_kwh":  round(on_peak_kwh,  3),
        "off_peak_kwh": round(off_peak_kwh, 3),
        "total_kwh":    round(total,         3),
    }


# ═════════════════════════════════════════════════════════════════════════════
# STREAMLIT PAGE CONFIG & CSS
# ═════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="FusionSolar Peak Calculator",
    page_icon="☀️",
    layout="wide",
)

st.markdown("""
<style>
/* ── Titles ── */
.main-title {
    font-size: 1.9rem; font-weight: 800;
    color: #E65100; margin-bottom: 0;
}
.sub-title {
    font-size: 0.95rem; color: #555;
    margin-top: 0.15rem; margin-bottom: 1rem;
}

/* ── Results table ── */
.result-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.95rem;
    margin-top: 0.8rem;
    margin-bottom: 1.2rem;
}
.result-table th {
    background-color: #E65100;
    color: #FFFFFF;
    padding: 11px 16px;
    font-weight: 700;
    border: 1px solid #c0c0c0;
    text-align: center;
}
.result-table th.left { text-align: left; }

/* ALL data cells: force black text */
.result-table td {
    padding: 9px 16px;
    border: 1px solid #ddd;
    color: #111111 !important;
    text-align: right;
}
.result-table td.left {
    text-align: left;
    font-weight: 600;
    color: #111111 !important;
}
.result-table tbody tr:nth-child(even) td { background-color: #FFF8F0; }
.result-table tbody tr:nth-child(odd)  td { background-color: #FFFFFF; }
.result-table tbody tr:hover           td { background-color: #FFE5CC; }

/* Grand total row */
.total-row td {
    background-color: #E3F2FD !important;
    font-weight: 700 !important;
    color: #111111 !important;
    border-top: 2px solid #1565C0 !important;
}

/* Error cell */
.err-cell {
    color: #B00020 !important;
    text-align: center !important;
    font-style: italic;
}

/* Status banner */
.status-box {
    background: #FFF3E0;
    border: 1px solid #FF6B00;
    border-radius: 6px;
    padding: 8px 14px;
    color: #BF360C;
    font-weight: 600;
    font-size: 0.9rem;
    margin-bottom: 6px;
}
</style>
""", unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════════════════════
# UI — HEADER
# ═════════════════════════════════════════════════════════════════════════════

st.markdown(
    '<p class="main-title">☀️ FusionSolar On-Peak / Off-Peak Calculator</p>',
    unsafe_allow_html=True,
)
st.markdown(
    '<p class="sub-title">'
    'Thailand PEA TOU Tariff  ·  Huawei FusionSolar API  ·  Multi-Plant Edition'
    '</p>',
    unsafe_allow_html=True,
)

with st.expander("📋 Thailand PEA TOU Rules", expanded=False):
    st.markdown("""
| Period | Days | Hours |
|---|---|---|
| 🔴 **On-Peak** | Mon – Fri (excl. public holidays) | 09:00 – 22:00 |
| 🟢 **Off-Peak** | Mon – Fri (excl. public holidays) | 22:00 – 09:00 |
| 🟢 **Off-Peak** | Saturday & Sunday | All day |
| 🟢 **Off-Peak** | Public Holidays | All day |

> Source: PEA TOU Off-Peak Calendar B.E. 2569 / 2026
    """)

st.divider()

# ═════════════════════════════════════════════════════════════════════════════
# UI — MONTH / YEAR
# ═════════════════════════════════════════════════════════════════════════════

months = [
    "January","February","March","April","May","June",
    "July","August","September","October","November","December",
]
col_m, col_y, _ = st.columns([2, 2, 5])
with col_m:
    selected_month_name = st.selectbox("📅 Month", months, index=3)
    selected_month = months.index(selected_month_name) + 1
with col_y:
    selected_year = int(st.number_input(
        "📅 Year", min_value=2020, max_value=2035, value=2026, step=1
    ))

# Holiday preview banner
holiday_dates_all = get_thai_holidays(selected_year)
month_holidays = sorted(
    d for d in holiday_dates_all
    if d.year == selected_year and d.month == selected_month
)
if month_holidays:
    hol_str = ", ".join(d.strftime("%d %b") for d in month_holidays)
    st.info(
        f"🗓️ Public holidays in {selected_month_name} {selected_year}: "
        f"**{hol_str}** — treated as Off-Peak all day"
    )
else:
    st.info(f"🗓️ No public holidays in {selected_month_name} {selected_year}.")

st.divider()

# ═════════════════════════════════════════════════════════════════════════════
# UI — PLANT NAME INPUTS  (10 rows, 2 per row)
# ═════════════════════════════════════════════════════════════════════════════

st.markdown("### 🏭 Enter Plant Names (up to 10)")
st.caption(
    "Type plant names exactly as shown in FusionSolar (case-insensitive). "
    "Leave rows blank to skip."
)

plant_inputs = []
for i in range(0, MAX_PLANTS, 2):
    c1, c2 = st.columns(2)
    with c1:
        v = st.text_input(
            f"Plant {i+1}", key=f"p_{i+1}",
            placeholder=f"Plant name {i+1}"
        )
        plant_inputs.append(v.strip())
    with c2:
        if i + 1 < MAX_PLANTS:
            v2 = st.text_input(
                f"Plant {i+2}", key=f"p_{i+2}",
                placeholder=f"Plant name {i+2}"
            )
            plant_inputs.append(v2.strip())

plant_list = [p for p in plant_inputs if p]   # remove blanks

st.divider()

# ═════════════════════════════════════════════════════════════════════════════
# UI — CALCULATE BUTTON
# ═════════════════════════════════════════════════════════════════════════════

n_plants = len(plant_list)
calc_btn = st.button(
    f"⚡  Calculate  {selected_month_name} {selected_year}  "
    f"({n_plants} plant{'s' if n_plants != 1 else ''})",
    type="primary",
    use_container_width=True,
    disabled=(n_plants == 0),
)
if n_plants == 0:
    st.caption("⬆️ Enter at least one plant name to enable the button.")

# ═════════════════════════════════════════════════════════════════════════════
# CALCULATION
# ═════════════════════════════════════════════════════════════════════════════

if calc_btn and plant_list:

    progress_bar = st.progress(0)
    status_el    = st.empty()
    results      = []
    session      = None
    token        = None

    def show_status(msg: str):
        status_el.markdown(
            f'<div class="status-box">{msg}</div>',
            unsafe_allow_html=True,
        )

    try:
        # Login
        show_status("🔐 Authenticating with FusionSolar API…")
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, */*",
        })
        token = api_login(session)

        # Fetch full plant list
        show_status("📋 Fetching plant list from API…")
        all_plants = get_all_plants(session)
        plant_map  = {
            p.get("plantName", "").strip().lower(): p
            for p in all_plants
        }

        holiday_dates = get_thai_holidays(selected_year)

        # Process each plant
        for idx, pname in enumerate(plant_list):
            matched = plant_map.get(pname.lower())

            if not matched:
                results.append({
                    "plant_name":   pname,
                    "capacity_kw":  "N/A",
                    "on_peak_kwh":  None,
                    "off_peak_kwh": None,
                    "total_kwh":    None,
                    "error": "Plant not found in account",
                })
                continue

            def _status(msg, _idx=idx):
                show_status(f"Plant {_idx+1}/{len(plant_list)}  ·  {msg}")

            def _progress(pct, _idx=idx):
                overall = (_idx + pct) / len(plant_list)
                progress_bar.progress(min(overall, 1.0))

            r = calculate_one_plant(
                session       = session,
                plant         = matched,
                year          = selected_year,
                month         = selected_month,
                holiday_dates = holiday_dates,
                status_fn     = _status,
                progress_fn   = _progress,
            )
            r["error"] = None
            results.append(r)

        # Logout
        api_logout(session, token)
        session.close()

        progress_bar.progress(1.0)
        show_status("✅ All plants processed!")
        time.sleep(0.8)
        progress_bar.empty()
        status_el.empty()

        # ══════════════════════════════════════════════════════════════════
        # RESULTS TABLE
        # ══════════════════════════════════════════════════════════════════
        st.markdown(
            f"### 📊 Results — {selected_month_name} {selected_year}"
        )

        successful = [r for r in results if not r["error"]]
        total_on   = sum(r["on_peak_kwh"]  for r in successful)
        total_off  = sum(r["off_peak_kwh"] for r in successful)
        total_all  = sum(r["total_kwh"]    for r in successful)

        # Build table rows
        body_rows = ""
        for r in results:
            if r["error"]:
                body_rows += f"""
  <tr>
    <td class="left">{r['plant_name']}</td>
    <td class="err-cell" colspan="3">❌ {r['error']}</td>
  </tr>"""
            else:
                body_rows += f"""
  <tr>
    <td class="left">{r['plant_name']}</td>
    <td>{r['on_peak_kwh']:,.2f}</td>
    <td>{r['off_peak_kwh']:,.2f}</td>
    <td>{r['total_kwh']:,.2f}</td>
  </tr>"""

        # Grand total row (only when 2+ successful plants)
        total_row = ""
        if len(successful) > 1:
            total_row = f"""
  <tr class="total-row">
    <td class="left">📊 TOTAL ({len(successful)} plants)</td>
    <td>{total_on:,.2f}</td>
    <td>{total_off:,.2f}</td>
    <td>{total_all:,.2f}</td>
  </tr>"""

        st.markdown(f"""
<table class="result-table">
  <thead>
    <tr>
      <th class="left">🏭 Plant Name</th>
      <th>🔴 On-Peak (kWh)</th>
      <th>🟢 Off-Peak (kWh)</th>
      <th>⚡ Total (kWh)</th>
    </tr>
  </thead>
  <tbody>
    {body_rows}
    {total_row}
  </tbody>
</table>
""", unsafe_allow_html=True)

        # ══════════════════════════════════════════════════════════════════
        # COPY-PASTE BOX  (tab-separated → paste directly into Excel)
        # ══════════════════════════════════════════════════════════════════
        st.markdown("---")
        st.markdown("#### 📋 Copy & Paste into Excel")
        st.caption(
            "Click inside the text box → **Ctrl+A** (select all) → "
            "**Ctrl+C** (copy) → open Excel → **Ctrl+V** (paste).  "
            "Columns are tab-separated and map directly to Excel cells."
        )

        lines = ["Plant Name\tOn-Peak (kWh)\tOff-Peak (kWh)\tTotal (kWh)"]
        for r in results:
            if r["error"]:
                lines.append(f"{r['plant_name']}\tERROR\tERROR\tERROR")
            else:
                lines.append(
                    f"{r['plant_name']}\t"
                    f"{r['on_peak_kwh']:.2f}\t"
                    f"{r['off_peak_kwh']:.2f}\t"
                    f"{r['total_kwh']:.2f}"
                )
        if len(successful) > 1:
            lines.append(
                f"TOTAL ({len(successful)} plants)\t"
                f"{total_on:.2f}\t"
                f"{total_off:.2f}\t"
                f"{total_all:.2f}"
            )

        st.text_area(
            label="",
            value="\n".join(lines),
            height=min(320, 50 + 30 * len(lines)),
        )

        # ── CSV download ───────────────────────────────────────────────────
        df_export = pd.DataFrame([
            {
                "Plant Name":     r["plant_name"],
                "On-Peak (kWh)":  r["on_peak_kwh"]  if not r["error"] else "",
                "Off-Peak (kWh)": r["off_peak_kwh"] if not r["error"] else "",
                "Total (kWh)":    r["total_kwh"]    if not r["error"] else "",
                "Error":          r["error"] or "",
            }
            for r in results
        ])
        st.download_button(
            label="⬇️  Download CSV",
            data=df_export.to_csv(index=False).encode("utf-8"),
            file_name=(
                f"fusionsolar_peak_{selected_year}_{selected_month:02d}.csv"
            ),
            mime="text/csv",
        )

    except RuntimeError as exc:
        progress_bar.empty()
        status_el.empty()
        st.error(f"❌ Error:\n\n{exc}")
    except Exception as exc:
        if session:
            try:
                session.close()
            except Exception:
                pass
        progress_bar.empty()
        status_el.empty()
        st.error(f"❌ Unexpected error: {exc}")
        raise

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "Data: Huawei FusionSolar NBI API v25.4.0  ·  "
    "TOU: PEA Electricity Tariff (effective May 2023)  ·  "
    "Holidays: PEA Off-Peak Calendar B.E. 2569 / 2026"
)
