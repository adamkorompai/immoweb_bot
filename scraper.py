"""
Immoweb Scraper — Ixelles / Phone Number Filter → Telegram
Runs every 30 minutes automatically using APScheduler.
"""

import json
import logging
import os
import re
import time
import random
from pathlib import Path
from datetime import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials
from scrapling.fetchers import StealthySession
from apscheduler.schedulers.blocking import BlockingScheduler

# ─────────────────────────────────────────────
#  CONFIG — edit these before running
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
GOOGLE_SHEET_ID     = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS  = os.environ.get("GOOGLE_CREDENTIALS", "")

SEEN_FILE = Path("seen_listings.json")            # tracks already-sent listing IDs
INTERVAL_MINUTES = 30                             # how often to check

# Immoweb search URLs — toutes les communes de Bruxelles, apparts + maisons
_COMMUNES = [
    ("anderlecht",          "1070"),
    ("auderghem",           "1160"),
    ("berchem-ste-agathe",  "1082"),
    ("bruxelles",           "1000"),
    ("etterbeek",           "1040"),
    ("evere",               "1140"),
    ("forest",              "1190"),
    ("ganshoren",           "1083"),
    ("ixelles",             "1050"),
    ("jette",               "1090"),
    ("koekelberg",          "1081"),
    ("molenbeek-saint-jean","1080"),
    ("saint-gilles",        "1060"),
    ("saint-josse-ten-noode","1210"),
    ("schaerbeek",          "1030"),
    ("uccle",               "1180"),
    ("watermael-boitsfort", "1170"),
    ("woluwe-saint-lambert","1200"),
    ("woluwe-saint-pierre", "1150"),
]
SEARCH_URLS = [
    f"https://www.immoweb.be/fr/recherche/{prop_type}/a-louer/{commune}/{zip_}?orderBy=newest"
    for commune, zip_ in _COMMUNES
    for prop_type in ("appartement", "maison")
]

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scraper.log"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  PHONE NUMBER DETECTION (Belgian formats)
# ─────────────────────────────────────────────
PHONE_PATTERNS = [
    # Mobile: 04xx xx xx xx / +32 4xx xx xx xx
    r"(?:\+32\s?|0032\s?)4[5-9]\d[\s.\-]?\d{2}[\s.\-]?\d{2}[\s.\-]?\d{2}|(?<!\d)04[5-9]\d[\s.\-]?\d{2}[\s.\-]?\d{2}[\s.\-]?\d{2}(?!\d)",
    # Landline Brussels: 02 xxx xx xx
    r"(?:\+32\s?|0032\s?)2[\s.\-]?\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}|(?<!\d)02[\s.\-]?\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}(?!\d)",
    # Other landlines: 0x xxx xx xx
    r"(?<!\d)0[1-9][\s.\-]?\d{2,3}[\s.\-]?\d{2}[\s.\-]?\d{2}(?!\d)",
]
PHONE_REGEX = re.compile("|".join(PHONE_PATTERNS))


def extract_phone_numbers(text: str) -> list[str]:
    """Find all Belgian phone numbers in a block of text."""
    matches = PHONE_REGEX.findall(text)
    cleaned = []
    for m in matches:
        m = m.strip()
        if m and len(re.sub(r"\D", "", m)) >= 8:
            cleaned.append(m)
    return list(dict.fromkeys(cleaned))


# ─────────────────────────────────────────────
#  SEEN LISTINGS (JSON persistence with 30-day TTL)
# ─────────────────────────────────────────────
SEEN_TTL_DAYS = 30


def load_seen() -> dict:
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        if isinstance(data, list):  # migrate old format
            data = {lid: 0 for lid in data}
        cutoff = time.time() - SEEN_TTL_DAYS * 86400
        return {lid: ts for lid, ts in data.items() if ts > cutoff}
    return {}


def save_seen(seen: dict):
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


# ─────────────────────────────────────────────
#  GOOGLE SHEETS
# ─────────────────────────────────────────────
SHEET_HEADERS = ["Date", "Commune", "Type", "Prix (€)", "Chambres", "Surface (m²)", "Téléphone", "Lien", "Appelé", "Répondu", "Notes"]

def get_sheet():
    if not GOOGLE_CREDENTIALS or not GOOGLE_SHEET_ID:
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        return client.open_by_key(GOOGLE_SHEET_ID).sheet1
    except Exception as e:
        log.error(f"Google Sheets connection failed: {e}")
        return None


def append_to_sheet(listing: dict):
    sheet = get_sheet()
    if sheet is None:
        return
    try:
        if not sheet.row_values(1):
            sheet.append_row(SHEET_HEADERS)
        sheet.append_row([
            datetime.now().strftime("%d/%m/%Y %H:%M"),
            listing.get("locality", ""),
            listing.get("type", ""),
            listing.get("price", ""),
            listing.get("bedrooms", ""),
            listing.get("area", ""),
            " | ".join(listing.get("phones", [])),
            f'=HYPERLINK("{listing.get("url", "")}";"Voir annonce")',
            "", "", "",  # Appelé, Répondu, Notes
        ], value_input_option="USER_ENTERED")
        log.info("Row appended to Google Sheet.")
    except Exception as e:
        log.error(f"Google Sheet append failed: {e}")


# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent.")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ─────────────────────────────────────────────
#  IMMOWEB SCRAPING
# ─────────────────────────────────────────────
def fetch_search_results(session) -> list[dict]:
    """
    Fetch all 38 Immoweb search pages (19 communes × appartement + maison)
    and return deduplicated listings sorted by ID descending (newest first).
    """
    seen_ids: set[str] = set()
    listings: list[dict] = []

    for url in SEARCH_URLS:
        log.info(f"Fetching {url}")
        try:
            page = session.fetch(url, network_idle=True)
            if page.status != 200:
                log.warning(f"HTTP {page.status} — skipping {url}")
                continue
        except Exception as e:
            log.warning(f"Fetch error for {url}: {e}")
            continue

        cards = page.css("article[id^='classified_']")
        log.info(f"  → {len(cards)} cards")

        for card in cards:
            listing_id = card.attrib.get("id", "").replace("classified_", "")
            if not listing_id or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            link = card.css("h2.card--result__title a")
            if not link:
                continue
            listing_url = link[0].attrib.get("href", "")

            # Type, locality, zip from the canonical listing URL
            url_m = re.search(r'/classified/([^/]+)/(?:for-rent|a-louer)/([^/]+)/(\d+)/\d+', listing_url)
            if url_m:
                prop_type = url_m.group(1).replace('-', ' ').title()
                locality   = url_m.group(2).replace('-', ' ').title()
                zip_code   = url_m.group(3)
            else:
                prop_type, locality, zip_code = "?", "?", ""

            # Price: screen-reader span inside the price block
            price = "N/A"
            price_el = card.css("p.card--result__price .sr-only")
            if price_el:
                price_text = (price_el[0].text or "").strip()
                pm = re.search(r'(\d[\d,]*)', price_text.replace(',', ''))
                if pm:
                    price = int(pm.group(1))

            # Bedrooms: screen-reader span in property info
            bedrooms = "?"
            for el in card.css(".card__information--property .sr-only"):
                t = (el.text or "").strip()
                bm = re.search(r'(\d+)\s*(?:bedroom|chambre)', t)
                if bm:
                    bedrooms = int(bm.group(1))
                    break

            # Area: direct text node before m²
            area = "?"
            for t in card.css(".card__information--property::text").getall():
                t = t.strip()
                if t and t.isdigit() and len(t) >= 2:
                    area = t
                    break

            is_private = len(card.css(".card--result__agency-logo")) == 0

            listings.append({
                "id": listing_id,
                "url": listing_url,
                "price": price,
                "locality": locality,
                "zip": zip_code,
                "type": prop_type,
                "bedrooms": bedrooms,
                "area": area,
                "is_private": is_private,
            })

    log.info(f"Total: {len(listings)} listings across all communes.")
    return listings


def fetch_listing_detail(listing: dict, session) -> dict:
    """
    Fetch the individual listing page and extract phone numbers.
    Prefers window.classified JSON; falls back to regex on visible text.
    """
    time.sleep(random.uniform(2, 5))
    try:
        page = session.fetch(listing["url"], network_idle=True)
        if page.status != 200:
            log.warning(f"HTTP {page.status} for listing {listing['id']}")
            return listing
    except Exception as e:
        log.warning(f"Failed to fetch listing {listing['id']}: {e}")
        return listing

    phones = []

    # Primary: extract from window.classified JSON in script tags
    for script in page.find_all("script"):
        js = script.text or ""
        if "window.classified" not in js:
            continue
        m = re.search(r'window\.classified\s*=\s*(\{.+?\});\s*\n', js, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                for customer in data.get("customers", []):
                    for field in ("phoneNumber", "mobileNumber"):
                        number = customer.get(field)
                        if number:
                            phones.append(number)
            except Exception:
                pass
        break

    # Fallback: regex scan of all visible text on the page
    if not phones:
        all_text = " ".join(page.css("body ::text").getall())
        phones = extract_phone_numbers(all_text)

    listing["phones"] = list(dict.fromkeys(phones))
    return listing


# ─────────────────────────────────────────────
#  FORMAT TELEGRAM MESSAGE
# ─────────────────────────────────────────────
def format_message(listing: dict) -> str:
    phones_str = " | ".join(listing.get("phones", []))
    price = listing.get("price", "N/A")
    locality = listing.get("locality", "")
    zip_code = listing.get("zip", "")
    bedrooms = listing.get("bedrooms", "?")
    area = listing.get("area", "?")
    prop_type = listing.get("type", "").replace("_", " ").title()
    url = listing.get("url", "")
    is_private = listing.get("is_private", False)
    owner_tag = "👤 Private owner" if is_private else "🏢 Agency"

    lines = [
        f"🏠 <b>New listing — {locality} {zip_code}</b>",
        f"💶 <b>Price:</b> {price}€/month",
        f"🛏 <b>Bedrooms:</b> {bedrooms} | 📐 <b>Area:</b> {area} m²",
        f"📋 <b>Type:</b> {prop_type}",
        f"{owner_tag}",
        f"📞 <b>Phone:</b> <code>{phones_str}</code>",
        f"🔗 <a href=\"{url}\">View listing</a>",
        f"<i>🕐 Found at {datetime.now().strftime('%d/%m/%Y %H:%M')}</i>",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  MAIN JOB
# ─────────────────────────────────────────────
def run_scraper():
    log.info("=" * 50)
    log.info("Running scraper job...")

    seen = load_seen()
    new_count = 0
    sent_count = 0

    with StealthySession(headless=True, solve_cloudflare=True) as session:
        listings = fetch_search_results(session)

        if not listings:
            log.warning("No listings found. Immoweb may have changed its structure.")
            save_seen(seen)
            return

        for listing in listings:
            lid = listing["id"]
            if lid in seen:
                continue

            if not listing.get("is_private", False):
                log.info(f"  ⏭  Agency listing {lid} — skipping")
                seen[lid] = time.time()
                continue

            new_count += 1
            log.info(f"New private listing found: {lid} — fetching details...")
            listing = fetch_listing_detail(listing, session)
            phones = listing.get("phones", [])

            if phones:
                log.info(f"  ✅ Private owner + phone found: {phones} — sending to Telegram")
                message = format_message(listing)
                send_telegram(message)
                append_to_sheet(listing)
                sent_count += 1
            else:
                log.info(f"  ⏭  No phone number in listing {lid} — skipping")

            seen[lid] = time.time()

    save_seen(seen)
    log.info(f"Done. {new_count} new listings checked, {sent_count} sent to Telegram.")


# ─────────────────────────────────────────────
#  SCHEDULER ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    DRY_RUN = "--dry-run" in sys.argv

    if DRY_RUN:
        log.info("DRY RUN mode — Telegram messages will be printed, not sent.")
        send_telegram = lambda msg: print("\n--- TELEGRAM MESSAGE ---\n" + msg + "\n------------------------\n")

    log.info("🚀 Immoweb scraper starting up...")
    log.info(f"Will check every {INTERVAL_MINUTES} minutes.")

    run_scraper()

    if not DRY_RUN:
        scheduler = BlockingScheduler()
        scheduler.add_job(run_scraper, "interval", minutes=INTERVAL_MINUTES)
        log.info("Scheduler started. Press Ctrl+C to stop.")
        try:
            scheduler.start()
        except KeyboardInterrupt:
            log.info("Scraper stopped.")
