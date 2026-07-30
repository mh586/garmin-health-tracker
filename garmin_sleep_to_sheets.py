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

        if isinstance(data, dict):
            raw = data.get("stepItems") or data.get("stepsValuesArray") or []
        elif isinstance(data, list):
            raw = data
        else:
            raw = []

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


def upsert_hourly_data(ws, col_a, row_data_dict, headers):
    new_rows = []
    end_col = chr(ord("A") + len(headers) - 1)

    for dh in sorted(row_data_dict.keys()):
        row_vals = row_data_dict[dh]
        if dh in col_a:
            idx = col_a.index(dh) + 1
            ws.update(f"A{idx}:{end_col}{idx}", [row_vals])
            logging.info(f"Updated {ws.title} for {dh}")
        else:
            new_rows.append(row_vals)
            col_a.append(dh)

    if new_rows:
        ws.append_rows(new_rows)
        logging.info(f"Appended {len(new_rows)} new hourly rows to {ws.title}")


def main():
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        logging.error("SPREADSHEET_ID is missing.")
        sys.exit(1)

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)
    today = date.today()

    # 1. Prepare Sleep sheet
    sleep_ws, sleep_col_a = sync_sheet(sh, os.environ.get("SHEET_NAME", "Sleep"), SLEEP_HEADERS)
    sleep_count = max(0, len(sleep_col_a) - 1)
    sleep_candidates = [today] if sleep_count >= 60 else [today - timedelta(days=i) for i in range(59, -1, -1)]
    sleep_dates_needed = [dt for dt in sleep_candidates if dt.strftime("%Y-%m-%d") not in sleep_col_a]

    # 2. Prepare Activities sheet
    act_ws, act_col_a = sync_sheet(sh, os.environ.get("ACTIVITIES_SHEET_NAME", "Activities"), ACT_HEADERS)
    act_count = max(0, len(act_col_a) - 1)
    act_start = today - timedelta(days=1 if act_count >= 60 else 59)

    # 3. Prepare Hourly Sheets (Stress, HR, Steps)
    stress_ws, stress_col_a = sync_sheet(sh, os.environ.get("STRESS_SHEET_NAME", "Stress"), STRESS_HEADERS)
    hr_ws, hr_col_a = sync_sheet(sh, os.environ.get("HR_SHEET_NAME", "HR"), HR_HEADERS)
    steps_ws, steps_col_a = sync_sheet(sh, os.environ.get("STEPS_SHEET_NAME", "Steps"), STEPS_HEADERS)

    # Determine hourly fetch dates (7 days backfill if new sheet, else yesterday + today)
    def get_hourly_dates(col_a):
        return [today - timedelta(days=i) for i in range(6, -1, -1)] if len(col_a) < 24 else [today - timedelta(days=1), today]

    stress_dates = get_hourly_dates(stress_col_a)
    hr_dates = get_hourly_dates(hr_col_a)
    steps_dates = get_hourly_dates(steps_col_a)

    # Connect to Garmin
    garmin = get_garmin_client()

    # Sync Sleep
    if sleep_dates_needed:
        logging.info(f"Fetching sleep data for {len(sleep_dates_needed)} date(s)...")
        for dt in sleep_dates_needed:
            t_date = dt.strftime("%Y-%m-%d")
            try:
                data = garmin.get_sleep_data(t_date)
            except Exception as e:
                logging.error(f"Error fetching sleep for {t_date}: {e}")
                continue

            row = parse_sleep_row(t_date, data)
            if row:
                if t_date in sleep_col_a:
                    idx = sleep_col_a.index(t_date) + 1
                    sleep_ws.update(f"A{idx}:O{idx}", [row])
                else:
                    sleep_ws.append_row(row)
                    sleep_col_a.append(t_date)
                logging.info(f"Saved sleep for {t_date}")
            time.sleep(0.2)
    else:
        logging.info("Sleep data is up to date.")

    # Sync Activities
    logging.info(f"Fetching activities from {act_start} to {today}...")
    try:
        activities = garmin.get_activities_by_date(act_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")) or []
    except Exception as e:
        logging.error(f"Error fetching activities: {e}")
        activities = []

    for act in activities:
        act_id = str(act.get("activityId"))
        row = parse_act_row(act)

        if act_id in act_col_a:
            idx = act_col_a.index(act_id) + 1
            act_ws.update(f"A{idx}:O{idx}", [row])
            logging.info(f"Updated activity {act_id}")
        else:
            act_ws.append_row(row)
            act_col_a.append(act_id)
            logging.info(f"Appended new activity {act_id}")

    # Sync Stress
    logging.info("Processing hourly stress data...")
    stress_rows = process_stress_data(garmin, stress_dates)
    upsert_hourly_data(stress_ws, stress_col_a, stress_rows, STRESS_HEADERS)

    # Sync Heart Rate
    logging.info("Processing hourly heart rate data...")
    hr_rows = process_hr_data(garmin, hr_dates)
    upsert_hourly_data(hr_ws, hr_col_a, hr_rows, HR_HEADERS)

    # Sync Steps
    logging.info("Processing hourly steps data...")
    steps_rows = process_steps_data(garmin, steps_dates)
    upsert_hourly_data(steps_ws, steps_col_a, steps_rows, STEPS_HEADERS)

    logging.info("Garmin Sync (Sleep, Activities, Stress, HR, Steps) completed successfully.")


if __name__ == "__main__":
    main()
