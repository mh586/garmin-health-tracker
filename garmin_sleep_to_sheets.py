import base64
import json
import logging
import os
import sys
import time
from datetime import date, timedelta

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


def main():
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        logging.error("SPREADSHEET_ID is missing.")
        sys.exit(1)

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)

    # 1. Inspect Sleep sheet
    sleep_ws, sleep_col_a = sync_sheet(sh, os.environ.get("SHEET_NAME", "Sleep"), SLEEP_HEADERS)
    sleep_count = max(0, len(sleep_col_a) - 1)
    today = date.today()

    sleep_candidates = [today] if sleep_count >= 60 else [today - timedelta(days=i) for i in range(59, -1, -1)]
    sleep_dates_needed = [dt for dt in sleep_candidates if dt.strftime("%Y-%m-%d") not in sleep_col_a]

    # 2. Inspect Activities sheet
    act_ws, act_col_a = sync_sheet(sh, os.environ.get("ACTIVITIES_SHEET_NAME", "Activities"), ACT_HEADERS)
    act_count = max(0, len(act_col_a) - 1)
    act_start = today - timedelta(days=1 if act_count >= 60 else 59)

    # 3. Connect to Garmin
    garmin = get_garmin_client()

    # Sync Sleep Data
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

    # Sync Activities Data
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

    logging.info("Garmin Sleep and Activities sync completed successfully.")


if __name__ == "__main__":
    main()
