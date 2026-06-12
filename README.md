# Idealist Job Scraper

Scrapes remote, salaried jobs from Idealist daily and upserts them into a Google Sheet.

## Setup

### 1. Create a GitHub repository

Push this folder to a new GitHub repo.

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `GOOGLE_SHEET_ID` | The ID from your Google Sheet URL: `docs.google.com/spreadsheets/d/<ID>/edit` |
| `GOOGLE_CREDENTIALS_JSON` | The full contents of your service account JSON key file (paste the entire JSON) |

### 3. Share your Google Sheet with the service account

In your Google Sheet, click **Share** and add the service account email (found in the JSON key as `client_email`) with **Editor** access.

### 4. First run

The sheet named **"Jobs"** will be created automatically on first run with a header row.
To trigger manually: **Actions tab → Daily Idealist Scrape → Run workflow**.

### 5. Schedule

The workflow runs at **9:00 AM UTC** daily. To change the time, edit the `cron` expression in `.github/workflows/daily_scrape.yml`.

Cron syntax: `minute hour day month weekday`
Examples:
- `0 14 * * *` = 2:00 PM UTC
- `0 9 * * 1-5` = 9:00 AM UTC weekdays only

## How it works

1. Playwright loads the Idealist search page and intercepts the internal search API call.
2. `httpx` paginates through all results via that API.
3. Each unique org profile page is fetched to enrich `company_*` fields.
4. Rows are upserted into the Google Sheet keyed on job `id` (no duplicates on re-runs).

## Troubleshooting

**"Could not intercept Idealist search API"** — Idealist changed their site structure. Open the Idealist jobs page in Chrome DevTools → Network → XHR and look for the search request. Update the URL pattern in `discover_api_endpoint()` in `scraper.py`.

**Rate limiting / 429 errors** — Increase `REQUEST_DELAY` in `scraper.py` (currently 1.2 seconds).

**Sheet not updating** — Confirm the service account email has Editor access to the sheet.
