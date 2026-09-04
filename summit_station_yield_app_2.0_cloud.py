from pathlib import Path
import time
import hashlib
import traceback
import tempfile
import html
import urllib.parse
import json
import posixpath
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st

# Show detailed error logs in UI to make failures diagnosable on all runtimes.
try:
    st.set_option("client.showErrorDetails", "full")
except Exception:
    pass


REQUIRED_COLUMNS = [
    "BIRTH_DATE",
    "DRIVE_SBR_NUM",
    "FISCAL_WEEK",
    "HDA_CODE",
    "HGA_SUPPLIER",
    "MEDIA_TYPE",
    "OPERATION",
    "EVENT_STATUS",
    "DRIVE_SERIAL_NUM",
    "FORMAT_CAPACITY",
]
OPTIONAL_SOURCE_COLUMNS = ["PRIME", "DRV_COMP_TRK"]

STATION_ORDER = ["SCOPY", "PRE2", "CAL", "CAL2", "FNC2", "SPSC2", "CRT2", "PWT"]
DISPLAY_STATION_ORDER = [
    "SCOPY",
    "PRE2",
    "CAL",
    "CAL2",
    "FNC2",
    "SPSC2",
    "CRT2",
    "PWT",
    "APS2",
    "CUM_Yield",
]
DETAIL_STATION_ORDER = ["SCOPY", "PRE2", "CAL", "CAL2", "FNC2", "SPSC2", "CRT2", "PWT", "APS2"]
CONFIG_NAMES = [
    "RHO/RMO - BRCM HV",
    "RHO/RMO - BRCM LV",
    "RHO/RMO - eFesto LV",
    "TDK/RMO - LV",
    "TDK/RMO - HV",
    "RHO/Resonac - HV",
    "RHO/Resonac - LV",
]
LEGACY_TO_CURRENT_CONFIG_NAME = {
    "24TB HV": "RHO/RMO - BRCM HV",
    "24TB LV": "RHO/RMO - BRCM LV",
    "Efesto": "RHO/RMO - eFesto LV",
    "TDK LV": "TDK/RMO - LV",
    "TDK HV": "TDK/RMO - HV",
    "Resonac HV": "RHO/Resonac - HV",
    "Resonac LV": "RHO/Resonac - LV",
}
CURRENT_TO_LEGACY_CONFIG_NAME = {v: k for k, v in LEGACY_TO_CURRENT_CONFIG_NAME.items()}
CONFIG_FILTER_COLUMNS = ["ORG_SBR_NUM", "DRIVE_SBR_NUM", "HDA_CODE", "HGA_SUPPLIER", "MEDIA_TYPE"]
CONFIG_STATE_FILE = Path("./summit_config_state.json")
CONFIG_LOGIN_USERNAME = "309416"
CONFIG_LOGIN_PASSWORD = "Cqy001688"


def load_config_state() -> dict:
    try:
        if CONFIG_STATE_FILE.exists():
            return json.loads(CONFIG_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_config_state(config_defs: dict, config_week_defs: dict):
    try:
        payload = {
            "config_defs": config_defs,
            "config_week_defs": config_week_defs,
        }
        CONFIG_STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Non-fatal: app should keep working even if persistence fails.
        pass


def derive_org_sbr_num(df: pd.DataFrame) -> pd.Series:
    """Derive original SBR per DRIVE_SERIAL_NUM.

    Rule:
    - If SBR never changed for a serial, ORG_SBR_NUM == current DRIVE_SBR_NUM.
    - If SBR changed, ORG_SBR_NUM is the earliest known DRIVE_SBR_NUM for that serial.
    """
    # Keep only necessary columns to reduce memory/cpu overhead.
    temp = df[["DRIVE_SERIAL_NUM", "DRIVE_SBR_NUM", "BIRTH_DATE_DT"]].copy()
    temp["_SERIAL"] = temp["DRIVE_SERIAL_NUM"].astype(str).str.strip()
    temp["_SBR"] = temp["DRIVE_SBR_NUM"].astype(str).str.strip()
    temp["_ROW_IDX"] = range(len(temp))

    # Earliest record per serial: prioritize BIRTH_DATE_DT then original row order.
    temp = temp.sort_values(["_SERIAL", "BIRTH_DATE_DT", "_ROW_IDX"], na_position="last")

    first_sbr_map = (
        temp[temp["_SBR"] != ""]
        .drop_duplicates(subset=["_SERIAL"], keep="first")
        .set_index("_SERIAL")["_SBR"]
    )

    serial_now = df["DRIVE_SERIAL_NUM"].astype(str).str.strip()
    sbr_now = df["DRIVE_SBR_NUM"].astype(str).str.strip()
    return serial_now.map(first_sbr_map).fillna(sbr_now)


def _week_start_saturday(dt: pd.Timestamp) -> pd.Timestamp:
    return dt.normalize() - pd.Timedelta(days=(dt.weekday() - 5) % 7)


def to_birth_week(dt: pd.Timestamp) -> str:
    """
    Custom mapping based on user-defined calendar:
    - 2026/06/06~2026/06/12 => WW502026
    - 2026/07/04~2026/07/10 => WW012027 (new fiscal year)
    """
    if pd.isna(dt):
        return ""

    ws = _week_start_saturday(pd.Timestamp(dt))

    anchor_2026_w50 = pd.Timestamp("2026-06-06")
    anchor_2027_w01 = pd.Timestamp("2026-07-04")

    if ws >= anchor_2027_w01:
        week_num = int((ws - anchor_2027_w01).days // 7) + 1
        return f"WW{week_num:02d}2027"

    week_num = 50 + int((ws - anchor_2026_w50).days // 7)
    return f"WW{week_num:02d}2026"


def to_fiscal_year(dt: pd.Timestamp) -> int:
    """
    Fiscal year start rule aligned with the given example:
    FY(Y+1) starts at the first Saturday on/after July 1 of year Y.
    """
    if pd.isna(dt):
        return -1

    d = pd.Timestamp(dt).normalize()
    july1 = pd.Timestamp(year=d.year, month=7, day=1)
    # Monday=0 ... Saturday=5
    shift = (5 - july1.weekday()) % 7
    fy_start_next = july1 + pd.Timedelta(days=shift)
    return d.year + 1 if d >= fy_start_next else d.year


def _to_int_week(v) -> int:
    s = str(v).strip()
    try:
        return int(s)
    except ValueError:
        return -1


def fiscal_week_order(w: int) -> int:
    # user-defined fiscal sequence: 49..53, then 1..48
    return w if w >= 49 else w + 53


def build_fiscal_week_labels(df: pd.DataFrame) -> list[str]:
    temp = df.copy()
    temp = temp[temp["FISCAL_YEAR_DERIVED"] > 0]
    temp["FISCAL_WEEK_INT"] = temp["FISCAL_WEEK"].map(_to_int_week)
    temp = temp[temp["FISCAL_WEEK_INT"] > 0]

    uniq = (
        temp[["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_INT"]]
        .drop_duplicates()
        .assign(FISCAL_WEEK_ORDER=lambda x: x["FISCAL_WEEK_INT"].map(fiscal_week_order))
        .sort_values(["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_ORDER"])
    )

    return [
        f"FY{int(r.FISCAL_YEAR_DERIVED)}-WW{int(r.FISCAL_WEEK_INT):02d}"
        for r in uniq.itertuples(index=False)
    ]


def calc_prefix_fiscal_week_cum_yields(
    df: pd.DataFrame,
    prefixes=(5, 6, 8),
    aps2_yield: float = 0.98,
    start_week_label: str | None = None,
) -> pd.DataFrame:
    temp = df.copy()
    temp = temp[temp["FISCAL_YEAR_DERIVED"] > 0]
    temp["FISCAL_WEEK_INT"] = temp["FISCAL_WEEK"].map(_to_int_week)
    temp = temp[temp["FISCAL_WEEK_INT"] > 0]

    temp["FISCAL_WEEK_DERIVED"] = temp["FISCAL_WEEK_INT"]
    temp["FISCAL_WEEK_ORDER"] = temp["FISCAL_WEEK_DERIVED"].map(fiscal_week_order)

    uniq = (
        temp[["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_DERIVED", "FISCAL_WEEK_ORDER"]]
        .drop_duplicates()
        .sort_values(["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_ORDER"])
        .reset_index(drop=True)
    )
    uniq["WEEK_LABEL"] = uniq.apply(
        lambda r: f"FY{int(r['FISCAL_YEAR_DERIVED'])}-WW{int(r['FISCAL_WEEK_DERIVED']):02d}",
        axis=1,
    )

    if start_week_label and start_week_label in set(uniq["WEEK_LABEL"].tolist()):
        start_idx = int(uniq.index[uniq["WEEK_LABEL"] == start_week_label][0])
        uniq = uniq.iloc[start_idx:].reset_index(drop=True)

    rows = []
    for n in prefixes:
        sel = uniq.head(n)
        if sel.empty:
            rows.append({"Prefix": f"First {n} fiscal weeks", "Fiscal_Weeks_Included": "", "CUM_Yield": 0.0})
            continue

        joined = temp.merge(
            sel[["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_DERIVED"]],
            on=["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_DERIVED"],
            how="inner",
        )
        _, cum_val = calc_station_yield(joined, aps2_yield=aps2_yield)

        labels = [
            f"FY{int(r.FISCAL_YEAR_DERIVED)}-WW{int(r.FISCAL_WEEK_DERIVED):02d}"
            for r in sel.itertuples(index=False)
        ]
        rows.append(
            {
                "Prefix": f"First {n} fiscal weeks",
                "Fiscal_Weeks_Included": ", ".join(labels),
                "CUM_Yield": cum_val,
            }
        )

    return pd.DataFrame(rows)


def ordered_fiscal_weeks(df: pd.DataFrame, start_week_label: str | None = None) -> pd.DataFrame:
    temp = df.copy()
    temp = temp[temp["FISCAL_YEAR_DERIVED"] > 0]
    temp["FISCAL_WEEK_INT"] = temp["FISCAL_WEEK"].map(_to_int_week)
    temp = temp[temp["FISCAL_WEEK_INT"] > 0]

    uniq = (
        temp[["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_INT"]]
        .drop_duplicates()
        .rename(columns={"FISCAL_WEEK_INT": "FISCAL_WEEK_DERIVED"})
        .assign(FISCAL_WEEK_ORDER=lambda x: x["FISCAL_WEEK_DERIVED"].map(fiscal_week_order))
        .sort_values(["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_ORDER"])
        .reset_index(drop=True)
    )
    uniq["WEEK_LABEL"] = uniq.apply(
        lambda r: f"FY{int(r['FISCAL_YEAR_DERIVED'])}-WW{int(r['FISCAL_WEEK_DERIVED']):02d}",
        axis=1,
    )

    if start_week_label and start_week_label in set(uniq["WEEK_LABEL"].tolist()):
        start_idx = int(uniq.index[uniq["WEEK_LABEL"] == start_week_label][0])
        uniq = uniq.iloc[start_idx:].reset_index(drop=True)

    return uniq


def build_sr_drive_sn_export(
    df: pd.DataFrame,
    first_n: int,
    sr1_n: int,
    sr3_n: int,
    start_week_label: str | None = None,
) -> dict[str, pd.DataFrame]:
    uniq = ordered_fiscal_weeks(df, start_week_label=start_week_label)

    sr1_weeks = uniq.iloc[first_n:sr1_n][["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_DERIVED"]]
    sr3_weeks = uniq.iloc[sr1_n:sr3_n][["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_DERIVED"]]

    temp = df.copy()
    temp["FISCAL_WEEK_DERIVED"] = temp["FISCAL_WEEK"].map(_to_int_week)

    def _collect(weeks_df: pd.DataFrame, tag: str) -> pd.DataFrame:
        if weeks_df.empty:
            return pd.DataFrame(columns=["SR_Type"] + df.columns.tolist())
        joined = temp.merge(
            weeks_df,
            on=["FISCAL_YEAR_DERIVED", "FISCAL_WEEK_DERIVED"],
            how="inner",
        )
        out = joined[joined["EVENT_STATUS"].isin(["P", "F"])].copy()
        out = out[out["DRIVE_SERIAL_NUM"].astype(str).str.strip() != ""]
        if "FISCAL_WEEK_DERIVED" in out.columns:
            out = out.drop(columns=["FISCAL_WEEK_DERIVED"])

        # Keep all available RAW DATA information columns in export.
        keep_cols = [c for c in df.columns if c in out.columns]
        out = out[keep_cols].drop_duplicates()
        sort_cols = [c for c in ["EVENT_STATUS", "DRIVE_SERIAL_NUM", "FISCAL_WEEK", "BIRTH_WEEK"] if c in out.columns]
        if sort_cols:
            out = out.sort_values(sort_cols)
        out = out.reset_index(drop=True)

        out.insert(0, "SR_Type", tag)
        return out

    return {
        "SR_1week": _collect(sr1_weeks, "SR_1week"),
        "SR_3week": _collect(sr3_weeks, "SR_3week"),
    }


def compute_incomplete_missing_by_stage(
    df: pd.DataFrame, pwt_pass_sn_exclude: set[str] | None = None
) -> pd.DataFrame:
    """Find incomplete DRIVE_SERIAL_NUM by stage: upstream PASS - downstream TOTAL,
    excluding drives that already have PWT PASS.
    """
    if pwt_pass_sn_exclude is None:
        pwt_pass_sn = set(
            df.loc[
                (df["OPERATION"] == "PWT") & (df["EVENT_STATUS"] == "P"),
                "DRIVE_SERIAL_NUM",
            ]
            .dropna()
            .astype(str)
            .str.strip()
        )
    else:
        pwt_pass_sn = {str(v).strip() for v in pwt_pass_sn_exclude if str(v).strip()}

    chain_ops = STATION_ORDER
    missing_rows = []

    for i in range(1, len(chain_ops)):
        prev_op = chain_ops[i - 1]
        curr_op = chain_ops[i]

        prev_df = df[df["OPERATION"] == prev_op]
        curr_df = df[df["OPERATION"] == curr_op]

        prev_pass_sn = set(
            prev_df.loc[prev_df["EVENT_STATUS"] == "P", "DRIVE_SERIAL_NUM"]
            .dropna()
            .astype(str)
            .str.strip()
        )
        curr_pass_sn = set(
            curr_df.loc[curr_df["EVENT_STATUS"] == "P", "DRIVE_SERIAL_NUM"]
            .dropna()
            .astype(str)
            .str.strip()
        )
        curr_fail_sn = set(
            curr_df.loc[curr_df["EVENT_STATUS"] == "F", "DRIVE_SERIAL_NUM"]
            .dropna()
            .astype(str)
            .str.strip()
        )
        curr_total_sn = curr_pass_sn.union(curr_fail_sn)

        missing_sn = sorted(
            [sn for sn in (prev_pass_sn - curr_total_sn - pwt_pass_sn) if sn]
        )
        if not missing_sn:
            continue

        miss_df = pd.DataFrame({"DRIVE_SERIAL_NUM": missing_sn})
        miss_df["Incomplete_Stage"] = f"{prev_op}->{curr_op}"
        miss_df["Incomplete_Stage_Order"] = i
        missing_rows.append(miss_df)

    if not missing_rows:
        return pd.DataFrame(columns=["Incomplete_Stage", "DRIVE_SERIAL_NUM"])

    out = pd.concat(missing_rows, ignore_index=True).drop_duplicates()
    # If one DRIVE appears in multiple incomplete stages, keep only the last stage.
    out = (
        out.sort_values(["DRIVE_SERIAL_NUM", "Incomplete_Stage_Order"])
        .drop_duplicates(subset=["DRIVE_SERIAL_NUM"], keep="last")
        .drop(columns=["Incomplete_Stage_Order"])
        .reset_index(drop=True)
    )
    return out


def build_incomplete_drive_sn_export(
    df: pd.DataFrame, pwt_pass_sn_exclude: set[str] | None = None
) -> pd.DataFrame:
    """Export incomplete DRIVE_SERIAL_NUM details with full RAW DATA columns."""
    missing_all = compute_incomplete_missing_by_stage(df, pwt_pass_sn_exclude=pwt_pass_sn_exclude)
    if missing_all.empty:
        return pd.DataFrame(columns=["Incomplete_Stage"] + df.columns.tolist())

    out = missing_all.merge(df, on="DRIVE_SERIAL_NUM", how="left")

    keep_cols = ["Incomplete_Stage"] + [c for c in df.columns if c in out.columns]
    out = out[keep_cols].drop_duplicates()

    sort_cols = [
        c
        for c in ["Incomplete_Stage", "DRIVE_SERIAL_NUM", "EVENT_STATUS", "FISCAL_WEEK", "BIRTH_WEEK"]
        if c in out.columns
    ]
    if sort_cols:
        out = out.sort_values(sort_cols)

    # Final dedupe for export file: keep one row per DRIVE_SERIAL_NUM.
    if "DRIVE_SERIAL_NUM" in out.columns:
        out = out.drop_duplicates(subset=["DRIVE_SERIAL_NUM"], keep="last")

    return out.reset_index(drop=True)


def make_export_action_link(text: str, birth_week_raw: str, export_kind: str) -> str:
    safe_text = html.escape(text)
    q = urllib.parse.urlencode({"export_bw": birth_week_raw, "export_kind": export_kind})
    return f'<a href="?{q}" style="text-decoration:underline;">{safe_text}</a>'


def get_query_params_compat() -> dict[str, list[str]]:
    # Compatibility for different Streamlit query params APIs.
    try:
        qp = st.query_params
        out: dict[str, list[str]] = {}
        for k in qp.keys():
            values: list[str] = []
            try:
                # Newer API may support get_all
                got = qp.get_all(k)
                if isinstance(got, list):
                    values = [str(v) for v in got]
                elif got is not None:
                    values = [str(got)]
            except Exception:
                # Fallback: single value via get
                try:
                    got = qp.get(k)
                    if isinstance(got, list):
                        values = [str(v) for v in got]
                    elif got is not None:
                        values = [str(got)]
                except Exception:
                    values = []
            out[str(k)] = values
        if out:
            return out
    except Exception:
        pass

    try:
        old = st.experimental_get_query_params()
        return {str(k): [str(vv) for vv in v] for k, v in old.items()}
    except Exception:
        return {}


def is_localhost_session() -> bool:
    """Allow sensitive settings edits only for localhost access."""
    try:
        headers = st.context.headers
        host = str(headers.get("host", "")).lower()
        forwarded_host = str(headers.get("x-forwarded-host", "")).lower()
        candidate = host or forwarded_host
        return (
            candidate.startswith("localhost")
            or candidate.startswith("127.0.0.1")
            or candidate.startswith("[::1]")
        )
    except Exception:
        return False


def _open_config_login_dialog_if_needed() -> bool:
    """Return True when config editing is authenticated for this browser session."""
    if "config_auth_ok" not in st.session_state:
        st.session_state["config_auth_ok"] = False

    if st.session_state.get("config_auth_ok", False):
        return True

    # Prefer modal dialog when available; fallback to inline form on older Streamlit.
    if hasattr(st, "dialog"):
        @st.dialog("Config Login Required")
        def _config_login_dialog():
            st.caption("Enter username and password to unlock Config Settings.")
            with st.form("config_login_form_dialog", clear_on_submit=False):
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button("Unlock")

            if submitted:
                if username == CONFIG_LOGIN_USERNAME and password == CONFIG_LOGIN_PASSWORD:
                    st.session_state["config_auth_ok"] = True
                    st.success("Login successful. Config Settings unlocked.")
                    st.rerun()
                else:
                    st.error("Incorrect username or password.")

        _config_login_dialog()
    return bool(st.session_state.get("config_auth_ok", False))


def birth_week_sort_key(w: str):
    t = str(w).strip().upper()
    try:
        return (int(t[4:]), int(t[2:4]))
    except Exception:
        return (9999, 999)


def display_birth_week_label(w: str) -> str:
    t = str(w).strip().upper()
    if len(t) == 8 and t.startswith("WW") and t[2:4].isdigit() and t[4:8].isdigit():
        return f"WW{t[2:4]} {t[4:8]}"
    return str(w)


def parse_manual_filter_values(text: str) -> list[str]:
    if not str(text).strip():
        return []
    normalized = (
        str(text)
        .replace("\n", ",")
        .replace(";", ",")
        .replace("，", ",")
    )
    return [v.strip() for v in normalized.split(",") if v.strip()]


def make_fiscal_week_display(fy: int, fw: int) -> str:
    if int(fy) > 0 and int(fw) > 0:
        return f"WW{int(fw):02d} {int(fy)}"
    return ""


def fiscal_week_display_sort_key(v: str):
    s = str(v).strip().upper()
    # expected format: WW01 2027
    try:
        ww = int(s[2:4])
        yy = int(s[-4:])
        return (yy, fiscal_week_order(ww))
    except Exception:
        return (9999, 999)


def birth_week_to_fiscal_week_label(bw: str) -> str | None:
    """Convert BIRTH_WEEK style WWxxYYYY -> fiscal label FYyyyy-WWxx."""
    t = str(bw).strip().upper()
    if len(t) == 8 and t.startswith("WW") and t[2:4].isdigit() and t[4:8].isdigit():
        return f"FY{t[4:8]}-WW{t[2:4]}"
    return None


def calc_birth_week_metrics(
    df: pd.DataFrame,
    aps2_yield: float = 0.98,
    prefix_weeks: tuple[int, int, int] = (4, 5, 7),
    start_week_label: str | None = None,
    pwt_pass_sn_exclude: set[str] | None = None,
) -> pd.DataFrame:
    output_cols = [
        "BIRTH_WEEK",
        "loading_QTY",
        "First_yield",
        "SR_1week_yield",
        "SR_3week_yield",
        "SCOPY_Yield",
        "PRE2_Yield",
        "CAL_Yield",
        "CAL2_Yield",
        "FNC2_Yield",
        "SPSC2_Yield",
        "CRT2_Yield",
        "PWT_Yield",
        "APS2_Yield",
        "CUM_Yield",
        "Incomplte%",
    ]

    first_n, sr1_n, sr3_n = prefix_weeks
    birth_weeks = sorted(
        [v for v in df["BIRTH_WEEK"].dropna().unique().tolist() if str(v).strip()],
        key=birth_week_sort_key,
    )

    rows = []
    for i, bw in enumerate(birth_weeks):
        # Calculate each BIRTH_WEEK row using this BIRTH_WEEK only,
        # so results remain consistent whether user selects one or many BIRTH_WEEK values.
        sub = df[df["BIRTH_WEEK"] == bw]
        station_df, cum_val = calc_station_yield(sub, aps2_yield=aps2_yield)

        # First_yield counting start aligns to this BIRTH_WEEK by default.
        row_start_week_label = birth_week_to_fiscal_week_label(bw) or start_week_label

        prefix_df = calc_prefix_fiscal_week_cum_yields(
            sub,
            prefixes=(first_n, sr1_n, sr3_n),
            aps2_yield=aps2_yield,
            start_week_label=row_start_week_label,
        )
        prefix_map = {r["Prefix"]: float(r["CUM_Yield"]) for _, r in prefix_df.iterrows()}

        first_yield = prefix_map.get(f"First {first_n} fiscal weeks", 0.0)
        first_sr1 = prefix_map.get(f"First {sr1_n} fiscal weeks", 0.0)
        first_sr3 = prefix_map.get(f"First {sr3_n} fiscal weeks", 0.0)

        station_yield_map = {
            str(r["OPERATION"]): float(r["Yield"]) for _, r in station_df.iterrows()
        }
        station_pass_map = {
            str(r["OPERATION"]): (0 if pd.isna(r["P_SN_Count"]) else int(r["P_SN_Count"]))
            for _, r in station_df.iterrows()
        }
        station_total_map = {
            str(r["OPERATION"]): (
                0 if pd.isna(r["Total_SN_For_Yield"]) else int(r["Total_SN_For_Yield"])
            )
            for _, r in station_df.iterrows()
        }

        # Incomplte QTY stage logic:
        # upstream PASS - downstream TOTAL, and exclude drives with PWT PASS.
        incomplete_detail = compute_incomplete_missing_by_stage(
            sub, pwt_pass_sn_exclude=pwt_pass_sn_exclude
        )
        incomplete_qty_sum = len(incomplete_detail)

        scopy_total = station_total_map.get("SCOPY", 0)
        incomplete_pct = (incomplete_qty_sum / scopy_total) if scopy_total > 0 else 0.0

        rows.append(
            {
                "BIRTH_WEEK": bw,
                "loading_QTY": scopy_total,
                "First_yield": first_yield,
                "SR_1week_yield": first_sr1 - first_yield,
                "SR_3week_yield": first_sr3 - first_sr1,
                "SCOPY_Yield": station_yield_map.get("SCOPY", 0.0),
                "PRE2_Yield": station_yield_map.get("PRE2", 0.0),
                "CAL_Yield": station_yield_map.get("CAL", 0.0),
                "CAL2_Yield": station_yield_map.get("CAL2", 0.0),
                "FNC2_Yield": station_yield_map.get("FNC2", 0.0),
                "SPSC2_Yield": station_yield_map.get("SPSC2", 0.0),
                "CRT2_Yield": station_yield_map.get("CRT2", 0.0),
                "PWT_Yield": station_yield_map.get("PWT", 0.0),
                "APS2_Yield": station_yield_map.get("APS2", aps2_yield),
                "CUM_Yield": cum_val,
                "Incomplte%": incomplete_pct,
            }
        )

    if not rows:
        return pd.DataFrame(columns=output_cols)
    return pd.DataFrame(rows, columns=output_cols)


@st.cache_data(show_spinner=False)
def load_raw_data(path: str) -> pd.DataFrame:
    p = Path(path)
    use_cols = set(REQUIRED_COLUMNS + OPTIONAL_SOURCE_COLUMNS)

    if p.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        xl = pd.ExcelFile(path)
        parts = []
        for s in xl.sheet_names:
            one = pd.read_excel(path, sheet_name=s, usecols=lambda c: str(c).strip() in use_cols)
            if one is not None and not one.empty:
                parts.append(one)
        if not parts:
            raise ValueError("No non-empty sheets found in Excel file")
        df = pd.concat(parts, ignore_index=True)
    else:
        df = pd.read_csv(path, low_memory=False, usecols=lambda c: str(c).strip() in use_cols)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    out = df.copy()
    out["BIRTH_DATE_DT"] = pd.to_datetime(out["BIRTH_DATE"], errors="coerce")
    out["BIRTH_WEEK"] = out["BIRTH_DATE_DT"].map(to_birth_week)
    out["FISCAL_WEEK_INT"] = pd.to_numeric(out["FISCAL_WEEK"], errors="coerce").fillna(-1).astype(int)
    # Fiscal year is defined by the custom BIRTH_WEEK year suffix (WWxxYYYY)
    out["FISCAL_YEAR_DERIVED"] = pd.to_numeric(
        out["BIRTH_WEEK"].astype(str).str[-4:], errors="coerce"
    ).fillna(-1).astype(int)
    # Vectorized fiscal week display: WWxx YYYY
    out["FISCAL_WEEK_DISPLAY"] = ""
    valid_fw = (out["FISCAL_YEAR_DERIVED"] > 0) & (out["FISCAL_WEEK_INT"] > 0)
    out.loc[valid_fw, "FISCAL_WEEK_DISPLAY"] = (
        "WW"
        + out.loc[valid_fw, "FISCAL_WEEK_INT"].astype(int).astype(str).str.zfill(2)
        + " "
        + out.loc[valid_fw, "FISCAL_YEAR_DERIVED"].astype(int).astype(str)
    )

    # Derive PRIME with compatibility for new/old raw formats:
    # - preferred: DRV_COMP_TRK (new format)
    # - fallback: PRIME (old format)
    if "DRV_COMP_TRK" in out.columns:
        trk = out["DRV_COMP_TRK"].astype("string").str.strip().str.upper()
        is_prime = trk.str.fullmatch(r"P+").fillna(False)
        out["PRIME"] = is_prime.map({True: "Y", False: "N"}).astype(str)
    elif "PRIME" in out.columns:
        out["PRIME"] = out["PRIME"].astype("string").str.strip().str.upper().fillna("N")
        out["DRV_COMP_TRK"] = ""
    else:
        raise ValueError("Missing required source for PRIME derivation: need DRV_COMP_TRK or PRIME")

    for c in [
        "DRIVE_SBR_NUM",
        "DRV_COMP_TRK",
        "BIRTH_WEEK",
        "FISCAL_WEEK",
        "FISCAL_WEEK_DISPLAY",
        "PRIME",
        "HDA_CODE",
        "OPERATION",
        "EVENT_STATUS",
        "DRIVE_SERIAL_NUM",
        "FORMAT_CAPACITY",
    ]:
        out[c] = out[c].astype(str).str.strip()
    return out


def _load_raw_data_many_impl(paths: list[str]) -> pd.DataFrame:
    if not paths:
        return pd.DataFrame()

    # Persist merged dataframe to local cache file by file-signature.
    try:
        sig_src = []
        for p in paths:
            fp = Path(p)
            stt = fp.stat()
            sig_src.append(f"{fp.resolve()}|{stt.st_size}|{stt.st_mtime_ns}")
        sig = hashlib.md5("\n".join(sig_src).encode("utf-8")).hexdigest()[:16]
        cache_dir = Path(tempfile.gettempdir()) / "summit_station_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"merged_{sig}.pkl"
        if cache_file.exists():
            return pd.read_pickle(cache_file)
    except Exception:
        cache_file = None

    # Parallel load for multiple files (I/O-bound).
    workers = min(4, max(1, len(paths)))
    if len(paths) > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            parts = list(ex.map(load_raw_data, paths))
    else:
        parts = [load_raw_data(paths[0])]

    if not parts:
        return pd.DataFrame()

    out = pd.concat(parts, ignore_index=True)
    # Derive ORG_SBR_NUM once on merged data (faster + correct across multi-file uploads).
    if {"DRIVE_SBR_NUM", "DRIVE_SERIAL_NUM"}.issubset(out.columns):
        if "BIRTH_DATE_DT" not in out.columns and "BIRTH_DATE" in out.columns:
            out["BIRTH_DATE_DT"] = pd.to_datetime(out["BIRTH_DATE"], errors="coerce")
        out["ORG_SBR_NUM"] = derive_org_sbr_num(out)
    else:
        out["ORG_SBR_NUM"] = ""

    out["ORG_SBR_NUM"] = out["ORG_SBR_NUM"].astype(str).str.strip()

    try:
        if cache_file is not None:
            out.to_pickle(cache_file)
    except Exception:
        pass

    return out


@st.cache_data(show_spinner=False)
def _load_raw_data_many_cached(paths: list[str]) -> pd.DataFrame:
    return _load_raw_data_many_impl(paths)


def load_raw_data_many(paths: list[str]) -> pd.DataFrame:
    """Memory-safe wrapper for Streamlit cache failures on large datasets."""
    try:
        return _load_raw_data_many_cached(paths)
    except MemoryError:
        # Cache entry can fail to unpickle on low-memory sessions.
        try:
            st.cache_data.clear()
        except Exception:
            pass
        return _load_raw_data_many_impl(paths)


def apply_filters(
    df: pd.DataFrame,
    birth_weeks,
    config_filter,
    fiscal_weeks,
    prime_values,
    capacity_values,
) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)

    if birth_weeks:
        mask &= df["BIRTH_WEEK"].isin(birth_weeks)
    if config_filter:
        for col in CONFIG_FILTER_COLUMNS:
            vals = config_filter.get(col, [])
            if vals:
                mask &= df[col].isin(vals)
    if fiscal_weeks:
        if "FISCAL_WEEK_DISPLAY" in df.columns:
            fiscal_set = {str(v).strip() for v in fiscal_weeks}
            mask &= (
                df["FISCAL_WEEK_DISPLAY"].astype(str).isin(fiscal_set)
                | df["FISCAL_WEEK"].astype(str).isin(fiscal_set)
            )
        else:
            mask &= df["FISCAL_WEEK"].isin(fiscal_weeks)
    if prime_values:
        mask &= df["PRIME"].isin(prime_values)
    if capacity_values:
        mask &= df["FORMAT_CAPACITY"].isin(capacity_values)

    out = df.loc[mask].copy()
    return out


def calc_station_yield(df: pd.DataFrame, aps2_yield: float = 0.98) -> tuple[pd.DataFrame, float]:
    rows = []
    cum_yield = 1.0
    prev_pass_count = None
    pwt_pass_count = 0

    for op in STATION_ORDER:
        d = df[df["OPERATION"] == op]
        pass_sn = set(d.loc[d["EVENT_STATUS"] == "P", "DRIVE_SERIAL_NUM"].dropna())
        fail_sn = set(d.loc[d["EVENT_STATUS"] == "F", "DRIVE_SERIAL_NUM"].dropna())

        fail_effective = fail_sn - pass_sn
        p_count = len(pass_sn)
        f_raw = len(fail_sn)
        f_eff = len(fail_effective)
        total = p_count + f_eff

        if op == "SCOPY":
            # SCOPY keeps original definition
            y = (p_count / total) if total > 0 else 0.0
        else:
            # downstream station yield = current pass / previous station pass
            y = (p_count / prev_pass_count) if (prev_pass_count and prev_pass_count > 0) else 0.0

        cum_yield *= y
        prev_pass_count = p_count
        if op == "PWT":
            pwt_pass_count = p_count

        rows.append(
            {
                "OPERATION": op,
                "P_SN_Count": p_count,
                "F_SN_Count_Raw": f_raw,
                "F_SN_Count_Effective": f_eff,
                "Total_SN_For_Yield": total,
                "Yield": y,
            }
        )

    # APS2: if APS2 exists in raw data, use APS2 P / (APS2 P + APS2 effective F).
    # Otherwise fallback to configured value.
    aps2_df = df[df["OPERATION"].isin(["APS2", "Adaptive Proactive System 2"])].copy()
    if not aps2_df.empty:
        aps2_pass_sn = set(
            aps2_df.loc[aps2_df["EVENT_STATUS"] == "P", "DRIVE_SERIAL_NUM"].dropna()
        )
        aps2_fail_sn = set(
            aps2_df.loc[aps2_df["EVENT_STATUS"] == "F", "DRIVE_SERIAL_NUM"].dropna()
        )
        aps2_fail_effective = aps2_fail_sn - aps2_pass_sn
        aps2_p_count = len(aps2_pass_sn)
        aps2_f_raw = len(aps2_fail_sn)
        aps2_f_eff = len(aps2_fail_effective)
        aps2_total = aps2_p_count + aps2_f_eff
        aps2_y = (aps2_p_count / aps2_total) if aps2_total > 0 else 0.0
    else:
        aps2_y = max(0.0, min(float(aps2_yield), 1.0))
        aps2_p_count = None
        aps2_f_raw = None
        aps2_f_eff = None
        aps2_total = None

    cum_yield *= aps2_y
    rows.append(
        {
            "OPERATION": "APS2",
            "P_SN_Count": aps2_p_count,
            "F_SN_Count_Raw": aps2_f_raw,
            "F_SN_Count_Effective": aps2_f_eff,
            "Total_SN_For_Yield": aps2_total,
            "Yield": aps2_y,
        }
    )

    return pd.DataFrame(rows), cum_yield


def calc_station_yield_detail_by_birth_week(
    df: pd.DataFrame, aps2_yield: float = 0.98
) -> pd.DataFrame:
    birth_weeks = sorted(
        [v for v in df["BIRTH_WEEK"].dropna().unique().tolist() if str(v).strip()],
        key=birth_week_sort_key,
    )

    detail_parts = []
    for bw in birth_weeks:
        sub = df[df["BIRTH_WEEK"] == bw]
        one_df, _ = calc_station_yield(sub, aps2_yield=aps2_yield)
        one_df = one_df.copy()
        one_df.insert(0, "BIRTH_WEEK", bw)
        detail_parts.append(one_df)

    if not detail_parts:
        return pd.DataFrame(
            columns=[
                "BIRTH_WEEK",
                "OPERATION",
                "P_SN_Count",
                "F_SN_Count_Raw",
                "F_SN_Count_Effective",
                "Total_SN_For_Yield",
                "Yield",
            ]
        )

    return pd.concat(detail_parts, ignore_index=True)


def default_raw_path() -> str:
    # Prefer OneDrive raw-data folder with periodic CSV drops.
    one_drive_dir = Path("C:/Users/309416/OneDrive - Seagate Technology/Summit/Summit Yield Raw data")
    if one_drive_dir.exists() and one_drive_dir.is_dir():
        csv_files = sorted(one_drive_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if csv_files:
            return str(csv_files[0])

    # Legacy fallback path
    p = Path("C:/Users/309416/Qiuyue/RAW DATA/SUMMIT_YIELD_RAW_DATA_Update_0806ww50_update2.xlsx")
    return str(p) if p.exists() else ""


def default_raw_paths() -> list[str]:
    """Return all default raw paths (prefer OneDrive CSV set)."""
    one_drive_dir = Path("C:/Users/309416/OneDrive - Seagate Technology/Summit/Summit Yield Raw data")
    if one_drive_dir.exists() and one_drive_dir.is_dir():
        csv_files = sorted(one_drive_dir.glob("*.csv"), key=lambda p: p.name)
        if csv_files:
            return [str(p) for p in csv_files]

    p = default_raw_path()
    return [p] if p else []


def _get_onedrive_urls_from_secrets() -> list[str]:
    """Read OneDrive file URL(s) from Streamlit secrets for cloud runtime."""
    urls: list[str] = []

    try:
        secret_group = st.secrets.get("onedrive", {})
    except Exception:
        secret_group = {}

    # Preferred format:
    # [onedrive]
    # urls = ["https://...file1.csv", "https://...file2.xlsx"]
    raw_urls = secret_group.get("urls") if isinstance(secret_group, Mapping) else None
    if isinstance(raw_urls, list):
        urls.extend([str(u).strip() for u in raw_urls if str(u).strip()])
    elif isinstance(raw_urls, str):
        urls.extend([u.strip() for u in raw_urls.split(",") if u.strip()])

    # Backward-compatible single URL keys.
    for key in ["url", "ONEDRIVE_URL", "ONEDRIVE_FILE_URL"]:
        value = None
        if isinstance(secret_group, Mapping):
            value = secret_group.get(key)
        if not value:
            try:
                value = st.secrets.get(key)
            except Exception:
                value = None
        if value:
            urls.append(str(value).strip())

    # Deduplicate while preserving order.
    uniq = []
    seen = set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _get_msgraph_config_from_secrets() -> dict:
    """Read Microsoft Graph config from Streamlit secrets."""
    try:
        cfg = st.secrets.get("msgraph", {})
    except Exception:
        return {}

    if not isinstance(cfg, Mapping):
        return {}

    return {
        "tenant_id": str(cfg.get("tenant_id", "")).strip(),
        "client_id": str(cfg.get("client_id", "")).strip(),
        "client_secret": str(cfg.get("client_secret", "")).strip(),
        "site_hostname": str(cfg.get("site_hostname", "")).strip(),
        "site_path": str(cfg.get("site_path", "")).strip(),
        "drive_folder_path": str(cfg.get("drive_folder_path", "")).strip(),
        "file_extensions": [
            str(x).strip().lower() for x in cfg.get("file_extensions", [".csv", ".xlsx", ".xlsm", ".xls"]) if str(x).strip()
        ],
        "file_name_contains": str(cfg.get("file_name_contains", "")).strip().lower(),
        "max_files": int(cfg.get("max_files", 20)),
    }


def _is_msgraph_config_valid(cfg: dict) -> bool:
    required = [
        "tenant_id",
        "client_id",
        "client_secret",
        "site_hostname",
        "site_path",
        "drive_folder_path",
    ]
    return bool(cfg) and all(str(cfg.get(k, "")).strip() for k in required)


@st.cache_data(show_spinner=False, ttl=3300)
def _msgraph_get_access_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }
    resp = requests.post(token_url, data=payload, timeout=60)
    resp.raise_for_status()
    token_data = resp.json()
    token = str(token_data.get("access_token", "")).strip()
    if not token:
        raise RuntimeError("Microsoft Graph token response does not contain access_token.")
    return token


def _msgraph_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _normalize_site_path(site_path: str) -> str:
    p = site_path.strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


def _normalize_drive_folder_path(folder_path: str) -> str:
    p = folder_path.strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


def _list_graph_folder_files(token: str, site_hostname: str, site_path: str, folder_path: str) -> list[dict]:
    site_path = _normalize_site_path(site_path)
    folder_path = _normalize_drive_folder_path(folder_path)

    site_lookup_url = f"https://graph.microsoft.com/v1.0/sites/{site_hostname}:{site_path}"
    site_resp = requests.get(site_lookup_url, headers=_msgraph_headers(token), timeout=60)
    site_resp.raise_for_status()
    site_id = str(site_resp.json().get("id", "")).strip()
    if not site_id:
        raise RuntimeError("Unable to resolve Graph site id from site_hostname/site_path.")

    next_url = (
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:{folder_path}:/children"
        "?$top=200"
    )

    files: list[dict] = []
    while next_url:
        page_resp = requests.get(next_url, headers=_msgraph_headers(token), timeout=60)
        page_resp.raise_for_status()
        page = page_resp.json()
        for item in page.get("value", []):
            if isinstance(item, dict) and "file" in item:
                files.append(item)
        next_url = page.get("@odata.nextLink")

    return files


@st.cache_data(show_spinner=False, ttl=900)
def _download_msgraph_files(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    site_hostname: str,
    site_path: str,
    drive_folder_path: str,
    file_extensions: tuple[str, ...],
    file_name_contains: str,
    max_files: int,
) -> list[str]:
    token = _msgraph_get_access_token(tenant_id, client_id, client_secret)
    files = _list_graph_folder_files(token, site_hostname, site_path, drive_folder_path)

    ext_set = {e.lower() for e in file_extensions}
    filtered = []
    for item in files:
        name = str(item.get("name", "")).strip()
        ext = Path(name).suffix.lower()
        if ext_set and ext not in ext_set:
            continue
        if file_name_contains and file_name_contains not in name.lower():
            continue
        filtered.append(item)

    filtered.sort(key=lambda x: str(x.get("lastModifiedDateTime", "")), reverse=True)
    picked = filtered[: max(1, max_files)]

    cache_dir = Path(tempfile.gettempdir()) / "summit_station_msgraph_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    out_paths: list[str] = []
    for i, item in enumerate(picked):
        name = str(item.get("name", f"graph_file_{i+1}.csv")).strip()
        dl_url = str(item.get("@microsoft.graph.downloadUrl", "")).strip()
        if dl_url:
            data_resp = requests.get(dl_url, timeout=90)
        else:
            item_id = str(item.get("id", "")).strip()
            if not item_id:
                continue
            content_url = (
                "https://graph.microsoft.com/v1.0/"
                f"drives/{item.get('parentReference', {}).get('driveId')}/items/{item_id}/content"
            )
            data_resp = requests.get(content_url, headers=_msgraph_headers(token), timeout=90)
        data_resp.raise_for_status()

        ext = Path(name).suffix.lower()
        if ext not in {".csv", ".xlsx", ".xlsm", ".xls"}:
            ctype = str(data_resp.headers.get("Content-Type", "")).lower()
            ext = ".csv" if "csv" in ctype else ".xlsx"

        file_hash = hashlib.md5(data_resp.content).hexdigest()[:12]
        safe_stem = Path(name).stem.replace(" ", "_")
        out_file = cache_dir / f"{safe_stem}.{file_hash}{ext}"
        out_file.write_bytes(data_resp.content)
        out_paths.append(str(out_file))

    if not out_paths:
        raise RuntimeError("No files matched the Microsoft Graph filter settings.")

    return out_paths


@st.cache_data(show_spinner=False, ttl=900)
def _download_onedrive_files(urls: tuple[str, ...]) -> list[str]:
    """Download OneDrive shared links to temp files and return local paths."""
    cache_dir = Path(tempfile.gettempdir()) / "summit_station_onedrive_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    out_paths: list[str] = []
    for i, url in enumerate(urls):
        resp = requests.get(url, timeout=90)
        resp.raise_for_status()

        parsed = urllib.parse.urlparse(url)
        name_guess = Path(urllib.parse.unquote(parsed.path)).name or f"onedrive_{i+1}"
        suffix = Path(name_guess).suffix.lower()
        if suffix not in {".csv", ".xlsx", ".xlsm", ".xls"}:
            ctype = str(resp.headers.get("Content-Type", "")).lower()
            suffix = ".csv" if "csv" in ctype else ".xlsx"

        file_hash = hashlib.md5(resp.content).hexdigest()[:12]
        out_file = cache_dir / f"{Path(name_guess).stem}.{file_hash}{suffix}"
        out_file.write_bytes(resp.content)
        out_paths.append(str(out_file))

    return out_paths


st.set_page_config(page_title="Summit Yield by operation & Slow runner", page_icon="📊", layout="wide")
st.title("Summit Yield by operation & Slow runner")

# Make multiselect filter items use green style (instead of red theme color)
st.markdown(
    """
    <style>
    /* Selected chips in multiselect input */
    .stMultiSelect [data-baseweb="tag"] {
        background-color: #22c55e !important;
        border-color: #16a34a !important;
    }
    .stMultiSelect [data-baseweb="tag"] span {
        color: #ffffff !important;
    }
    .stMultiSelect [data-baseweb="tag"] svg {
        fill: #ffffff !important;
    }

    /* Filter value text in select controls */
    .stSelectbox [data-baseweb="select"] > div,
    .stMultiSelect [data-baseweb="select"] > div {
        color: #16a34a !important;
    }

    /* Selected option rows in dropdown panel */
    .stMultiSelect [role="option"][aria-selected="true"] {
        background-color: rgba(34, 197, 94, 0.16) !important;
        border-left: 3px solid #22c55e !important;
    }

    /* Check mark / selected indicator */
    .stMultiSelect [role="option"][aria-selected="true"] svg,
    .stMultiSelect [role="option"][aria-selected="true"] * {
        color: #16a34a !important;
        fill: #16a34a !important;
    }

    /* Download buttons: blue background + white text */
    .stDownloadButton button[kind="primary"] {
        background-color: #2563eb !important;
        border: 1px solid #1d4ed8 !important;
        color: #ffffff !important;
        font-weight: 700 !important;
    }
    .stDownloadButton button[kind="primary"]:hover {
        background-color: #1d4ed8 !important;
        border-color: #1e40af !important;
        color: #ffffff !important;
    }
    .stDownloadButton button[kind="primary"] p,
    .stDownloadButton button[kind="primary"] span {
        color: #ffffff !important;
        font-weight: 700 !important;
    }

    /* Keep dataframe/table surface stable when switching tabs/pages */
    [data-testid="stDataFrame"] {
        background-color: #ffffff !important;
    }
    [data-testid="stDataFrame"] [role="grid"],
    [data-testid="stDataFrame"] [role="row"],
    [data-testid="stDataFrame"] [role="gridcell"],
    [data-testid="stDataFrame"] [role="columnheader"],
    [data-testid="stDataFrame"] [role="rowheader"] {
        background-color: #ffffff !important;
        color: #111827 !important;
    }
    [data-testid="stTable"] table,
    [data-testid="stTable"] th,
    [data-testid="stTable"] td {
        background-color: #ffffff !important;
        color: #111827 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Settings")
    aps2_yield = (
        st.number_input("APS2 Yield (%)", min_value=0.0, max_value=100.0, value=98.0, step=0.1)
        / 100.0
    )

    uploaded_files = st.file_uploader(
        "Upload SUMMIT raw files (.csv/.xlsx)",
        type=["csv", "xlsx", "xlsm", "xls"],
        accept_multiple_files=True,
    )

    msgraph_cfg = _get_msgraph_config_from_secrets()
    msgraph_enabled = _is_msgraph_config_valid(msgraph_cfg)
    if msgraph_enabled:
        st.caption("Auto source enabled from Microsoft Graph secrets")

    onedrive_urls = _get_onedrive_urls_from_secrets()
    if onedrive_urls:
        st.caption("Auto source enabled from OneDrive secrets")

raw_paths = []
data_source_mode = ""
if uploaded_files:
    data_source_mode = "Manual Upload"
    save_path = Path("./uploads")
    try:
        save_path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        save_path = Path(tempfile.gettempdir()) / "summit_station_uploads"
        save_path.mkdir(parents=True, exist_ok=True)
    if "uploaded_raw_paths" not in st.session_state:
        st.session_state["uploaded_raw_paths"] = {}

    for i, uploaded in enumerate(uploaded_files):
        content = uploaded.getbuffer()
        file_hash = hashlib.md5(content).hexdigest()[:12]
        cache_key = f"{uploaded.name}|{uploaded.size}|{file_hash}"

        cached = st.session_state["uploaded_raw_paths"].get(cache_key)
        if cached and Path(cached).exists():
            raw_paths.append(cached)
            continue

        out = save_path / f"{uploaded.name}.{file_hash}.upload"
        out.write_bytes(content)
        out_path = str(out)
        st.session_state["uploaded_raw_paths"][cache_key] = out_path
        raw_paths.append(out_path)
elif msgraph_enabled:
    data_source_mode = "Microsoft Graph (Secrets)"
    try:
        raw_paths = _download_msgraph_files(
            tenant_id=msgraph_cfg["tenant_id"],
            client_id=msgraph_cfg["client_id"],
            client_secret=msgraph_cfg["client_secret"],
            site_hostname=msgraph_cfg["site_hostname"],
            site_path=msgraph_cfg["site_path"],
            drive_folder_path=msgraph_cfg["drive_folder_path"],
            file_extensions=tuple(msgraph_cfg["file_extensions"]),
            file_name_contains=msgraph_cfg["file_name_contains"],
            max_files=int(msgraph_cfg["max_files"]),
        )
    except Exception:
        st.error("Failed to load source files via Microsoft Graph.")
        with st.expander("Click to view Microsoft Graph error", expanded=True):
            st.code(traceback.format_exc(), language="text")
        st.stop()
elif onedrive_urls:
    data_source_mode = "OneDrive (Secrets)"
    try:
        raw_paths = _download_onedrive_files(tuple(onedrive_urls))
    except Exception:
        st.error("Failed to download OneDrive source file(s).")
        with st.expander("Click to view OneDrive download error", expanded=True):
            st.code(traceback.format_exc(), language="text")
        st.stop()
else:
    data_source_mode = "Local Default Path"
    raw_paths = default_raw_paths()

if not raw_paths:
    with st.sidebar:
        st.info("No data source found. Upload raw file(s) or configure OneDrive secrets.")
    st.stop()

with st.sidebar:
    st.info("Data source: " + data_source_mode)
    st.info("Using file(s): " + ", ".join(Path(p).name for p in raw_paths))

try:
    df = load_raw_data_many(raw_paths)
except KeyError:
    st.error("数据字段异常（KeyError）已隐藏。")
    with st.expander("点击展开 KeyError 详情", expanded=False):
        st.code(traceback.format_exc(), language="text")
    st.stop()
except Exception:
    st.error("加载数据失败。")
    with st.expander("点击展开错误详情", expanded=True):
        st.code(traceback.format_exc(), language="text")
    st.stop()

week_options = sorted(
    [v for v in df.get("FISCAL_WEEK_DISPLAY", pd.Series(dtype=str)).dropna().unique().tolist() if str(v).strip()],
    key=fiscal_week_display_sort_key,
)
if not week_options:
    week_options = sorted(
        [str(v).strip() for v in df["FISCAL_WEEK"].dropna().unique().tolist() if str(v).strip()]
    )
birth_week_options = sorted(
    [v for v in df["BIRTH_WEEK"].dropna().unique().tolist() if str(v).strip()],
    key=birth_week_sort_key,
)
prime_options = sorted(df["PRIME"].dropna().unique().tolist())
capacity_options = sorted(df["FORMAT_CAPACITY"].dropna().unique().tolist())
config_options = {
    col: sorted(df[col].dropna().unique().tolist())
    if col in df.columns
    else []
    for col in CONFIG_FILTER_COLUMNS
}

if "config_defs" not in st.session_state:
    persisted = load_config_state().get("config_defs", {})
    base_defs = {name: {col: [] for col in CONFIG_FILTER_COLUMNS} for name in CONFIG_NAMES}
    for name in CONFIG_NAMES:
        candidate = None
        if name in persisted and isinstance(persisted[name], dict):
            candidate = persisted[name]
        else:
            legacy_name = CURRENT_TO_LEGACY_CONFIG_NAME.get(name)
            if legacy_name in persisted and isinstance(persisted[legacy_name], dict):
                candidate = persisted[legacy_name]
        if isinstance(candidate, dict):
            for col in CONFIG_FILTER_COLUMNS:
                vals = candidate.get(col, [])
                if isinstance(vals, list):
                    base_defs[name][col] = [str(v).strip() for v in vals if str(v).strip()]
    st.session_state["config_defs"] = base_defs
if "config_week_defs" not in st.session_state:
    base_week_defs = {
        name: {
            "first_n": 4,
            "sr1_n": 5,
            "sr3_n": 7,
            "start_week_label": "Auto (earliest)",
        }
        for name in CONFIG_NAMES
    }
    persisted_week = load_config_state().get("config_week_defs", {})
    for name in CONFIG_NAMES:
        one = None
        if name in persisted_week and isinstance(persisted_week[name], dict):
            one = persisted_week[name]
        else:
            legacy_name = CURRENT_TO_LEGACY_CONFIG_NAME.get(name)
            if legacy_name in persisted_week and isinstance(persisted_week[legacy_name], dict):
                one = persisted_week[legacy_name]
        if isinstance(one, dict):
            try:
                base_week_defs[name]["first_n"] = int(one.get("first_n", base_week_defs[name]["first_n"]))
                base_week_defs[name]["sr1_n"] = int(one.get("sr1_n", base_week_defs[name]["sr1_n"]))
                base_week_defs[name]["sr3_n"] = int(one.get("sr3_n", base_week_defs[name]["sr3_n"]))
                base_week_defs[name]["start_week_label"] = str(
                    one.get("start_week_label", base_week_defs[name]["start_week_label"])
                )
            except Exception:
                pass
    st.session_state["config_week_defs"] = base_week_defs

fiscal_week_label_options = ["Auto (earliest)"] + build_fiscal_week_labels(df)
config_editable = _open_config_login_dialog_if_needed()

with st.sidebar:
    with st.expander("Config Settings (click to modify)", expanded=False):
        if not hasattr(st, "dialog") and not config_editable:
            st.caption("Enter username/password to unlock Config Settings.")
            with st.form("config_login_form_sidebar", clear_on_submit=False):
                username = st.text_input("Username", key="cfg_login_user")
                password = st.text_input("Password", type="password", key="cfg_login_pass")
                submitted = st.form_submit_button("Unlock")
            if submitted:
                if username == CONFIG_LOGIN_USERNAME and password == CONFIG_LOGIN_PASSWORD:
                    st.session_state["config_auth_ok"] = True
                    config_editable = True
                    st.success("Login successful. Please reopen this panel if needed.")
                    st.rerun()
                else:
                    st.error("Incorrect username or password.")

        if config_editable:
            if st.button("Lock Config Settings", key="cfg_lock_btn"):
                st.session_state["config_auth_ok"] = False
                st.rerun()

        cfg_tabs = st.tabs(CONFIG_NAMES)
        for cfg_name, tab in zip(CONFIG_NAMES, cfg_tabs):
            with tab:
                st.caption(f"Define filters for {cfg_name}")
                # Render filter selectors in 2 rows for better readability.
                split_idx = (len(CONFIG_FILTER_COLUMNS) + 1) // 2
                row1_cols = CONFIG_FILTER_COLUMNS[:split_idx]
                row2_cols = CONFIG_FILTER_COLUMNS[split_idx:]

                ui_row1 = st.columns(len(row1_cols))
                for ui_col, col in zip(ui_row1, row1_cols):
                    with ui_col:
                        key = f"cfg::{cfg_name}::{col}"
                        stored_default = st.session_state["config_defs"][cfg_name].get(col, [])
                        merged_options = sorted(
                            list(set(config_options[col]).union(set(stored_default))),
                            key=lambda x: str(x),
                        )
                        safe_default = [v for v in stored_default if v in merged_options]
                        selected_vals = st.multiselect(
                            col,
                            options=merged_options,
                            default=safe_default,
                            key=key,
                            disabled=not config_editable,
                        )
                        manual_text = st.text_input(
                            f"Manual {col} (comma separated)",
                            value="",
                            key=f"cfg_manual::{cfg_name}::{col}",
                            disabled=not config_editable,
                        )
                        manual_vals = parse_manual_filter_values(manual_text)
                        if config_editable:
                            st.session_state["config_defs"][cfg_name][col] = sorted(
                                list(set(selected_vals).union(set(manual_vals))),
                                key=lambda x: str(x),
                            )

                if row2_cols:
                    ui_row2 = st.columns(len(row2_cols))
                    for ui_col, col in zip(ui_row2, row2_cols):
                        with ui_col:
                            key = f"cfg::{cfg_name}::{col}"
                            stored_default = st.session_state["config_defs"][cfg_name].get(col, [])
                            merged_options = sorted(
                                list(set(config_options[col]).union(set(stored_default))),
                                key=lambda x: str(x),
                            )
                            safe_default = [v for v in stored_default if v in merged_options]
                            selected_vals = st.multiselect(
                                col,
                                options=merged_options,
                                default=safe_default,
                                key=key,
                                disabled=not config_editable,
                            )
                            manual_text = st.text_input(
                                f"Manual {col} (comma separated)",
                                value="",
                                key=f"cfg_manual::{cfg_name}::{col}",
                                disabled=not config_editable,
                            )
                            manual_vals = parse_manual_filter_values(manual_text)
                            if config_editable:
                                st.session_state["config_defs"][cfg_name][col] = sorted(
                                    list(set(selected_vals).union(set(manual_vals))),
                                    key=lambda x: str(x),
                                )

                wk1, wk2, wk3 = st.columns(3)
                with wk1:
                    first_n = st.number_input(
                        "First yield weeks",
                        min_value=1,
                        max_value=53,
                        value=int(st.session_state["config_week_defs"][cfg_name].get("first_n", 4)),
                        step=1,
                        key=f"cfg_week_first::{cfg_name}",
                        disabled=not config_editable,
                    )
                with wk2:
                    sr1_n = st.number_input(
                        "SR 1week end weeks",
                        min_value=int(first_n),
                        max_value=53,
                        value=max(
                            int(first_n),
                            int(st.session_state["config_week_defs"][cfg_name].get("sr1_n", 5)),
                        ),
                        step=1,
                        key=f"cfg_week_sr1::{cfg_name}",
                        disabled=not config_editable,
                    )
                with wk3:
                    sr3_n = st.number_input(
                        "SR 3week end weeks",
                        min_value=int(sr1_n),
                        max_value=53,
                        value=max(
                            int(sr1_n),
                            int(st.session_state["config_week_defs"][cfg_name].get("sr3_n", 7)),
                        ),
                        step=1,
                        key=f"cfg_week_sr3::{cfg_name}",
                        disabled=not config_editable,
                    )

                if config_editable:
                    st.session_state["config_week_defs"][cfg_name] = {
                        "first_n": int(first_n),
                        "sr1_n": int(sr1_n),
                        "sr3_n": int(sr3_n),
                        "start_week_label": st.session_state["config_week_defs"][cfg_name].get(
                            "start_week_label", "Auto (earliest)"
                        ),
                    }

                current_start = st.session_state["config_week_defs"][cfg_name].get(
                    "start_week_label", "Auto (earliest)"
                )
                if current_start not in fiscal_week_label_options:
                    current_start = "Auto (earliest)"
                start_week_label = st.selectbox(
                    "Start fiscal week",
                    options=fiscal_week_label_options,
                    index=fiscal_week_label_options.index(current_start),
                    key=f"cfg_week_start::{cfg_name}",
                    disabled=not config_editable,
                )
                if config_editable:
                    st.session_state["config_week_defs"][cfg_name]["start_week_label"] = start_week_label

save_config_state(
    st.session_state.get("config_defs", {}),
    st.session_state.get("config_week_defs", {}),
)

# Top filter row hidden by request; keep empty selections so all data is included.
selected_birth_week = []
selected_week = []
selected_capacity = []

global_pwt_pass_sn = set(
    df.loc[(df["OPERATION"] == "PWT") & (df["EVENT_STATUS"] == "P"), "DRIVE_SERIAL_NUM"]
    .dropna()
    .astype(str)
    .str.strip()
)

summary_cols = [
    "BIRTH_WEEK",
    "loading_QTY",
    "First_yield",
    "SR_1week_yield",
    "SR_3week_yield",
    "CUM_Yield",
    "Incomplte%",
]


def build_summary_views(by_bw_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (ui_matrix, export_rows)."""
    summary_show = by_bw_df[summary_cols].copy()
    summary_show["BIRTH_WEEK"] = summary_show["BIRTH_WEEK"].map(display_birth_week_label)
    summary_show["loading_QTY"] = summary_show["loading_QTY"].fillna(0).astype(int)
    for c in ["First_yield", "SR_1week_yield", "SR_3week_yield", "CUM_Yield", "Incomplte%"]:
        summary_show[c] = summary_show[c].map(lambda x: f"{x:.4%}")

    ui_matrix = summary_show.set_index("BIRTH_WEEK").T
    ui_matrix.index.name = None
    # Avoid Arrow dtype warnings on mixed-looking object columns.
    ui_matrix = ui_matrix.astype(str)
    return ui_matrix, summary_show


st.subheader("Derived yields by BIRTH_WEEK (All CONFIG)")

all_export_rows: list[pd.DataFrame] = []

for cfg_name in CONFIG_NAMES:
    cfg_filter = st.session_state["config_defs"].get(cfg_name, {col: [] for col in CONFIG_FILTER_COLUMNS})
    cfg_has_any_selection = any(len(cfg_filter.get(col, [])) > 0 for col in CONFIG_FILTER_COLUMNS)

    cfg_week_def = st.session_state["config_week_defs"].get(
        cfg_name,
        {"first_n": 4, "sr1_n": 5, "sr3_n": 7, "start_week_label": "Auto (earliest)"},
    )
    first_n = int(cfg_week_def.get("first_n", 4))
    sr1_n = int(cfg_week_def.get("sr1_n", 5))
    sr3_n = int(cfg_week_def.get("sr3_n", 7))
    cfg_start_week = cfg_week_def.get("start_week_label", "Auto (earliest)")
    if cfg_start_week == "Auto (earliest)":
        cfg_start_week = None

    if not cfg_has_any_selection:
        st.markdown(f"### {cfg_name}")
        st.warning("No data: this CONFIG has no conditions defined in Config Settings.")
        continue

    filtered_cfg = apply_filters(
        df,
        selected_birth_week,
        cfg_filter,
        selected_week,
        [],
        selected_capacity,
    )

    st.markdown(f"### {cfg_name}")
    st.caption(f"Filtered rows: {len(filtered_cfg):,}")

    has_data = False
    for prime_tag in ["Y", "N"]:
        st.markdown(f"**PRIME={prime_tag}**")
        prime_df = filtered_cfg[filtered_cfg["PRIME"].astype(str).str.upper() == prime_tag].copy()

        try:
            by_bw_df = calc_birth_week_metrics(
                prime_df,
                aps2_yield=aps2_yield,
                prefix_weeks=(first_n, sr1_n, sr3_n),
                start_week_label=cfg_start_week,
                pwt_pass_sn_exclude=global_pwt_pass_sn,
            )
        except Exception:
            st.error(f"{cfg_name} PRIME={prime_tag}: derived yield calculation failed.")
            with st.expander(f"{cfg_name} PRIME={prime_tag} error details", expanded=False):
                st.code(traceback.format_exc(), language="text")
            continue

        if by_bw_df.empty:
            st.info("No data for this PRIME.")
            continue

        has_data = True
        ui_matrix, export_rows = build_summary_views(by_bw_df)
        st.dataframe(ui_matrix, width="stretch")

        export_rows.insert(0, "PRIME", prime_tag)
        export_rows.insert(0, "CONFIG", cfg_name)
        all_export_rows.append(export_rows)

    if not has_data:
        st.info("No derived yield data under current filters.")

if all_export_rows:
    export_df = pd.concat(all_export_rows, ignore_index=True)
    st.download_button(
        "Download All CONFIG PRIME(Y/N) CSV",
        data=export_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="all_config_prime_y_n_derived_yield.csv",
        mime="text/csv",
        type="primary",
    )
else:
    st.info("No data available for CSV export.")
