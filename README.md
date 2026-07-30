# Setup Instructions

## 1. Google Sheets Setup
1. Create a Google Sheet and name the target sheet tab `Sleep`.
2. Open [Google Cloud Console](https://console.cloud.google.com/).
3. Create a project and enable the **Google Sheets API** and **Google Drive API**.
4. Navigate to **IAM & Admin > Service Accounts** and create a Service Account.
5. Create a new **JSON key** for the Service Account and download it.
6. Copy the Service Account email address.
7. Open your Google Sheet, click **Share**, and grant **Editor** access to the Service Account email.
8. Copy the **Spreadsheet ID** from the sheet URL: `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`.

## 2. Garmin Account Setup
1. Disable Multi-Factor Authentication (MFA) on your Garmin Connect account settings.

## 3. Encode Credentials File
Encode your downloaded Service Account JSON key to Base64:
- **macOS / Linux**: `base64 -i path/to/key.json`
- **Windows (PowerShell)**: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("path\to\key.json"))`

## 4. GitHub Secrets Setup
In your GitHub repository, go to **Settings > Secrets and variables > Actions** and add:

- `GARMIN_EMAIL`: Your Garmin account email.
- `GARMIN_PASSWORD`: Your Garmin account password.
- `GOOGLE_CREDENTIALS_JSON`: The Base64 string from step 3.
- `SPREADSHEET_ID`: The Spreadsheet ID from step 1.
- `SHEET_NAME`: (Optional) `Sleep`

## 5. Manual Execution Test
1. Go to the **Actions** tab in GitHub.
2. Select **Garmin Sleep Sync**.
3. Click **Run workflow** -> **Run workflow**.
