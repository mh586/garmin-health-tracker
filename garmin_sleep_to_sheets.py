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

HEADERS = [
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


def get_garmin_client() -> Garmin:
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    token_dir = "/tmp/garmin_tokens"

    if not email or not password:
        logging.error("GARMIN_EMAIL and GARMIN_PASSWORD environment variables are required.")
        sys.exit(1)

    try:
        garmin = Garmin(email=email, password=password)
        logging.info("Authenticating with Garmin Connect...")
        garmin.login(tokenstore=token_dir)
        return garmin
    except GarminConnectTooManyRequestsError:
        logging.error("Garmin API rate limit reached (HTTP 429). Wait 2–4 hours before re-running.")
        sys.exit(1)
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "rate limit" in err_msg.lower():
            logging.error("Garmin API rate limit reached (HTTP 429). Wait 2–4 hours before re-running.")
            sys.exit(1)
        if any(term in err_msg for term in ["MFA", "mfa", "2FA", "2fa", "MFARequired"]):
            logging.error("Garmin MFA is not supported in automated pipelines. Disable MFA on your Garmin account.")
            sys.exit(1)
        logging.error(f"Failed to authenticate with Garmin Connect: {e}")
        sys.exit(1)


def get_gspread_client() -> gspread.Client:
    creds_path = "/tmp/gsa.json"
    if os.path.exists(creds_path):
        return gspread.service_account(filename=creds_path)

    raw_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not raw_creds:
        logging.error("GOOGLE_CREDENTIALS_JSON environment variable or /tmp/gsa.json is required.")
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
    overall_score = scores.get("overall") or {}

    row_dict = {
        "date": target_date,
        "sleep_start_timestamp_local": dto.get("sleepStartTimestampLocal") or dto.get("startTimestampLocal"),
        "sleep_end_timestamp_local": dto.get("sleepEndTimestampLocal") or dto.get("endTimestampLocal"),
        "total_sleep_seconds": dto.get("sleepTimeSeconds"),
        "deep_sleep_seconds": dto.get("deepSleepSeconds"),
        "light_sleep_seconds": dto.get("lightSleepSeconds"),
        "rem_sleep_seconds": dto.get("remSleepSeconds"),
        "awake_seconds": dto.get("awakeSleepSeconds"),
        "sleep_score": overall_score.get("value") or dto.get("sleepScore"),
        "sleep_score_qualifier": overall_score.get("qualifierKey") or dto.get("sleepScoreQualifier"),
        "avg_overnight_hrv": sleep_data.get("avgOvernightHrv") or dto.get("avgOvernightHrv"),
        "avg_spo2_value": sleep_data.get("averageSpO2Value") or dto.get("averageSpO2Value"),
        "avg_respiration_value": sleep_data.get("averageRespirationValue") or dto.get("averageRespirationValue"),
        "resting_heart_rate": sleep_data.get("restingHeartRate") or dto.get("restingHeartRate"),
        "body_battery_change": sleep_data.get("bodyBatteryChange") or dto.get("bodyBatteryChange"),
    }
    return [row_dict.get(h) if row_dict.get(h) is not None else "" for h in HEADERS]


def main():
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    sheet_name = os.environ.get("SHEET_NAME", "Sleep")
    if not spreadsheet_id:
        logging.error("SPREADSHEET_ID environment variable is missing.")
        sys.exit(1)

    # 1. Connect to Google Sheets first to check existing records
    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)

    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows="100", cols="20")

    existing_headers = worksheet.row_values(1)
    if not existing_headers:
        worksheet.append_row(HEADERS)
        col_a = [HEADERS[0]]
    else:
        col_a = worksheet.col_values(1)

    data_rows_count = max(0, len(col_a) - 1)
    today = date.today()

    # 2. Determine candidate dates to pull
    if data_rows_count >= 60:
        candidate_dates = [today]
    else:
        candidate_dates = [today - timedelta(days=i) for i in range(59, -1, -1)]

    # 3. Filter out dates that are already recorded in Google Sheet
    dates_to_fetch = [dt for dt in candidate_dates if dt.strftime("%Y-%m-%d") not in col_a]

    if not dates_to_fetch:
        logging.info("Target sleep data is already recorded in Google Sheet. Skipping Garmin API pull.")
        sys.exit(0)

    logging.info(f"Found {len(dates_to_fetch)} missing date(s). Connecting to Garmin to fetch...")

    # 4. Authenticate with Garmin ONLY when missing data needs to be pulled
    garmin = get_garmin_client()

    for dt in dates_to_fetch:
        target_date = dt.strftime("%Y-%m-%d")
        try:
            sleep_data = garmin.get_sleep_data(target_date)
        except Exception as e:
            logging.error(f"Error fetching sleep data for {target_date}: {e}")
            continue

        row_values = parse_sleep_row(target_date, sleep_data)
        if not row_values:
            logging.warning(f"No sleep data returned from Garmin for {target_date}.")
            continue

        if target_date in col_a:
            row_idx = col_a.index(target_date) + 1
            worksheet.update(f"A{row_idx}:O{row_idx}", [row_values])
            logging.info(f"Updated row {row_idx} for date {target_date}.")
        else:
            worksheet.append_row(row_values)
            col_a.append(target_date)
            logging.info(f"Appended new row for date {target_date}.")

        if len(dates_to_fetch) > 1:
            time.sleep(0.2)


if __name__ == "__main__":
    main()
