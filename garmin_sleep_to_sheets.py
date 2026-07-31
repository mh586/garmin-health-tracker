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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Header Definitions
SLEEP_HEADERS = [
    "date",
    "sleep_start_timestamp_local",
    "sleep_end_timestamp_local",
    "total_sleep_seconds",
    "deep_sleep_seconds",
    "light_sleep_seconds",
    "rem_sleep_seconds",
    "awake_seconds",
    "sleep_score",
    "sleep_score_qualifier",
    "avg_overnight_hrv",
    "avg_spo2_value",
    "avg_respiration_value",
    "resting_heart_rate",
    "body_battery_change",
]

ACT_HEADERS = [
    "activity_id",
    "activity_name",
    "activity_type",
    "start_time_local",
    "distance_meters",
    "duration_seconds",
    "elapsed_duration_seconds",
    "moving_duration_seconds",
    "average_speed_mps",
    "max_speed_mps",
    "calories",
    "average_hr",
    "max_hr",
    "steps",
    "elevation_gain_meters",
]

STRESS_HEADERS = ["date_hour", "min_stress", "max_stress", "avg_stress"]
HR_HEADERS = ["date_hour", "min_hr", "max_hr", "avg_hr"]
STEPS_HEADERS = ["date_hour", "total_steps"]

SNAPSHOTS_HEADERS = [
    "snapshot_id",
    "timestamp_local",
    "avg_hr",
    "avg_stress",
    "avg_respiration",
    "spo2",
    "rmssd",
    "sdnn",
]
STAGES_HEADERS = ["start_time_local", "end_time_local", "stage", "duration_seconds"]
MOVEMENT_HEADERS = ["date_hour", "min_movement", "max_movement", "avg_movement"]
BREATHING_HEADERS = ["timestamp_local", "respiration_rate"]
RESTLESS_HEADERS = ["timestamp_local", "restless_level"]


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
    except GarminConnectTooManyRequestsError:
        logging.error("Garmin rate limit reached (HTTP 429). Wait 2–4 hours.")
        sys.exit(1)
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "rate limit" in err_msg.lower():
            logging.error("Garmin rate limit reached (HTTP 429). Wait 2–4 hours.")
            sys.exit(1)
        if any(term in err_msg for term in ["MFA", "mfa", "2FA", "2fa", "MFARequired"]):
            logging.error("Garmin MFA is not supported in automated pipelines. Disable MFA on your Garmin account.")
            sys.exit(1)
        logging.error(f"Failed to authenticate with Garmin: {e}")
        sys.exit(1)


def get_gspread_client() -> gspread.Client:
    creds_path = "/tmp/gsa.json"
    if os.path.exists(creds_path):
        return gspread.service_account(filename=creds_path)

    raw_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not raw_creds:
        logging.error("GOOGLE_CREDENTIALS_JSON or /tmp/gsa.json is required.")
        sys.exit(1)

    try:
        creds_dict = json.loads(raw_creds)
    except json.JSONDecodeError:
        creds_dict = json.loads(base64.b64decode(raw_creds).decode("utf-8"))

    return gspread.service_account_from_dict(creds_dict)


def to_date_hour(ts):
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:00")
    if isinstance(ts, str):
        ts_clean = ts.replace("T", " ").split(".")[0]
        try:
            dt = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:00")
        except ValueError:
            pass
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

    data_keys = col_a[1:] if len(col_a) > 1 else []
    for key in data_keys:
        if not key:
            continue
        date_part = str(key).split(" ")[0].split("T")[0]
        try:
            parsed_dt = datetime.strptime(date_part, "%Y-%m-%d").date()
            recorded_dates.append(parsed_dt)
        except ValueError:
            continue

    if not recorded_dates:
        return [today - timedelta(days=i) for i in range(max_backfill_days - 1, -1, -1)]

    latest_date = max(recorded_dates)
    days_to_fetch = max(1, (today - latest_date).days)
    return [latest_date + timedelta(days=i) for i in range(days_to_fetch + 1)]


def upsert_data(ws, col_a, row_data_dict, headers):
    if not row_data_dict:
        return

    new_rows = []
    end_col = chr(ord("A") + len(headers) - 1)

    for key in sorted(row_data_dict.keys()):
        row_vals = row_data_dict[key]
        if key in col_a:
            idx = col_a.index(key) + 1
            ws.update(f"A{idx}:{end_col}{idx}", [row_vals])
            time.sleep(0.1)
            logging.info(f"Updated {ws.title} for key {key}")
        else:
            new_rows.append(row_vals)
            col_a.append(key)

    if new_rows:
        ws.append_rows(new_rows)
        time.sleep(0.1)
        logging.info(f"Appended {len(new_rows)} new rows to {ws.title}")


def parse_sleep_row(target_date: str, sleep_data: dict) -> list:
    dto = (sleep_data or {}).get("dailySleepDTO")
    if not dto:
        return []

    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") or {}

    row_dict = {
        "date": target_date,
        "sleep_start_timestamp_local": dto.get("sleepStartTimestampLocal") or dto.get("startTimestampLocal"),
        "sleep_end_timestamp_local": dto.get("sleepEndTimestampLocal") or dto.get("endTimestampLocal"),
        "total_sleep_seconds": dto.get("sleepTimeSeconds"),
        "deep_sleep_seconds": dto.get("deepSleepSeconds"),
        "light_sleep_seconds": dto.get("lightSleepSeconds"),
        "rem_sleep_seconds": dto.get("remSleepSeconds"),
        "awake_seconds": dto.get("awakeSleepSeconds"),
        "sleep_score": overall.get("value") or dto.get("sleepScore"),
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
        "activity_id": act.get("activityId"),
        "activity_name": act.get("activityName"),
        "activity_type": type_key,
        "start_time_local": act.get("startTimeLocal"),
        "distance_meters": act.get("distance"),
        "duration_seconds": act.get("duration"),
        "elapsed_duration_seconds": act.get("elapsedDuration"),
        "moving_duration_seconds": act.get("movingDuration"),
        "average_speed_mps": act.get("averageSpeed"),
        "max_speed_mps": act.get("maxSpeed"),
        "calories": act.get("calories"),
        "average_hr": act.get("averageHR"),
        "max_hr": act.get("maxHR"),
        "steps": act.get("steps"),
        "elevation_gain_meters": act.get("elevationGain"),
    }
    return [row_dict.get(h) if row_dict.get(h) is not None else "" for h in ACT_HEADERS]


def fetch_health_snapshots(garmin, dates):
    rows = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            if hasattr(garmin, "get_health_snapshot"):
                data = garmin.get_health_snapshot(date_str)
            else:
                data = garmin.connectapi(f"/healthsnapshot-service/snapshot/daily/{date_str}")
        except Exception as e:
            logging.error(f"Error fetching health snapshots for {date_str}: {e}")
            continue

        snapshots = data if isinstance(data, list) else (data.get("summaries") or data.get("snapshotList") or [])
        for item in snapshots:
            if isinstance(item, dict):
                snap_id = str(item.get("snapshotId") or item.get("summaryId") or item.get("startTimestampLocal") or item.get("startTimestampGMT"))
                if not snap_id:
                    continue

                ts = item.get("startTimestampLocal") or item.get("startTimestampGMT") or item.get("startTimeLocal")
                avg_hr = item.get("averageHeartRate") or item.get("avgHeartRate")
                avg_stress = item.get("averageStress") or item.get("avgStress")
                avg_resp = item.get("averageRespiration") or item.get("avgRespiration")
                spo2 = item.get("spo2") or item.get("averageSpo2")
                rmssd = item.get("rmssd") or item.get("hrvRmssd")
                sdnn = item.get("sdnn") or item.get("hrvSdnn")

                rows[snap_id] = [
                    snap_id,
                    ts or "",
                    avg_hr if avg_hr is not None else "",
                    avg_stress if avg_stress is not None else "",
                    avg_resp if avg_resp is not None else "",
                    spo2 if spo2 is not None else "",
                    rmssd if rmssd is not None else "",
                    sdnn if sdnn is not None else "",
                ]
    return rows


def process_sub_sleep_data(garmin, dates):
    stages_rows, breathing_rows, restless_rows = {}, {}, {}

    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_sleep_data(date_str) or {}
        except Exception as e:
            logging.error(f"Error fetching detailed sleep for {date_str}: {e}")
            continue

        # 1. Stages
        levels_map = data.get("sleepLevels") or (data.get("dailySleepDTO") or {}).get("sleepLevels") or {}
        stage_items = []
        if isinstance(levels_map, dict):
            for stage_name, items in levels_map.items():
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict):
                            it_copy = dict(it)
                            it_copy["stage"] = stage_name
                            stage_items.append(it_copy)
        elif isinstance(levels_map, list):
            stage_items = levels_map

        for it in stage_items:
            if isinstance(it, dict):
                start = it.get("startLocal") or it.get("startTimeLocal") or it.get("startGMT")
                end = it.get("endLocal") or it.get("endTimeLocal") or it.get("endGMT")
                stage = it.get("stage") or it.get("activityLevel") or it.get("level") or "unknown"
                dur = it.get("durationInSeconds") or it.get("duration") or 0
                if start:
                    key = str(start)
                    stages_rows[key] = [key, end or "", stage, dur]

        # 2. Breathing Disruption
        resp = data.get("sleepRespiration") or data.get("respirationValues") or data.get("epochRespiration") or []
        for it in resp if isinstance(resp, list) else []:
            if isinstance(it, dict):
                ts = it.get("startGMT") or it.get("startLocal") or it.get("timestamp")
                val = it.get("respirationRate") or it.get("value") or it.get("epochValue")
                if ts and val is not None:
                    key = str(ts)
                    breathing_rows[key] = [key, val]

        # 3. Restless Moments
        restless = data.get("restlessMoments") or data.get("sleepRestlessMoments") or (data.get("dailySleepDTO") or {}).get("restlessMoments") or []
        for it in restless if isinstance(restless, list) else []:
            if isinstance(it, dict):
                ts = it.get("startGMT") or it.get("startLocal") or it.get("timestamp")
                val = it.get("duration") or it.get("value") or 1
                if ts:
                    key = str(ts)
                    restless_rows[key] = [key, val]

    return stages_rows, breathing_rows, restless_rows


def process_sleep_movement(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_sleep_data(date_str) or {}
        except Exception as e:
            logging.error(f"Error fetching sleep movement for {date_str}: {e}")
            continue

        movements = data.get("sleepMovement") or data.get("sleepMovementValues") or (data.get("dailySleepDTO") or {}).get("sleepMovement") or []
        for it in movements if isinstance(movements, list) else []:
            ts, val = None, None
            if isinstance(it, dict):
                ts = it.get("startGMT") or it.get("startLocal") or it.get("timestamp") or it.get("startTimestampGMT")
                val = it.get("activityLevel") or it.get("value") or it.get("movementLevel") or 0
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                ts, val = it[0], it[1]

            if ts and val is not None and isinstance(val, (int, float)):
                dh = to_date_hour(ts)
                if dh:
                    hourly_buckets.setdefault(dh, []).append(val)

    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals:
            rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def process_stress_data(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_stress_data(date_str) or {}
        except Exception as e:
            logging.error(f"Error fetching stress for {date_str}: {e}")
            continue

        raw = data.get("stressValuesArray") or (data.get("userBodyMap") or {}).get("stressValuesArray") or []
        for item in raw:
            ts, val = None, None
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                ts, val = item[0], item[1]
            elif isinstance(item, dict):
                ts = item.get("startTimestampGMT") or item.get("timestamp") or item.get("startGMT")
                val = item.get("stressLevel") or item.get("value")

            if ts is not None and val is not None and isinstance(val, (int, float)) and val >= 0:
                dh = to_date_hour(ts)
                if dh:
                    hourly_buckets.setdefault(dh, []).append(val)

    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals:
            rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def process_hr_data(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_heart_rates(date_str) or {}
        except Exception as e:
            logging.error(f"Error fetching heart rates for {date_str}: {e}")
            continue

        raw = data.get("heartRateValues") or (data.get("userBodyMap") or {}).get("heartRateValues") or []
        for item in raw:
            ts, val = None, None
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                ts, val = item[0], item[1]
            elif isinstance(item, dict):
                ts = item.get("startTimestampGMT") or item.get("timestamp") or item.get("startGMT")
                val = item.get("heartRate") or item.get("value")

            if ts is not None and val is not None and isinstance(val, (int, float)) and val > 0:
                dh = to_date_hour(ts)
                if dh:
                    hourly_buckets.setdefault(dh, []).append(val)

    rows = {}
    for dh, vals in hourly_buckets.items():
        if vals:
            rows[dh] = [dh, min(vals), max(vals), round(sum(vals) / len(vals), 1)]
    return rows


def process_steps_data(garmin, dates):
    hourly_buckets = {}
    for dt in dates:
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_steps_data(date_str) or []
        except Exception as e:
            logging.error(f"Error fetching steps for {date_str}: {e}")
            continue

        raw = data if isinstance(data, list) else (data.get("stepItems") or data.get("stepsValuesArray") or [])
        for item in raw:
            ts, steps = None, 0
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                ts, steps = item[0], item[1]
            elif isinstance(item, dict):
                ts = item.get("startGMT") or item.get("startLocal") or item.get("timestamp")
                steps = item.get("steps") or item.get("value") or 0

            if ts is not None and isinstance(steps, (int, float)) and steps >= 0:
                dh = to_date_hour(ts)
                if dh:
                    hourly_buckets[dh] = hourly_buckets.get(dh, 0) + int(steps)

    rows = {}
    for dh, total_steps in hourly_buckets.items():
        rows[dh] = [dh, total_steps]
    return rows


def main():
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        logging.error("SPREADSHEET_ID is missing.")
        sys.exit(1)

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)

    # 1. Prepare Sheets
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

    # Calculate incremental date windows per dataset
    sleep_dates = get_incremental_dates(sleep_col_a, 60)
    act_dates = get_incremental_dates(act_col_a, 60)
    hourly_dates = get_incremental_dates(stress_col_a, 7)
    hr_dates = get_incremental_dates(hr_col_a, 7)
    steps_dates = get_incremental_dates(steps_col_a, 7)
    snap_dates = get_incremental_dates(snap_col_a, 60)
    stages_dates = get_incremental_dates(stages_col_a, 7)
    move_dates = get_incremental_dates(move_col_a, 7)
    breath_dates = get_incremental_dates(breath_col_a, 7)
    restless_dates = get_incremental_dates(restless_col_a, 7)

    # 2. Connect to Garmin
    garmin = get_garmin_client()

    # Sync Sleep Summary
    logging.info(f"Syncing Sleep summary ({len(sleep_dates)} date(s))...")
    sleep_rows = {}
    for dt in sleep_dates:
        t_date = dt.strftime("%Y-%m-%d")
        try:
            data = garmin.get_sleep_data(t_date)
        except Exception as e:
            logging.error(f"Error fetching sleep for {t_date}: {e}")
            continue
        row = parse_sleep_row(t_date, data)
        if row:
            sleep_rows[t_date] = row
        time.sleep(0.2)
    upsert_data(sleep_ws, sleep_col_a, sleep_rows, SLEEP_HEADERS)

    # Sync Activities
    act_start = min(act_dates)
    act_end = max(act_dates)
    logging.info(f"Syncing Activities from {act_start} to {act_end}...")
    try:
        activities = garmin.get_activities_by_date(act_start.strftime("%Y-%m-%d"), act_end.strftime("%Y-%m-%d")) or []
    except Exception as e:
        logging.error(f"Error fetching activities: {e}")
        activities = []

    act_rows = {}
    for act in activities:
        act_id = str(act.get("activityId"))
        act_rows[act_id] = parse_act_row(act)
    upsert_data(act_ws, act_col_a, act_rows, ACT_HEADERS)

    # Sync Hourly Datasets (Stress, HR, Steps)
    logging.info("Syncing Stress...")
    upsert_data(stress_ws, stress_col_a, process_stress_data(garmin, hourly_dates), STRESS_HEADERS)

    logging.info("Syncing Heart Rate...")
    upsert_data(hr_ws, hr_col_a, process_hr_data(garmin, hr_dates), HR_HEADERS)

    logging.info("Syncing Steps...")
    upsert_data(steps_ws, steps_col_a, process_steps_data(garmin, steps_dates), STEPS_HEADERS)

    # Sync Health Snapshots
    logging.info("Syncing Health Snapshots...")
    upsert_data(snap_ws, snap_col_a, fetch_health_snapshots(garmin, snap_dates), SNAPSHOTS_HEADERS)

    # Sync Sub-Sleep Breakdown
    logging.info("Syncing Sleep Stages...")
    st_rows, br_rows, rest_rows = process_sub_sleep_data(garmin, stages_dates)
    upsert_data(stages_ws, stages_col_a, st_rows, STAGES_HEADERS)

    logging.info("Syncing Sleep Movement (Hourly Summaries)...")
    upsert_data(move_ws, move_col_a, process_sleep_movement(garmin, move_dates), MOVEMENT_HEADERS)

    logging.info("Syncing Breathing Disruption & Restless Moments...")
    upsert_data(breath_ws, breath_col_a, br_rows, BREATHING_HEADERS)
    upsert_data(restless_ws, restless_col_a, rest_rows, RESTLESS_HEADERS)

    logging.info("Garmin Sync complete across all tabs.")


if __name__ == "__main__":
    main()
