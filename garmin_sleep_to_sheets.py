import base64
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
import gspread

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Silence noisy internal logging from the third-party Garmin libraries
logging.getLogger("garminconnect").setLevel(logging.WARNING)
logging.getLogger("garth").setLevel(logging.WARNING)

# Header Definitions
SLEEP_HEADERS = [
    "date", "sleep_start_timestamp_local", "sleep_end_timestamp_local", "total_sleep_seconds",
    "deep_sleep_seconds", "light_sleep_seconds", "rem_sleep_seconds", "awake_seconds",
    "sleep_score", "sleep_score_qualifier", "avg_overnight_hrv", "avg_spo2_value",
    "avg_respiration_value", "resting_heart_rate", "body_battery_change",
]
ACT_HEADERS = [
    "activity_id", "activity_name", "activity_type", "start_time_local",
    "distance_meters", "duration_seconds", "elapsed_duration_seconds",
    "moving_duration_seconds", "average_speed_mps", "max_speed_mps",
    "calories", "average_hr", "max_hr", "steps", "elevation_gain_meters",
]
STRESS_HEADERS = ["date_hour", "min_stress", "max_stress", "avg_stress"]
HR_HEADERS = ["date_hour", "min_hr", "max_hr", "avg_hr"]
STEPS_HEADERS = ["date_hour", "total_steps"]
SNAPSHOTS_HEADERS = ["snapshot_id", "timestamp_local", "avg_hr", "avg_stress", "avg_respiration", "spo2", "rmssd", "sdnn"]
STAGES_HEADERS = ["start_time_local", "end_time_local", "stage", "duration_seconds"]
MOVEMENT_HEADERS = ["date_hour", "min_movement", "max_movement", "avg_movement"]
BREATHING_HEADERS = ["date_hour", "min_respiration", "max_respiration", "avg_respiration"]
RESTLESS_HEADERS = ["date_hour", "min_restless", "max_restless", "avg_restless"]
BODY_BATTERY_HEADERS = ["date_hour", "min_body_battery", "max_body_battery", "avg_body_battery"]


def get_garmin_client() -> Garmin:
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    token_dir = "/tmp/garmin_tokens"
    if not email or not password:
        logging.error("GARMIN_EMAIL and GARMIN_PASSWORD required.")
        sys.exit(1)
    try:
        garmin = Garmin(email=email, password=password)
        logging.info("Authenticating with Garmin Connect...")
        garmin.login(tokenstore=token_dir)
        return garmin
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "rate limit" in err_msg.lower():
            logging.error("Garmin rate limit hit. Wait 2-4 hours.")
        elif any(term in err_msg for term in ["MFA", "mfa", "2FA", "2fa"]):
            logging.error("Garmin MFA required. Disable MFA on Garmin account.")
        else:
            logging.error(f"Garmin Auth Failed: {e}")
        sys.exit(1)


def get_gspread_client() -> gspread.Client:
    creds_path = "/tmp/gsa.json"
    if os.path.exists(creds_path):
        return gspread.service_account(filename=creds_path)
    raw_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not raw_creds:
        logging.error("GOOGLE_CREDENTIALS_JSON missing.")
        sys.exit(1)
    try:
        creds_dict = json.loads(raw_creds)
    except json.JSONDecodeError:
        creds_dict = json.loads(base64.b64decode(raw_creds).decode("utf-8"))
    return gspread.service_account_from_dict(creds_dict)


def to_date_hour(ts):
    if not ts: return None
    if isinstance(ts, (int, float)):
        if ts > 1e11: ts /= 1000.0
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:00")
    if isinstance(ts, str):
        ts_clean = ts.replace("T", " ").split(".")[0]
        try:
            return datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:00")
        except ValueError: pass
    return None


def find_val_recursive(d, target_key):
    if isinstance(d, dict):
        for k, v in d.items():
            if k.lower() == target_key.lower(): return v
            res = find_val_recursive(v, target_key)
            if res is not None: return res
    elif isinstance(d, list):
        for item in d:
            res = find_val_recursive(item, target_key)
            if res is not None: return res
    return None


def sync_sheet(sh, title, headers):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows="100", cols="20")
    existing = ws.row_values(1)
    if not existing:
        ws.append_row(headers)
        col_a = [headers[0]]
    else:
        col_a = [str(x) for x in ws.col_values(1)]
    return ws, col_a


def get_incremental_dates(col_a, max_backfill_days):
    today = date.today()
    recorded_dates = []
    for key in col_a[1:]:
        if not key: continue
        try:
            date_part = str(key).split(" ")[0].split("T")[0]
            recorded_dates.append(datetime.strptime(date_part, "%Y-%m-%d").date())
        except ValueError: continue
    if not recorded_dates:
        return [today - timedelta(days=i) for i in range(max_backfill_days - 1, -1, -1)]
    
    latest_recorded = max(recorded_dates)
    effective_start = min(latest_recorded, today)
    
    days_to_fetch = max(1, (today - effective_start).days)
    return [effective_start + timedelta(days=i) for i in range(days_to_fetch + 1)]


def cells_are_equal(v1, v2):
    s1 = str(v1).strip() if v1 is not None else ""
    s2 = str(v2).strip() if v2 is not None else ""
    
    if s1 == s2 or (not s1 and not s2): return True
    if not s1 or not s2: return False

    parsed_matched = False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"):
        try:
            dt1, dt2 = datetime.strptime(s1.replace("T", " ").split(".")[0], fmt), datetime.strptime(s2.replace("T", " ").split(".")[0], fmt)
            if dt1 == dt2: parsed_matched = True; break
        except ValueError: continue
    if parsed_matched: return True

    try:
        n1, n2 = float(s1.replace(",", ".")), float(s2.replace(",", "."))
        if abs(n1 - n2) < 0.05: return True
    except ValueError: pass

    return s1.lower() == s2.lower()


def rows_are_equal(new_vals, existing_vals, length):
    ext_row = list(existing_vals)
    while len(ext_row) < length: ext_row.append("")
    ext_row = ext_row[:length]

    new_row = list(new_vals)
    while len(new_row) < length: new_row.append("")
    new_row = new_row[:length]

    for v1, v2 in zip(new_row, ext_row):
        if not cells_are_equal(v1, v2): return False
    return True


def upsert_data(ws, col_a, row_data_dict, headers):
    if not row_data_dict: return
    all_existing_values = ws.get_all_values()
    existing_row_map = {}
    if len(all_existing_values) > 1:
        for row in all_existing_values[1:]:
            if row: existing_row_map[str(row[0])] = row

    end_col_letter = chr(ord("A") + len(headers) - 1)
    keys_to_update, keys_to_insert = [], []
    for key in row_data_dict.keys():
        if str(key) in existing_row_map: keys_to_update.append(key)
        else: keys_to_insert.append(key)

    # 1. Update existing entries in-place (ONLY if normalized values differ)
    for key in keys_to_update:
        key_str = str(key)
        new_vals = [v if v is not None else "" for v in row_data_dict[key]]
        if not rows_are_equal(new_vals, existing_row_map[key_str], len(headers)):
            idx = col_a.index(key_str) + 1
            ws.update(f"A{idx}:{end_col_letter}{idx}", [new_vals])
            time.sleep(0.15)
            logging.info(f"Updated {ws.title} row {idx} for {key_str}")
        else:
            logging.info(f"Skipped unchanged row in {ws.title} for {key_str}")

    # 2. Bulk Insert new entries below header (Row 2)
    if keys_to_insert:
        sorted_keys = sorted(keys_to_insert, reverse=True)
        rows_to_insert = []
        for key in sorted_keys:
            key_str = str(key)
            new_vals = [v if v is not None else "" for v in row_data_dict[key]]
            rows_to_insert.append(new_vals)
            col_a.insert(1, key_str)

        ws.insert_rows(rows_to_insert, row=2)
        time.sleep(0.15)
        logging.info(f"Bulk inserted {len(rows_to_insert)} rows at top of {ws.title}")


def parse_sleep_row(target_date: str, sleep_data: dict) -> list:
    dto = (sleep_data or {}).get("dailySleepDTO")
    if not dto: return []
    
    total_sleep = dto.get("sleepTimeSeconds")
    sleep_score = (dto.get("sleepScores") or {}).get("overall", {}).get("value") or dto.get("sleepScore")
    if not total_sleep and not sleep_score:
        return []

    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    row_dict = {
        "date": target_date, "sleep_start_timestamp_local": dto.get("sleepStartTimestampLocal") or dto.get("startTimestampLocal"),
        "sleep_end_timestamp_local": dto.get("sleepEndTimestampLocal") or dto.get("endTimestampLocal"),
        "total_sleep_seconds": total_sleep, "deep_sleep_seconds": dto.get("deepSleepSeconds"),
        "light_sleep_seconds": dto.get("lightSleepSeconds"), "rem_sleep_seconds": dto.get("remSleepSeconds"),
        "awake_seconds": dto.get("awakeSleepSeconds"), "sleep_score": sleep_score,
        "sleep_score_qualifier": overall.get("qualifierKey") or dto.get("sleepScoreQualifier"),
        "avg_overnight_hrv": sleep_data.get("avgOvernightHrv") or dto.get("avgOvernightHrv"),
        "avg_spo2_value": sleep_data.get("averageSpO2Value") or dto.get("averageSpO2Value"),
        "avg_respiration_value": sleep_data.get("averageRespirationValue") or dto.get("averageRespirationValue"),
        "resting_heart_rate": sleep_data.get("restingHeartRate") or dto.get("restingHeartRate"),
        "body_battery_change": sleep_data.get("bodyBatteryChange") or dto.get("bodyBatteryChange"),
    }
    return [row_dict.get(h) if row_dict.get(h) is not None else "" for h in SLEEP_HEADERS]


def parse_act_row(act: dict) -> list:
    act_type = act.get("activityType") or {}
    type_key = act_type.get("typeKey") if isinstance(act_type, dict) else str(act_type)
    row_dict = {
        "activity_id": act.get("activityId"), "activity_name": act.get("activityName"),
        "activity_type": type_key, "start_time_local": act.get("startTimeLocal"),
        "distance_meters": act.get("distance"), "duration_seconds": act.get("duration"),
        "elapsed_duration_seconds": act.get("elapsedDuration"), "moving_duration_seconds": act.get("movingDuration"),
        "average_speed_mps": act.get("averageSpeed"), "max_speed_mps": act.get("maxSpeed"),
        "calories": act.get("calories"), "average_hr": act.get("averageHR"),
        "max_hr": act.get("maxHR"), "steps": act.get("steps"), "elevation_gain_meters": act.get("elevationGain"),
    }
    return [row_dict.get(h) if row_dict.get(h) is not None else "" for h in ACT_HEADERS]


def fetch_health_snapshots(garmin, dates):
    rows = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.connectapi(f"/healthsnapshot-service/snapshot/daily/{date_str}")
            if not data: continue
            snapshots = data if isinstance(data, list) else (find_val_recursive(data, "summaries") or find_val_recursive(data, "snapshotList") or find_val_recursive(data, "snapshots") or [])
            for item in snapshots:
                if not isinstance(item, dict): continue
                snap_id = str(item.get("snapshotId") or item.get("summaryId") or item.get("startTimestampLocal") or item.get("startTimestampGMT"))
                if not snap_id: continue
                ts = item.get("startTimestampLocal") or item.get("startTimestampGMT") or item.get("startTimeLocal")
                rows[snap_id] = [
                    snap_id, ts or "",
                    item.get("averageHeartRate") or item.get("avgHeartRate"),
                    item.get("averageStress") or item.get("avgStress"),
                    item.get("averageRespiration") or item.get("avgRespiration"),
                    item.get("spo2") or item.get("averageSpO2"),
                    item.get("rmssd") or item.get("hrvRmssd"),
                    item.get("sdnn") or item.get("hrvSdnn"),
                ]
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No Health Snapshots recorded on {date_str}.")
            else:
                logging.error(f"Snapshot error {date_str}: {e}")
    return rows


def process_sleep_stages(garmin, dates):
    stages_rows = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_sleep_data(date_str) or {}
            levels_map = find_val_recursive(data, "sleepLevels") or find_val_recursive(data, "sleepLevel") or find_val_recursive(data, "sleepStage") or {}
            if isinstance(levels_map, dict):
                for stage_name, items in levels_map.items():
                    if isinstance(items, list):
                        for it in items:
                            if isinstance(it, dict):
                                start = it.get("startLocal") or it.get("startTimeLocal") or it.get("startGMT")
                                if start: stages_rows[str(start)] = [str(start), it.get("endLocal") or it.get("endTimeLocal") or "", stage_name, it.get("durationInSeconds") or it.get("duration") or 0]
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No sleep stages exists on {date_str}.")
            else:
                logging.error(f"Stages error {date_str}: {e}")
    return stages_rows


def process_sleep_movement(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_sleep_data(date_str) or {}
            movements = find_val_recursive(data, "sleepMovement") or find_val_recursive(data, "movement") or []
            for it in movements if isinstance(movements, list) else []:
                ts, val = None, None
                if isinstance(it, dict):
                    ts = it.get("startGMT") or it.get("startLocal") or it.get("timestamp") or it.get("startTimestampGMT")
                    val = it.get("activityLevel") or it.get("value") or it.get("movementLevel") or 0
                elif isinstance(it, (list, tuple)) and len(it) >= 2: ts, val = it[0], it[1]
                if ts and val is not None and isinstance(val, (int, float)):
                    dh = to_date_hour(ts)
                    if dh: hourly_buckets.setdefault(dh, []).append(val)
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No sleep movement exists on {date_str}.")
            else:
                logging.error(f"Movement error {date_str}: {e}")
    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals: rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def process_breathing_disruptions(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_sleep_data(date_str) or {}
            respiration_data = find_val_recursive(data, "respiration") or find_val_recursive(data, "breath") or []
            if isinstance(respiration_data, dict): respiration_data = respiration_data.get("values") or []
            for it in respiration_data if isinstance(respiration_data, list) else []:
                ts, val = None, None
                if isinstance(it, dict):
                    ts = it.get("startGMT") or it.get("startLocal") or it.get("timestamp") or it.get("startTimeLocal")
                    val = it.get("respirationRate") or it.get("value") or it.get("epochValue")
                elif isinstance(it, (list, tuple)) and len(it) >= 2: ts, val = it[0], it[1]
                if ts and val is not None and isinstance(val, (int, float)):
                    dh = to_date_hour(ts)
                    if dh: hourly_buckets.setdefault(dh, []).append(val)
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No respiration data exists on {date_str}.")
            else:
                logging.error(f"Breathing error {date_str}: {e}")
    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals: rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def process_restless_moments(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_sleep_data(date_str) or {}
            restless = find_val_recursive(data, "restless") or find_val_recursive(data, "movement") or []
            for it in restless if isinstance(restless, list) else []:
                ts, val = None, None
                if isinstance(it, dict):
                    ts = it.get("startGMT") or it.get("startLocal") or it.get("timestamp")
                    val = it.get("duration") or it.get("value") or 1
                elif isinstance(it, (list, tuple)) and len(it) >= 2: ts, val = it[0], it[1]
                if ts and val is not None and isinstance(val, (int, float)):
                    dh = to_date_hour(ts)
                    if dh: hourly_buckets.setdefault(dh, []).append(val)
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No restless moments data exists on {date_str}.")
            else:
                logging.error(f"Restless error {date_str}: {e}")
    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals: rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def process_stress_data(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_stress_data(date_str) or {}
            raw = data.get("stressValuesArray") or (data.get("userBodyMap") or {}).get("stressValuesArray") or []
            for item in raw:
                ts, val = None, None
                if isinstance(item, (list, tuple)) and len(item) >= 2: ts, val = item[0], item[1]
                elif isinstance(item, dict):
                    ts = item.get("startTimestampGMT") or item.get("timestamp") or item.get("startGMT")
                    val = item.get("stressLevel") or item.get("value")
                if ts is not None and val is not None and isinstance(val, (int, float)) and val >= 0:
                    dh = to_date_hour(ts)
                    if dh: hourly_buckets.setdefault(dh, []).append(val)
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No stress data exists on {date_str}.")
            else:
                logging.error(f"Stress error {date_str}: {e}")
    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals: rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def process_hr_data(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_heart_rates(date_str) or {}
            raw = data.get("heartRateValues") or (data.get("userBodyMap") or {}).get("heartRateValues") or []
            for item in raw:
                ts, val = None, None
                if isinstance(item, (list, tuple)) and len(item) >= 2: ts, val = item[0], item[1]
                elif isinstance(item, dict):
                    ts = item.get("startTimestampGMT") or item.get("timestamp") or item.get("startGMT")
                    val = item.get("heartRate") or item.get("value")
                if ts is not None and val is not None and isinstance(val, (int, float)) and val > 0:
                    dh = to_date_hour(ts)
                    if dh: hourly_buckets.setdefault(dh, []).append(val)
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No heart rates exists on {date_str}.")
            else:
                logging.error(f"HR error {date_str}: {e}")
    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals: rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def process_steps_data(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_steps_data(date_str) or []
            raw = data if isinstance(data, list) else (data.get("stepItems") or data.get("stepsValuesArray") or [])
            for item in raw:
                ts, steps = None, 0
                if isinstance(item, (list, tuple)) and len(item) >= 2: ts, steps = item[0], item[1]
                elif isinstance(item, dict):
                    ts = item.get("startGMT") or item.get("startLocal") or item.get("timestamp")
                    steps = item.get("steps") or item.get("value") or 0
                if ts is not None and isinstance(steps, (int, float)) and steps >= 0:
                    dh = to_date_hour(ts)
                    if dh: hourly_buckets[dh] = hourly_buckets.get(dh, 0) + int(steps)
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No steps data exists on {date_str}.")
            else:
                logging.error(f"Steps error {date_str}: {e}")
    rows = {}
    for dh, total_steps in hourly_buckets.items():
        rows[dh] = [dh, total_steps]
    return rows


def process_body_battery_data(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_body_battery(date_str) or []
            raw = data if isinstance(data, list) else (find_val_recursive(data, "bodyBatteryValuesArray") or find_val_recursive(data, "values") or [])
            for item in raw if isinstance(raw, list) else []:
                ts, val = None, None
                if isinstance(item, dict):
                    ts = item.get("timestamp") or item.get("startTimestampGMT") or item.get("startGMT")
                    val = item.get("bodyBatteryValue") or item.get("value")
                elif isinstance(item, (list, tuple)) and len(item) >= 3:
                    ts = item[0]
                    val = item[2]
                if ts is not None and val is not None and isinstance(val, (int, float)) and 0 <= val <= 100:
                    dh = to_date_hour(ts)
                    if dh: hourly_buckets.setdefault(dh, []).append(val)
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg.lower():
                logging.info(f"No body battery data exists on {date_str}.")
            else:
                logging.error(f"Body battery error {date_str}: {e}")
    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals: rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def main():
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        logging.error("SPREADSHEET_ID is missing.")
        sys.exit(1)

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)
    today = date.today()

    # Prepare Sheets
    sleep_ws, sleep_col_a = sync_sheet(sh, os.environ.get("SHEET_NAME", "Sleep"), SLEEP_HEADERS)
    act_ws, act_col_a = sync_sheet(sh, os.environ.get("ACTIVITIES_SHEET_NAME", "Activities"), ACT_HEADERS)
    stress_ws, stress_col_a = sync_sheet(sh, os.environ.get("STRESS_SHEET_NAME", "Stress"), STRESS_HEADERS)
    hr_ws, hr_col_a = sync_sheet(sh, os.environ.get("HR_SHEET_NAME", "HR"), HR_HEADERS)
    steps_ws, steps_col_a = sync_sheet(sh, os.environ.get("STEPS_SHEET_NAME", "Steps"), STEPS_HEADERS)
    snap_ws, snap_col_a = sync_sheet(sh, os.environ.get("SNAPSHOTS_SHEET_NAME", "Snapshots"), SNAPSHOTS_HEADERS)
    stages_ws, stages_col_a = sync_sheet(sh, os.environ.get("STAGES_SHEET_NAME", "Sleep stages"), STAGES_HEADERS)
    move_ws, move_col_a = sync_sheet(sh, os.environ.get("MOVEMENT_SHEET_NAME", "Sleep Movement"), MOVEMENT_HEADERS)
    breath_ws, breath_col_a = sync_sheet(sh, os.environ.get("BREATHING_SHEET_NAME", "Breathing Disruption"), BREATHING_HEADERS)
    restless_ws, restless_col_a = sync_sheet(sh, os.environ.get("RESTLESS_SHEET_NAME", "Restless Moments"), RESTLESS_HEADERS)
    bb_ws, bb_col_a = sync_sheet(sh, os.environ.get("BODY_BATTERY_SHEET_NAME", "Body Battery"), BODY_BATTERY_HEADERS)

    # SELF-HEALING: Locate placeholder rows that contain only dates but no sleep summary data
    all_sleep_values = sleep_ws.get_all_values()
    empty_sleep_dates = []
    if len(all_sleep_values) > 1:
        for row in all_sleep_values[1:]:
            if row:
                date_key = row[0]
                if not any(row[1:]):
                    empty_sleep_dates.append(date_key)
                    if date_key in sleep_col_a:
                        sleep_col_a.remove(date_key)

    # GAP-FILLING: Identify all missing sleep nights in the past 60 days
    candidate_sleep_dates = [today - timedelta(days=i) for i in range(59, -1, -1)]
    sleep_dates = [dt for dt in candidate_sleep_dates if dt.strftime("%Y-%m-%d") not in sleep_col_a or dt == today]
    
    # Sync Activities for the past 14 days
    act_dates = [today - timedelta(days=i) for i in range(13, -1, -1)]

    # Detailed tables fetch the past 7 days (Quota-safe comparative sync)
    sub_sleep_dates = [today - timedelta(days=i) for i in range(7)]
    hourly_dates = [today - timedelta(days=i) for i in range(7)]
    snap_dates = [today - timedelta(days=i) for i in range(60)]

    garmin = get_garmin_client()

    # Sync Sleep
    if sleep_dates:
        sleep_rows = {}
        for dt in sleep_dates:
            t_date = dt.strftime("%Y-%m-%d")
            try:
                data = garmin.get_sleep_data(t_date)
                row = parse_sleep_row(t_date, data)
                if row: 
                    sleep_rows[t_date] = row
                else:
                    if t_date in empty_sleep_dates:
                        try:
                            all_rows = sleep_ws.get_all_values()
                            row_keys = [r[0] for r in all_rows]
                            if t_date in row_keys:
                                idx = row_keys.index(t_date) + 1
                                sleep_ws.delete_rows(idx)
                                logging.info(f"Deleted blank sleep placeholder row for {t_date}")
                        except Exception as del_err:
                            logging.error(f"Blank cleanup failed for {t_date}: {del_err}")
            except Exception as e: 
                err_msg = str(e)
                if "404" in err_msg or "not found" in err_msg.lower():
                    logging.info(f"No sleep data exists for {t_date} (404).")
                else:
                    logging.error(f"Sleep error {t_date}: {e}")
            time.sleep(0.2)
        upsert_data(sleep_ws, sleep_col_a, sleep_rows, SLEEP_HEADERS)

    # Sync Activities
    if act_dates:
        act_start, act_end = min(act_dates), max(act_dates)
        try:
            activities = garmin.get_activities_by_date(act_start.strftime("%Y-%m-%d"), act_end.strftime("%Y-%m-%d")) or []
            act_rows = {str(a.get("activityId")): parse_act_row(a) for a in activities}
            upsert_data(act_ws, act_col_a, act_rows, ACT_HEADERS)
        except Exception as e: logging.error(f"Act error: {e}")

    # Sync Hourly/Granular
    upsert_data(stress_ws, stress_col_a, process_stress_data(garmin, hourly_dates), STRESS_HEADERS)
    upsert_data(hr_ws, hr_col_a, process_hr_data(garmin, hourly_dates), HR_HEADERS)
    upsert_data(steps_ws, steps_col_a, process_steps_data(garmin, hourly_dates), STEPS_HEADERS)
    upsert_data(snap_ws, snap_col_a, fetch_health_snapshots(garmin, snap_dates), SNAPSHOTS_HEADERS)
    upsert_data(bb_ws, bb_col_a, process_body_battery_data(garmin, hourly_dates), BODY_BATTERY_HEADERS)

    # Detailed Sub-Sleep
    upsert_data(stages_ws, stages_col_a, process_sleep_stages(garmin, sub_sleep_dates), STAGES_HEADERS)
    upsert_data(breath_ws, breath_col_a, process_breathing_disruptions(garmin, sub_sleep_dates), BREATHING_HEADERS)
    upsert_data(restless_ws, restless_col_a, process_restless_moments(garmin, sub_sleep_dates), RESTLESS_HEADERS)
    upsert_data(move_ws, move_col_a, process_sleep_movement(garmin, sub_sleep_dates), MOVEMENT_HEADERS)

    logging.info("Garmin Sync complete.")

if __name__ == "__main__":
    main()
