"""
Aurora Air HK Operations Control Panel
Multi-Wave Network Flow & Multi-Swap Fleet Optimizer
Updated: Supports 'arrival_time' in flights CSV (with automatic midnight wrap calculation).
"""

import csv
import io
import math
import os
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import plotly.express as px
import streamlit as st

# --- CONSTANTS ---
MINUTES_PER_WEEK = 7 * 24 * 60  # 10,080 minutes
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="Aurora Air HK Operations Control Panel",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- BRANDING HEADER ---
col_logo, col_title = st.columns([1, 4])
logo_path = (
    "logo.png"
    if os.path.exists("logo.png")
    else ("input_file_2.png" if os.path.exists("input_file_2.png") else None)
)

with col_logo:
    if logo_path:
        st.image(logo_path, use_container_width=True)
    else:
        uploaded_logo = st.file_uploader(
            "Upload Logo",
            type=["png", "jpg", "jpeg", "webp"],
            label_visibility="collapsed",
        )
        if uploaded_logo:
            st.image(uploaded_logo, use_container_width=True)

with col_title:
    st.title("Aurora Air HK Operations Control Panel")
    st.caption(
        "Multi-Wave Network Flow Optimizer, Maintenance Compliance & Route Slot Discovery"
    )

st.markdown("---")


# --- TIME CONVERSION UTILITIES ---
def parse_days(day_str: str) -> List[int]:
    s = str(day_str).strip().lower()
    if s in ("daily", "all", "*"):
        return list(range(7))
    m = re.match(r"^([1-7])-([1-7])$", s)
    if m:
        st_d, en_d = int(m.group(1)) - 1, int(m.group(2)) - 1
        return (
            list(range(st_d, en_d + 1))
            if st_d <= en_d
            else list(range(st_d, 7)) + list(range(0, en_d + 1))
        )
    if s.isdigit():
        return sorted({int(c) - 1 for c in s if "1" <= c <= "7"})
    return sorted({DAY_MAP[t[:3]] for t in re.split(r"[,/ ]+", s) if t[:3] in DAY_MAP})


def parse_clock_minutes(t_str: str) -> int:
    """Parses 'HH:MM' into minutes from 00:00 (0 to 1439)."""
    hh, mm = map(int, str(t_str).strip().split(":"))
    return hh * 60 + mm


def parse_time(day_idx: int, t_str: str) -> int:
    """Converts day index (0..6) and 'HH:MM' into minutes of the week (0..10079)."""
    return day_idx * 1440 + parse_clock_minutes(t_str)


def fmt_time(m: int) -> str:
    m %= MINUTES_PER_WEEK
    d, rem = divmod(m, 1440)
    return f"{DAYS[d]} {rem // 60:02d}:{rem % 60:02d}"


def minutes_to_iso_date(minute: int) -> str:
    minute %= MINUTES_PER_WEEK
    d_idx, rem = divmod(minute, 1440)
    hh, mm = divmod(rem, 60)
    day_num = 5 + d_idx  # Anchor calendar week Mon 2026-01-05 to Sun 2026-01-11
    return f"2026-01-{day_num:02d} {hh:02d}:{mm:02d}:00"


def extract_flight_num(fid: str) -> Tuple[str, Optional[int]]:
    base = fid.split("_")[0].strip()
    m = re.match(r"^([A-Za-z]+)(\d+)$", base)
    if m:
        return m.group(1).upper(), int(m.group(2))
    return base.upper(), None


# --- DATA STRUCTURES ---
@dataclass
class Aircraft:
    aircraft_id: str
    fleet_type: str
    aircraft_type_code: str
    base_airport: str
    min_maint_minutes: int
    min_ground_maint_min: int


@dataclass
class FlightLeg:
    flight_id: str
    base_id: str
    origin: str
    destination: str
    allowed_types: Set[str]
    dep_time: int
    arr_time: int
    duration: int
    turnaround: int


@dataclass
class RotationPairing:
    outbound: FlightLeg
    inbound: FlightLeg
    dep_hub: int
    arr_hub: int
    allowed_types: Set[str]


def parse_fleet(csv_content: str, default_hub: str = "HKG") -> List[Aircraft]:
    reader = csv.DictReader(io.StringIO(csv_content))
    fleet = []
    for r in reader:
        fleet.append(
            Aircraft(
                aircraft_id=r["aircraft_id"].strip(),
                fleet_type=r.get("fleet_type", "Widebody/Narrowbody").strip(),
                aircraft_type_code=r["aircraft_type_code"].strip().upper(),
                base_airport=r.get("base_airport", default_hub).strip(),
                min_maint_minutes=int(float(r.get("min_maint_hours", 10.6)) * 60),
                min_ground_maint_min=int(r.get("min_ground_maint_min", 227)),
            )
        )
    return fleet


def parse_flights(csv_content: str) -> List[FlightLeg]:
    """
    Parses flights CSV.
    Supports either 'arrival_time' (e.g. 12:10) or 'flight_duration_min' / 'flight_duration'.
    """
    reader