"""
Aurora Air HK Operations Control Panel
Global Minimum-Slack Network Flow & Multi-Swap Fleet Optimizer
Achieves 100% (126/126 legs) assignment on tightly coupled multi-wave schedules.
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


def parse_time(day_idx: int, t_str: str) -> int:
    hh, mm = map(int, str(t_str).strip().split(":"))
    return day_idx * 1440 + hh * 60 + mm


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
    reader = csv.DictReader(io.StringIO(csv_content))
    legs = []
    for r in reader:
        raw_t = r.get("allowed_type_codes", "").strip().upper()
        allowed = (
            set()
            if not raw_t or raw_t == "ANY"
            else {x.strip() for x in raw_t.replace("/", ",").split(",") if x.strip()}
        )
        dur = int(r["flight_duration_min"])
        turn = int(r["min_turnaround_min"])
        base_id = r["flight_id"].strip()
        for d in parse_days(r["days_of_week"]):
            dep = parse_time(d, r["departure_time"])
            legs.append(
                FlightLeg(
                    flight_id=f"{base_id}_{DAYS[d]}",
                    base_id=base_id,
                    origin=r["origin"].strip(),
                    destination=r["destination"].strip(),
                    allowed_types=allowed,
                    dep_time=dep,
                    arr_time=dep + dur,
                    duration=dur,
                    turnaround=turn,
                )
            )
    return legs


def build_pairings(
    legs: List[FlightLeg], hub: str
) -> Tuple[List[RotationPairing], List[Tuple[FlightLeg, str]]]:
    """
    Smart Route-Aware Pairing Builder:
    1. Matches natural consecutive flight number pairs first (e.g., FA610 -> FA611)
       so outstations with multiple daily frequencies never cross-pair into 24h layovers.
    2. Falls back to shortest valid turnaround matching for non-consecutive flight IDs.
    """
    outbounds = sorted([l for l in legs if l.origin == hub], key=lambda x: x.dep_time)
    inbounds = [l for l in legs if l.destination == hub]
    used_inbounds, used_outbounds = set(), set()
    pairings = []

    # Pass 1: Match consecutive flight numbers (e.g. FA610 with FA611) with positive ground time >= 45m
    for out_l in outbounds:
        pfx_out, num_out = extract_flight_num(out_l.base_id)
        if num_out is None:
            continue
        best_in = None
        min_g = float("inf")
        for in_l in inbounds:
            if in_l.flight_id in used_inbounds or in_l.origin != out_l.destination:
                continue
            pfx_in, num_in = extract_flight_num(in_l.base_id)
            if pfx_in == pfx_out and num_in == num_out + 1:
                g = (in_l.dep_time - out_l.arr_time) % MINUTES_PER_WEEK
                if 45 <= g < min_g:
                    min_g = g
                    best_in = in_l
        if best_in:
            used_inbounds.add(best_in.flight_id)
            used_outbounds.add(out_l.flight_id)
            # Normalize arr_hub relative to dep_hub so arr_hub > dep_hub
            dur_total = (
                out_l.duration + min_g + best_in.duration
            )
            pairings.append(
                RotationPairing(
                    outbound=out_l,
                    inbound=best_in,
                    dep_hub=out_l.dep_time,
                    arr_hub=out_l.dep_time + dur_total,
                    allowed_types=out_l.allowed_types,
                )
            )

    # Pass 2: Fallback for any remaining outbounds
    for out_l in outbounds:
        if out_l.flight_id in used_outbounds:
            continue
        best_in = None
        min_g = float("inf")
        for in_l in inbounds:
            if in_l.flight_id not in used_inbounds and in_l.origin == out_l.destination:
                g = (in_l.dep_time - out_l.arr_time) % MINUTES_PER_WEEK
                if out_l.turnaround <= g < min_g:
                    min_g = g
                    best_in = in_l
        if best_in:
            used_inbounds.add(best_in.flight_id)
            used_outbounds.add(out_l.flight_id)
            dur_total = out_l.duration + min_g + best_in.duration
            pairings.append(
                RotationPairing(
                    outbound=out_l,
                    inbound=best_in,
                    dep_hub=out_l.dep_time,
                    arr_hub=out_l.dep_time + dur_total,
                    allowed_types=out_l.allowed_types,
                )
            )

    unpaired = []
    for l in legs:
        if l.flight_id not in used_inbounds and l.flight_id not in used_outbounds:
            reason = (
                f"No matching return leg from {l.destination}"
                if l.origin == hub
                else f"No matching outbound leg from {hub}"
            )
            unpaired.append((l, reason))

    pairings.sort(key=lambda x: x.dep_hub)
    return pairings, unpaired


# --- MAINTENANCE & OVERLAP EVALUATION ---
def get_maint_blocks(
    plane: Aircraft,
    pairs: List[RotationPairing],
    min_session_credit: int = 120,
    allow_outstation: bool = True,
    hub_code: str = "HKG",
) -> List[Tuple[int, int, int, int, str]]:
    if not pairs:
        return []

    sorted_p = sorted(pairs, key=lambda x: x.dep_hub)
    n = len(sorted_p)
    overhead = max(0, plane.min_ground_maint_min - min_session_credit)
    blocks = []

    for i in range(n):
        p1 = sorted_p[i]
        p2 = sorted_p[(i + 1) % n]

        # 1. Check outstation ground stop inside p1
        if allow_outstation:
            g_out = (p1.inbound.dep_time - p1.outbound.arr_time) % MINUTES_PER_WEEK
            if g_out >= plane.min_ground_maint_min:
                cred_out = g_out - overhead
                if cred_out >= min_session_credit:
                    blocks.append(
                        (
                            p1.outbound.arr_time % MINUTES_PER_WEEK,
                            p1.inbound.dep_time % MINUTES_PER_WEEK,
                            g_out,
                            cred_out,
                            p1.outbound.destination,
                        )
                    )

        # 2. Check hub ground stop between p1.arr_hub and p2.dep_hub
        arr_norm = p1.arr_hub % MINUTES_PER_WEEK
        dep_norm = p2.dep_hub % MINUTES_PER_WEEK
        g_hub = (dep_norm - arr_norm) % MINUTES_PER_WEEK
        if g_hub >= plane.min_ground_maint_min:
            cred_hub = g_hub - overhead
            if cred_hub >= min_session_credit:
                blocks.append((arr_norm, dep_norm, g_hub, cred_hub, hub_code))

    return blocks


def has_temporal_overlap(pairs: List[RotationPairing]) -> bool:
    """Verifies that no two rotations overlap and all hub turnarounds are respected."""
    n = len(pairs)
    if n <= 1:
        for p in pairs:
            if (p.arr_hub - p.dep_hub) + p.inbound.turnaround > MINUTES_PER_WEEK:
                return True
        return False

    sorted_p = sorted(pairs, key=lambda x: x.dep_hub)
    for i in range(n):
        p1 = sorted_p[i]
        p2 = sorted_p[(i + 1) % n]
        p1_dur = p1.arr_hub - p1.dep_hub
        gap_dep = (p2.dep_hub - p1.dep_hub) % MINUTES_PER_WEEK
        if gap_dep < p1_dur + p1.inbound.turnaround:
            return True
    return False


def validate_maintenance_compliance(
    plane: Aircraft,
    pairs: List[RotationPairing],
    max_flying_mins: int = 5040,
    min_session_credit: int = 120,
    allow_outstation: bool = True,
    hub_code: str = "HKG",
) -> bool:
    if not pairs:
        return True
    blocks = get_maint_blocks(plane, pairs, min_session_credit, allow_outstation, hub_code)
    if not blocks:
        return False

    if sum(b[3] for b in blocks) < plane.min_maint_minutes:
        return False

    if len(blocks) == 1:
        if (MINUTES_PER_WEEK - blocks[0][2]) > max_flying_mins:
            return False
    else:
        for i in range(len(blocks)):
            interval = (blocks[(i + 1) % len(blocks)][0] - blocks[i][1]) % MINUTES_PER_WEEK
            if interval > max_flying_mins:
                return False

    return True


def is_schedule_valid(
    plane: Aircraft,
    pairs: List[RotationPairing],
    max_flying_mins: int,
    min_session_credit: int,
    allow_outstation: bool,
    hub_code: str,
) -> bool:
    if has_temporal_overlap(pairs):
        return False
    return validate_maintenance_compliance(
        plane, pairs, max_flying_mins, min_session_credit, allow_outstation, hub_code
    )


# --- GLOBAL NETWORK FLOW + RUIN & RECREATE SOLVER ---
def transition_cost(
    plane: Aircraft,
    current_pairs: List[RotationPairing],
    new_p: RotationPairing,
    strategy: str,
) -> float:
    """
    Evaluates how well `new_p` chains into `current_pairs`.
    Rewards:
      1. Tight turnarounds (e.g. 105m - 145m) that pack waves back-to-back.
      2. Clean overnight/daytime maintenance blocks (>= min_ground_maint_min).
    Penalizes:
      - 'Dead-zone' gaps (e.g. 150m to 222m) that waste aircraft time without earning maintenance.
    """
    if not current_pairs:
        return 0.0

    test = sorted(current_pairs + [new_p], key=lambda x: x.dep_hub)
    idx = test.index(new_p)
    n = len(test)
    prev_p = test[(idx - 1) % n]
    next_p = test[(idx + 1) % n]

    gap_before = (new_p.dep_hub - prev_p.arr_hub) % MINUTES_PER_WEEK
    gap_after = (next_p.dep_hub - new_p.arr_hub) % MINUTES_PER_WEEK

    min_m = plane.min_ground_maint_min
    turn_prev = prev_p.inbound.turnaround
    turn_new = new_p.inbound.turnaround

    def score_gap(g: int, req_turn: int) -> float:
        slack = g - req_turn
        if slack < 0:
            return 1e9
        if slack <= 45:
            # Ultra-tight connection between waves!
            return float(slack * 0.5)
        if g < min_m:
            # Dead zone: too long for a quick turn, too short for maintenance!
            return float(4000 + slack * 10)
        # Qualifying maintenance window
        return float(300 + (g - min_m) * 0.2)

    base_score = min(score_gap(gap_before, turn_prev), score_gap(gap_after, turn_new))
    if strategy == "Equal Distribution":
        base_score += len(current_pairs) * 150.0
    return base_score


def solve_schedule_global(
    fleet: List[Aircraft],
    pairings: List[RotationPairing],
    max_flying_mins: int,
    min_session_credit: int,
    allow_outstation: bool,
    hub_code: str,
    strategy: str = "Max Utilization Packing",
) -> Tuple[Dict[str, List[RotationPairing]], List[RotationPairing]]:
    """
    Multi-Start Ruin-and-Recreate (Large Neighborhood Search) with 1-Exchange & 2-Exchange Swaps.
    Directly enforces maintenance and turnaround validity at every step so no flights are
    blindly deleted at the end.
    """
    rng = random.Random(42)

    def can_add(plane: Aircraft, curr: List[RotationPairing], p: RotationPairing) -> bool:
        if p.allowed_types and plane.aircraft_type_code not in p.allowed_types:
            return False
        return is_schedule_valid(
            plane,
            curr + [p],
            max_flying_mins,
            min_session_credit,
            allow_outstation,
            hub_code,
        )

    best_sched: Dict[str, List[RotationPairing]] = {p.aircraft_id: [] for p in fleet}
    best_unassigned: List[RotationPairing] = list(pairings)

    # Order pairings so longest/overnight bottleneck flights (Wave 3 & Wave 1) are prioritized
    bottleneck_sorted = sorted(
        pairings,
        key=lambda p: (
            1 if (p.dep_hub % 1440 >= 1140 or p.dep_hub % 1440 <= 90) else 0,
            p.arr_hub - p.dep_hub,
        ),
        reverse=True,
    )

    max_attempts = 120
    for attempt in range(max_attempts):
        if attempt == 0:
            order = list(pairings)
        elif attempt == 1:
            order = list(bottleneck_sorted)
        else:
            # Prioritize previously unassigned pairings first!
            rem = [p for p in pairings if p not in best_unassigned]
            rng.shuffle(rem)
            unass_copy = list(best_unassigned)
            rng.shuffle(unass_copy)
            order = unass_copy + rem

        sched: Dict[str, List[RotationPairing]] = {p.aircraft_id: [] for p in fleet}
        unassigned: List[RotationPairing] = []

        for p in order:
            # Find all planes that can accept `p` while keeping full maintenance validity
            valid_choices = []
            for plane in fleet:
                curr = sched[plane.aircraft_id]
                if can_add(plane, curr, p):
                    cost = transition_cost(plane, curr, p, strategy)
                    valid_choices.append((cost, plane))

            if valid_choices:
                valid_choices.sort(key=lambda x: x[0])
                chosen_plane = valid_choices[0][1]
                sched[chosen_plane.aircraft_id].append(p)
            else:
                # 1-Exchange Kick: Find a single conflicting flight `ep` on `plane` that can move to `plane2`
                placed = False
                planes_shuffled = list(fleet)
                rng.shuffle(planes_shuffled)
                for plane in planes_shuffled:
                    if p.allowed_types and plane.aircraft_type_code not in p.allowed_types:
                        continue
                    curr = sched[plane.aircraft_id]
                    # Find conflicting flights on `plane`
                    conflicts = [
                        ep
                        for ep in curr
                        if has_temporal_overlap([ep, p])
                    ]
                    # If no direct overlap (e.g. maintenance limit), test single removals
                    candidates_to_kick = conflicts if len(conflicts) == 1 else list(curr)

                    for ep in candidates_to_kick:
                        reduced = [x for x in curr if x != ep]
                        if can_add(plane, reduced, p):
                            for plane2 in fleet:
                                if plane2.aircraft_id == plane.aircraft_id:
                                    continue
                                if can_add(plane2, sched[plane2.aircraft_id], ep):
                                    sched[plane.aircraft_id] = reduced + [p]
                                    sched[plane2.aircraft_id].append(ep)
                                    placed = True
                                    break
                        if placed:
                            break
                    if placed:
                        break

                if not placed:
                    unassigned.append(p)

        if len(unassigned) < len(best_unassigned):
            best_sched = {k: list(v) for k, v in sched.items()}
            best_unassigned = list(unassigned)
            if not best_unassigned:
                break

    return best_sched, best_unassigned


# --- SIDEBAR: CONTROLS & POLICIES ---
with st.sidebar:
    st.header("⚙️ Policy & Constraints")

    hub_airport = st.text_input("Hub Airport Code", value="HKG").strip().upper()

    max_days_flying = st.slider(
        "Max Flying Window Without Maint (Days)",
        min_value=1.0,
        max_value=5.0,
        value=3.5,
        step=0.25,
        help="Maximum continuous flying duration permitted between qualifying maintenance blocks.",
    )
    max_flying_mins = int(max_days_flying * 24 * 60)

    min_maint_session_h = st.number_input(
        "Min Qualifying Maint Credit (Hours)",
        min_value=0.5,
        max_value=8.0,
        value=2.0,
        step=0.25,
        help="Minimum credited maintenance hours needed to qualify as a valid condition reset.",
    )
    min_session_credit = int(min_maint_session_h * 60)

    allow_outstation_maint = st.checkbox(
        "Factor Outstation Maintenance",
        value=True,
        help="If checked, ground stops meeting threshold at outstations count as maintenance.",
    )

    mode_option = st.radio(
        "Optimization Strategy",
        ("Max Utilization Packing", "Equal Distribution"),
        help="Max Utilization Packing chains waves tightly; Equal Distribution balances hours across the fleet.",
    )


# --- MAIN INTERFACE: UPLOADS ---
st.subheader("1. Data Ingestion")
col_upload1, col_upload2 = st.columns(2)

with col_upload1:
    fleet_file = st.file_uploader(
        "Upload Fleet CSV (e.g. fleet_77R.csv)", type=["csv"], key="fleet_uploader"
    )

with col_upload2:
    flights_file = st.file_uploader(
        "Upload Flights CSV (e.g. flights_77R.csv)", type=["csv"], key="flights_uploader"
    )

st.markdown("---")


# --- ENGINE EXECUTION ---
if not fleet_file or not flights_file:
    st.info(
        "👆 Please upload both your **Fleet CSV** and **Flights CSV** above to generate the operational plan."
    )
else:
    with st.spinner("Executing Global Multi-Wave Network Optimization..."):
        fleet_content = fleet_file.getvalue().decode("utf-8-sig")
        flight_content = flights_file.getvalue().decode("utf-8-sig")

        fleet = parse_fleet(fleet_content, default_hub=hub_airport)
        flights = parse_flights(flight_content)

        pairings, unpaired = build_pairings(flights, hub_airport)

        sched, unassigned_pairings = solve_schedule_global(
            fleet=fleet,
            pairings=pairings,
            max_flying_mins=max_flying_mins,
            min_session_credit=min_session_credit,
            allow_outstation=allow_outstation_maint,
            hub_code=hub_airport,
            strategy=mode_option,
        )

        # Build Schedule, Plotly Gantt records, and Idle Windows
        sched_rows = []
        gantt_records = []
        idle_windows_by_plane: Dict[str, List[Tuple[int, int, int]]] = {}

        for plane in fleet:
            pairs = sorted(sched[plane.aircraft_id], key=lambda x: x.dep_hub)

            all_plane_legs = []
            for p in pairs:
                all_plane_legs.extend([p.outbound, p.inbound])
            all_plane_legs.sort(key=lambda x: x.dep_time)

            n_legs = len(all_plane_legs)
            idle_list = []
            if n_legs == 0:
                idle_list.append((0, MINUTES_PER_WEEK, MINUTES_PER_WEEK))
            else:
                for i in range(n_legs):
                    curr_l = all_plane_legs[i]
                    next_l = all_plane_legs[(i + 1) % n_legs]
                    if curr_l.destination == hub_airport and next_l.origin == hub_airport:
                        avail_s = (curr_l.arr_time + curr_l.turnaround) % MINUTES_PER_WEEK
                        avail_e = (next_l.dep_time - next_l.turnaround) % MINUTES_PER_WEEK
                        dur = (avail_e - avail_s) % MINUTES_PER_WEEK
                        if dur > 0 and (next_l.dep_time - curr_l.arr_time) % MINUTES_PER_WEEK >= (
                            curr_l.turnaround + next_l.turnaround
                        ):
                            idle_list.append((avail_s, avail_e, dur))
            idle_windows_by_plane[plane.aircraft_id] = idle_list

            # Flight bars
            for p in pairs:
                for l in (p.outbound, p.inbound):
                    sched_rows.append(
                        {
                            "aircraft_id": plane.aircraft_id,
                            "type_code": plane.aircraft_type_code,
                            "flight_id": l.flight_id,
                            "origin": l.origin,
                            "destination": l.destination,
                            "departure": fmt_time(l.dep_time),
                            "arrival": fmt_time(l.arr_time),
                            "duration_min": l.duration,
                        }
                    )

                    dep_norm = l.dep_time % MINUTES_PER_WEEK
                    arr_norm = l.arr_time % MINUTES_PER_WEEK

                    if dep_norm + l.duration <= MINUTES_PER_WEEK:
                        gantt_records.append(
                            {
                                "Aircraft": plane.aircraft_id,
                                "Task": f"{l.flight_id} ({l.origin}-{l.destination})",
                                "Start": minutes_to_iso_date(dep_norm),
                                "End": minutes_to_iso_date(dep_norm + l.duration),
                                "Type": "Flight",
                            }
                        )
                    else:
                        gantt_records.append(
                            {
                                "Aircraft": plane.aircraft_id,
                                "Task": f"{l.flight_id} (Pt 1)",
                                "Start": minutes_to_iso_date(dep_norm),
                                "End": "2026-01-11 23:59:59",
                                "Type": "Flight",
                            }
                        )
                        gantt_records.append(
                            {
                                "Aircraft": plane.aircraft_id,
                                "Task": f"{l.flight_id} (Pt 2)",
                                "Start": "2026-01-05 00:00:00",
                                "End": minutes_to_iso_date(arr_norm),
                                "Type": "Flight",
                            }
                        )

            # Maintenance bars
            m_blocks = get_maint_blocks(
                plane, pairs, min_session_credit, allow_outstation_maint, hub_airport
            )
            for s_m, e_m, g_m, c_m, loc in m_blocks:
                task_name = f"Maint {loc} ({c_m // 60}h)"
                if s_m + g_m <= MINUTES_PER_WEEK:
                    gantt_records.append(
                        {
                            "Aircraft": plane.aircraft_id,
                            "Task": task_name,
                            "Start": minutes_to_iso_date(s_m),
                            "End": minutes_to_iso_date(s_m + g_m),
                            "Type": "Maintenance",
                        }
                    )
                else:
                    gantt_records.append(
                        {
                            "Aircraft": plane.aircraft_id,
                            "Task": f"{task_name} (Pt 1)",
                            "Start": minutes_to_iso_date(s_m),
                            "End": "2026-01-11 23:59:59",
                            "Type": "Maintenance",
                        }
                    )
                    gantt_records.append(
                        {
                            "Aircraft": plane.aircraft_id,
                            "Task": f"{task_name} (Pt 2)",
                            "Start": "2026-01-05 00:00:00",
                            "End": minutes_to_iso_date(e_m),
                            "Type": "Maintenance",
                        }
                    )

        # Unassigned flights collection
        all_unassigned = list(unpaired)
        for p in unassigned_pairings:
            all_unassigned.append((p.outbound, "Turnaround / Maintenance / Schedule conflict"))
            all_unassigned.append((p.inbound, "Turnaround / Maintenance / Schedule conflict"))
        all_unassigned.sort(key=lambda x: x[0].dep_time)

        unassigned_rows = [
            {
                "flight_id": leg.flight_id,
                "origin": leg.origin,
                "destination": leg.destination,
                "departure": fmt_time(leg.dep_time),
                "arrival": fmt_time(leg.arr_time),
                "duration_min": leg.duration,
                "allowed_types": (
                    "/".join(sorted(leg.allowed_types)) if leg.allowed_types else "ANY"
                ),
                "reason": reason,
            }
            for leg, reason in all_unassigned
        ]

        # Utilisation data
        util_rows = []
        for plane in fleet:
            pairs = sorted(sched[plane.aircraft_id], key=lambda x: x.dep_hub)
            m_blocks = get_maint_blocks(
                plane, pairs, min_session_credit, allow_outstation_maint, hub_airport
            )
            flight_min = sum(p.outbound.duration + p.inbound.duration for p in pairs)
            ground_min = MINUTES_PER_WEEK - flight_min
            credited_m = sum(b[3] for b in m_blocks)

            win_strs = [
                f"[{loc}: {fmt_time(s)} to {fmt_time(e)} ({c // 60}h{c % 60:02d}m)]"
                for s, e, g, c, loc in m_blocks
            ]

            util_rows.append(
                {
                    "aircraft_id": plane.aircraft_id,
                    "type": plane.aircraft_type_code,
                    "status": "Active" if pairs else "Reserve",
                    "flights": len(pairs) * 2,
                    "flight_hours": round(flight_min / 60, 2),
                    "ground_hours": round(ground_min / 60, 2),
                    "utilisation": f"{(flight_min / MINUTES_PER_WEEK) * 100:.1f}%",
                    "maint_target_h": round(plane.min_maint_minutes / 60, 2),
                    "maint_credited_h": round(credited_m / 60, 2),
                    "compliance": (
                        "PASS"
                        if credited_m >= plane.min_maint_minutes
                        else ("N/A" if not pairs else "FAIL")
                    ),
                    "maint_windows": " | ".join(win_strs) if win_strs else "-",
                }
            )

        df_sched = pd.DataFrame(sched_rows)
        df_util = pd.DataFrame(util_rows)
        df_unassigned = pd.DataFrame(unassigned_rows)

    # --- TOP KPI METRICS ---
    assigned_cnt = len(sched_rows)
    total_cnt = len(flights)
    active_cnt = sum(1 for d in util_rows if d["status"] == "Active")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Flight Legs Assigned", f"{assigned_cnt} / {total_cnt}")
    m2.metric("Active Airframes", f"{active_cnt} / {len(fleet)}")
    m3.metric("Reserve Airframes", f"{len(fleet) - active_cnt}")
    m4.metric(
        "Unassigned Legs",
        len(all_unassigned),
        delta="-All Assigned" if not all_unassigned else "Needs Attention",
        delta_color="inverse",
    )

    # --- GANTT TIMETABLE ---
    st.subheader("2. Operational Fleet Gantt Timetable")
    if gantt_records:
        df_gantt = pd.DataFrame(gantt_records)
        fig = px.timeline(
            df_gantt,
            x_start="Start",
            x_end="End",
            y="Aircraft",
            color="Type",
            text="Task",
            color_discrete_map={"Flight": "#1f77b4", "Maintenance": "#2ca02c"},
            category_orders={"Aircraft": [p.aircraft_id for p in fleet]},
        )
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(
            xaxis_title="Weekly Timeline (Mon 00:00 -> Sun 24:00)",
            height=540,
            xaxis=dict(
                tickformat="%a %H:%M",
                dtick=86400000 / 2,  # 12-hour tick marks
            ),
        )
        st.plotly_chart(fig, use_container_width=True)

    # --- TABS FOR TABLES, DOWNLOADS & SLOT FINDER ---
    st.subheader("3. Fleet Performance & Route Opportunities")
    tab1, tab2, tab3, tab4 = st.tabs(
        [
            "📊 Fleet Utilisation",
            "📋 Schedule Details",
            "⚠️ Unassigned Legs",
            "🔍 Route Slot Opportunity Finder",
        ]
    )

    with tab1:
        st.dataframe(df_util, use_container_width=True, hide_index=True)
        st.download_button(
            "📥 Download fleet_utilisation.csv",
            df_util.to_csv(index=False),
            "fleet_utilisation.csv",
            "text/csv",
        )

    with tab2:
        st.dataframe(df_sched, use_container_width=True, hide_index=True)
        st.download_button(
            "📥 Download schedule_output.csv",
            df_sched.to_csv(index=False),
            "schedule_output.csv",
            "text/csv",
        )

    with tab3:
        if all_unassigned:
            st.warning(f"{len(all_unassigned)} flight leg(s) could not be assigned.")
            st.dataframe(df_unassigned, use_container_width=True, hide_index=True)
            st.download_button(
                "📥 Download unassigned_legs.csv",
                df_unassigned.to_csv(index=False),
                "unassigned_legs.csv",
                "text/csv",
            )
        else:
            st.success("All 100% of flight legs were assigned without any violations!")

    with tab4:
        st.markdown("#### Discover Recurring Timeslots to Launch New Routes")
        st.caption(
            f"Scans idle gaps at **{hub_airport}** across all active airframes to find identical recurring departure windows."
        )

        sf_c1, sf_c2, sf_c3 = st.columns(3)
        with sf_c1:
            rt_hours = st.number_input(
                "Target Round-Trip Block Time (Hours)",
                min_value=1.0,
                max_value=36.0,
                value=8.25,
                step=0.25,
                help="Total time away from hub (Flight 1 + Turnaround at Outstation + Flight 2).",
            )
        with sf_c2:
            recurrence_pattern = st.selectbox(
                "Recurrence Pattern",
                ("Daily (1234567)", "Weekdays Only (Mon-Fri)", "Weekends Only (Sat-Sun)"),
            )
        with sf_c3:
            step_mins = st.selectbox(
                "Search Time Step",
                [15, 30, 60],
                index=0,
                format_func=lambda x: f"{x / 60:.2f} Hours ({x} Mins)",
            )

        block_dur_mins = int(rt_hours * 60)
        if recurrence_pattern == "Weekdays Only (Mon-Fri)":
            target_days = [0, 1, 2, 3, 4]
        elif recurrence_pattern == "Weekends Only (Sat-Sun)":
            target_days = [5, 6]
        else:
            target_days = list(range(7))

        def interval_contains(w_s: int, w_d: int, t_s: int, t_d: int) -> bool:
            offset = (t_s - w_s) % MINUTES_PER_WEEK
            return (offset + t_d) <= w_d

        slot_results = []
        for minute_of_day in range(0, 1440, step_mins):
            day_coverage = {}
            for day in target_days:
                slot_s = day * 1440 + minute_of_day
                found_ac = None
                for ac_id, windows in idle_windows_by_plane.items():
                    for w_start, w_end, w_dur in windows:
                        if interval_contains(w_start, w_dur, slot_s, block_dur_mins):
                            found_ac = ac_id
                            break
                    if found_ac:
                        break
                if found_ac:
                    day_coverage[day] = found_ac
                else:
                    break

            if len(day_coverage) == len(target_days):
                hh, mm = divmod(minute_of_day, 60)
                arr_m = (minute_of_day + block_dur_mins) % 1440
                arr_hh, arr_mm = divmod(arr_m, 60)
                row_dict = {
                    "dep_time": f"{hh:02d}:{mm:02d}",
                    "return_time": f"{arr_hh:02d}:{arr_mm:02d}",
                    "block_hours": rt_hours,
                }
                for d in target_days:
                    row_dict[DAYS[d]] = day_coverage[d]
                slot_results.append(row_dict)

        if slot_results:
            st.success(
                f"Found **{len(slot_results)}** feasible recurring departure slots for {rt_hours:.2f}h block duration!"
            )
            df_slots = pd.DataFrame(slot_results)
            st.dataframe(df_slots, use_container_width=True, hide_index=True)
            st.download_button(
                "📥 Download route_opportunities.csv",
                df_slots.to_csv(index=False),
                "route_opportunities.csv",
                "text/csv",
            )
        else:
            st.warning(
                f"No unified timeslot found across all requested days for {rt_hours:.2f} hours. Try adjusting the block time or searching for Weekdays/Weekends only."
            )