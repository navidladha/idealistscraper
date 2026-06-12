"""
Idealist job scraper — no browser required.
Calls Idealist's internal search API directly with httpx.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import gspread
import httpx
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

SOURCE_PLATFORM = "idealist.org"
BASE_URL = "https://www.idealist.org"
SEARCH_QUERY = "https://www.idealist.org/en/jobs?hasSalary=true&locationType=REMOTE"

REQUEST_DELAY = 1.5
PAGE_SIZE = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.idealist.org/en/jobs",
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

# Known API endpoint candidates (tried in order)
API_CANDIDATES = [
    "https://www.idealist.org/api/v1/listings",
    "https://www.idealist.org/api/listings",
    "https://www.idealist.org/api/v2/listings",
    "https://www.idealist.org/api/jobs",
]

SEARCH_PARAMS = {
    "type": "JOB",
    "hasSalary": "true",
    "locationType": "REMOTE",
    "page": 0,
    "count": PAGE_SIZE,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(val: Any) -> str:
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    if val is None:
        return ""
    return str(val)


def extract_email(text: str) -> str:
    m = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", text or "")
    return m.group(0) if m else ""


def extract_phone(text: str) -> str:
    m = re.search(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}", text or "")
    return m.group(0) if m else ""


def extract_website(text: str) -> str:
    m = re.search(r"https?://[^\s)>\"]+", text or "")
    return m.group(0) if m else ""


def format_salary(min_: Any, max_: Any, period: str) -> str:
    if not min_ and not max_:
        return ""
    def fmt(n):
        try:
            n = int(n)
            return f"${n//1000}k" if n >= 1000 else f"${n}"
        except Exception:
            return str(n)
    parts = [fmt(min_), fmt(max_)] if min_ and max_ else [fmt(min_ or max_)]
    return f"{' - '.join(parts)}/{period}" if period else " - ".join(parts)


def extract_listings(data: dict) -> list:
    for key in ("hits", "listings", "jobs", "results", "data", "items"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


def extract_total(data: dict) -> int:
    for key in ("total", "totalCount", "nbHits", "count", "totalResults", "totalItems"):
        val = data.get(key)
        if isinstance(val, int):
            return val
    return 0

# ── API discovery ──────────────────────────────────────────────────────────────

async def find_api(client: httpx.AsyncClient) -> tuple[str | None, dict | None]:
    """Try known API endpoints until one returns job listings."""
    for url in API_CANDIDATES:
        try:
            resp = await client.get(url, params=SEARCH_PARAMS)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and extract_listings(data):
                    print(f"Found API: {url}")
                    return url, data
        except Exception as e:
            print(f"  {url} failed: {e}")
        await asyncio.sleep(0.5)

    # Last resort: scrape the page HTML and look for embedded JSON
    print("Trying embedded JSON fallback...")
    try:
        resp = await client.get(
            SEARCH_QUERY,
            headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
        )
        soup = BeautifulSoup(resp.text, "html.parser")

        # Next.js / React apps often embed data in <script id="__NEXT_DATA__">
        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            nd = json.loads(script.string)
            # Walk the tree looking for a list of job objects
            text = json.dumps(nd)
            if '"title"' in text and '"organization"' in text:
                print("Found embedded JSON in __NEXT_DATA__")
                return "embedded", nd

        # Look for window.__INITIAL_STATE__ or similar
        for tag in soup.find_all("script"):
            src = tag.string or ""
            if "window.__" in src and "title" in src:
                m = re.search(r"window\.__\w+__\s*=\s*(\{.+?\});", src, re.DOTALL)
                if m:
                    data = json.loads(m.group(1))
                    print("Found embedded window state")
                    return "embedded", data
    except Exception as e:
        print(f"Embedded fallback failed: {e}")

    return None, None

# ── Pagination ─────────────────────────────────────────────────────────────────

async def fetch_all(client: httpx.AsyncClient, api_url: str, first_data: dict) -> list[dict]:
    all_listings = extract_listings(first_data)
    total = extract_total(first_data)
    print(f"Page 0: {len(all_listings)} listings (total ~{total})")

    if api_url == "embedded":
        return all_listings  # embedded data has no pagination via API

    page = 1
    while len(all_listings) < total and page < 100:
        params = {**SEARCH_PARAMS, "page": page}
        resp = await client.get(api_url, params=params)
        resp.raise_for_status()
        data = resp.json()
        page_items = extract_listings(data)
        if not page_items:
            break
        all_listings.extend(page_items)
        print(f"Page {page}: {len(page_items)} listings")
        page += 1
        await asyncio.sleep(REQUEST_DELAY)

    return all_listings

# ── Org enrichment ─────────────────────────────────────────────────────────────

async def enrich_org(client: httpx.AsyncClient, org_slug: str) -> dict:
    empty = {k: "" for k in [
        "company_website", "company_description", "company_address",
        "company_phone", "company_email", "company_joined", "company_job_count",
    ]}
    if not org_slug:
        return empty

    url = f"{BASE_URL}/en/org/{org_slug}"
    try:
        resp = await client.get(url, headers={**HEADERS, "Accept": "text/html"})
        if resp.status_code != 200:
            return empty
        soup = BeautifulSoup(resp.text, "html.parser")

        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            nd = json.loads(script.string)
            org = (
                nd.get("props", {}).get("pageProps", {}).get("org") or
                nd.get("props", {}).get("pageProps", {}).get("organization") or {}
            )
            if org:
                joined = org.get("joinedAt") or org.get("createdAt", "")
                try:
                    dt = datetime.fromisoformat(str(joined).replace("Z", "+00:00"))
                    joined = dt.strftime("%B %Y")
                except Exception:
                    pass
                return {
                    "company_website": org.get("url") or org.get("website", ""),
                    "company_description": org.get("description") or org.get("about", ""),
                    "company_address": org.get("location") or org.get("address", ""),
                    "company_phone": org.get("phone", ""),
                    "company_email": org.get("email", ""),
                    "company_joined": joined,
                    "company_job_count": org.get("activeJobCount", ""),
                }
    except Exception as e:
        print(f"  Org enrich error ({org_slug}): {e}")
    return empty

# ── Job parsing ────────────────────────────────────────────────────────────────

def parse_job(raw: dict, org_data: dict, scraped_at: str) -> dict:
    org = raw.get("organization") or raw.get("org") or {}
    salary = raw.get("salary") or raw.get("compensation") or {}
    loc = raw.get("location") if isinstance(raw.get("location"), dict) else {}

    job_id = str(raw.get("id") or raw.get("listingId") or raw.get("slug") or "")
    slug = raw.get("slug") or job_id

    desc_html = raw.get("description") or raw.get("descriptionHtml") or ""
    desc_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc_html)).strip()

    salary_min = salary.get("min") or salary.get("minimum") or raw.get("salaryMin")
    salary_max = salary.get("max") or salary.get("maximum") or raw.get("salaryMax")
    salary_currency = salary.get("currency") or raw.get("salaryCurrency") or "USD"
    salary_period = (salary.get("period") or salary.get("frequency") or raw.get("salaryPeriod") or "").lower()
    salary_notes = salary.get("notes") or salary.get("additionalInfo") or raw.get("salaryNotes") or ""

    location_type = (raw.get("locationType") or raw.get("workplaceType") or "").lower()
    remote_ok = location_type == "remote" or bool(raw.get("isRemote"))

    city = loc.get("city") or raw.get("city") or ""
    state = loc.get("state") or loc.get("stateCode") or raw.get("state") or ""
    country = loc.get("country") or loc.get("countryCode") or raw.get("country") or ""
    full_location = raw.get("location") if isinstance(raw.get("location"), str) else ", ".join(filter(None, [city, state, country]))

    org_id = str(org.get("id") or org.get("orgId") or raw.get("orgId") or "")
    org_slug = org.get("slug") or org_id
    company_profile_url = f"{BASE_URL}/en/org/{org_slug}" if org_slug else ""

    deadline = raw.get("applicationDeadline") or raw.get("deadline") or ""
    if deadline and "T" in str(deadline):
        try:
            dt = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
            deadline = dt.strftime("%B %-d, %Y")
        except Exception:
            pass

    posted_raw = raw.get("publishedAt") or raw.get("postedAt") or raw.get("createdAt") or ""
    try:
        posted_at = datetime.fromisoformat(str(posted_raw).replace("Z", "+00:00")).isoformat()
    except Exception:
        posted_at = str(posted_raw)

    return {
        "id": job_id,
        "title": raw.get("title") or raw.get("name") or "",
        "company": org.get("name") or raw.get("orgName") or "",
        "company_logo_url": org.get("logoUrl") or org.get("logo") or "",
        "company_profile_url": company_profile_url,
        "company_id": org_id,
        **org_data,
        "location": full_location,
        "city": city,
        "state": state,
        "country": country,
        "location_type": location_type,
        "remote_ok": remote_ok,
        "tags": ", ".join(raw.get("functions") or raw.get("tags") or []),
        "areas_of_focus": ", ".join(raw.get("areasOfFocus") or raw.get("impactAreas") or []),
        "job_type": ", ".join(raw.get("employmentType") or raw.get("jobType") or []),
        "org_type": org.get("type") or org.get("orgType") or raw.get("orgType") or "",
        "professional_level": raw.get("professionalLevel") or raw.get("experienceLevel") or "",
        "education": raw.get("education") or raw.get("educationRequirement") or "",
        "description_full": desc_text,
        "contact_email": extract_email(desc_text) or raw.get("contactEmail") or "",
        "contact_phone": extract_phone(desc_text),
        "contact_website": extract_website(desc_text),
        "apply_url": raw.get("applicationUrl") or raw.get("applyUrl") or raw.get("externalUrl") or "",
        "application_deadline": str(deadline),
        "salary_min": salary_min or "",
        "salary_max": salary_max or "",
        "salary_currency": salary_currency,
        "salary_period": salary_period,
        "salary_text": format_salary(salary_min, salary_max, salary_period),
        "salary_notes": str(salary_notes),
        "is_active": True,
        "posted_at": posted_at,
        "source_url": f"{BASE_URL}/en/job/{slug}" if slug else "",
        "source_platform": SOURCE_PLATFORM,
        "search_query": SEARCH_QUERY,
        "scraped_at": scraped_at,
    }

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
        ws = sh.add_worksheet("Jobs", rows=5000, cols=len(SHEET_COLUMNS))
        ws.append_row(SHEET_COLUMNS)
    return ws


def upsert_to_sheet(ws, jobs: list[dict]):
    existing = ws.get_all_records(expected_headers=SHEET_COLUMNS)
    existing_ids = {str(row.get("id", "")): i + 2 for i, row in enumerate(existing)}

    rows_to_add = []
    updates = []

    for job in jobs:
        row = [clean(job.get(col, "")) for col in SHEET_COLUMNS]
        job_id = str(job["id"])
        if job_id in existing_ids:
            updates.append({"range": f"A{existing_ids[job_id]}", "values": [row]})
        else:
            rows_to_add.append(row)

    if updates:
        ws.batch_update(updates)
        print(f"Updated {len(updates)} existing rows.")
    if rows_to_add:
        ws.append_rows(rows_to_add, value_input_option="RAW")
        print(f"Added {len(rows_to_add)} new rows.")

# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    scraped_at = now_iso()
    print(f"[{scraped_at}] Starting Idealist scrape...")

    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        api_url, first_data = await find_api(client)

        if not api_url or not first_data:
            raise RuntimeError(
                "Could not find Idealist API. "
                "Check network access or inspect idealist.org for API changes."
            )

        raw_listings = await fetch_all(client, api_url, first_data)
        print(f"Total fetched: {len(raw_listings)}")

        org_cache: dict[str, dict] = {}
        jobs = []

        for i, raw in enumerate(raw_listings):
            org = raw.get("organization") or raw.get("org") or {}
            org_slug = org.get("slug") or str(org.get("id") or "")

            if org_slug not in org_cache:
                print(f"  Enriching org {i+1}/{len(raw_listings)}: {org.get('name', org_slug)}")
                org_cache[org_slug] = await enrich_org(client, org_slug)
                await asyncio.sleep(REQUEST_DELAY)

            jobs.append(parse_job(raw, org_cache[org_slug], scraped_at))

    print(f"Writing {len(jobs)} jobs to Google Sheets...")
    ws = get_sheet()
    upsert_to_sheet(ws, jobs)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
