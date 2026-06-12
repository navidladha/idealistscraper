"""
Idealist job scraper — calls Apify actor, deduplicates, writes new jobs to Google Sheets.
"""

import json
import os
from datetime import datetime, timezone

import gspread
import httpx
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

APIFY_TOKEN = os.environ["APIFY_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

APIFY_ENDPOINT = (
    "https://api.apify.com/v2/acts/santamaria-automations~idealist-scraper"
    "/run-sync-get-dataset-items"
)

APIFY_INPUT = {
    "searchUrls": ["https://www.idealist.org/en/jobs?hasSalary=true&locationType=REMOTE"],
    "maxResultsPerQuery": 500,
    "maxResults": 0,
    "includeCompanyInfo": True,
    "maxConcurrency": 5,
}

SHEET_COLUMNS = [
    "id", "title", "company", "company_logo_url", "company_profile_url",
    "company_id", "company_website", "company_description", "company_address",
    "company_phone", "company_email", "company_joined", "company_job_count",
    "location", "city", "state", "country", "location_type", "remote_ok",
    "tags", "areas_of_focus", "job_type", "org_type", "professional_level",
    "education", "description_full", "contact_email", "contact_phone",
    "contact_website", "apply_url", "application_deadline",
    "salary_min", "salary_max", "salary_currency", "salary_period",
    "salary_text", "salary_notes", "is_active", "posted_at",
    "source_url", "source_platform", "search_query", "scraped_at",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def clean(val):
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    if val is None:
        return ""
    return str(val)

# ── Apify ─────────────────────────────────────────────────────────────────────

def fetch_from_apify() -> list[dict]:
    print("Calling Apify actor...")
    with httpx.Client(timeout=300) as client:
        resp = client.post(
            APIFY_ENDPOINT,
            params={"token": APIFY_TOKEN},
            json=APIFY_INPUT,
        )
        resp.raise_for_status()
        jobs = resp.json()
    print(f"Apify returned {len(jobs)} jobs.")
    return jobs

# ── Google Sheets ──────────────────────────────────────────────────────────────

def get_sheet():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet("Jobs")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Jobs", rows=10000, cols=len(SHEET_COLUMNS))
        ws.append_row(SHEET_COLUMNS)
    return ws

def get_existing_ids(ws) -> set:
    col_values = ws.col_values(1)  # column A = id
    return set(col_values[1:])     # skip header

def append_new_jobs(ws, jobs: list[dict], existing_ids: set):
    scraped_at = now_iso()
    new_rows = []

    for job in jobs:
        job_id = str(job.get("id") or "")
        if not job_id or job_id in existing_ids:
            continue
        row = [clean(job.get(col, "")) for col in SHEET_COLUMNS]
        # Stamp scraped_at if not already set by Apify
        scraped_at_idx = SHEET_COLUMNS.index("scraped_at")
        if not row[scraped_at_idx]:
            row[scraped_at_idx] = scraped_at
        new_rows.append(row)

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
        print(f"Added {len(new_rows)} new jobs.")
    else:
        print("No new jobs found.")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{now_iso()}] Starting scrape...")
    jobs = fetch_from_apify()
    ws = get_sheet()
    existing_ids = get_existing_ids(ws)
    print(f"Sheet has {len(existing_ids)} existing jobs.")
    append_new_jobs(ws, jobs, existing_ids)
    print("Done.")

if __name__ == "__main__":
    main()
