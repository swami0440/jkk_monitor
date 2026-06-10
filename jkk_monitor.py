#!/usr/bin/env python3
"""
JKK Net vacancy monitor — watches JKK東京 (jhomes.to-kousya.or.jp) for new
rental vacancies in a given ward (default: 江東区 / Koto-ku) and emails you
when a new one appears.

WHY PLAYWRIGHT (not requests):
  The search endpoint (akiyaJyoukenRef) is session-based. Hitting it directly
  returns the site's "おわび" (apology) page. You must navigate from the start
  page so the server issues a session cookie, then submit the search form.
  A real browser handles the session, the Shift-JIS encoding, and any JS.

WHAT IT DOES (one run):
  1. Opens the JKK Net search start page.
  2. Finds the ward dropdown and selects 江東区.
  3. Clicks the search button.
  4. Reads the result rows.
  5. Compares them to what it saw last time (state file).
  6. Emails you ONLY the newly-appeared units.

SCHEDULING:
  Run it every 30 min via cron / Task Scheduler / launchd (see README at bottom),
  OR run with --loop 30 to keep it running and self-schedule.

FIRST RUN — CALIBRATE:
  Run `python jkk_monitor.py --debug` once. It saves a screenshot + HTML of each
  step to ./debug/ and prints what it detected. If the auto-detection misses the
  ward dropdown, the search button, or the result rows, share those files and the
  selectors can be pinned down exactly. The generic detection below works for most
  standard <select>/<table> forms but the JKK flow may have an extra step.
"""

import argparse
import hashlib
import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# CONFIG  — override any of these with environment variables
# ---------------------------------------------------------------------------
WARD = os.environ.get("JKK_WARD", "江東区")

# JKK Net search START page (sets the session). The flow then lands on the
# conditions page. If this URL changes, update it here.
START_URL = os.environ.get(
    "JKK_START_URL",
    "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyoukenStartInit",
)

STATE_FILE = Path(os.environ.get("JKK_STATE_FILE", "jkk_seen.json"))
DEBUG_DIR = Path("debug")

# --- Telegram (preferred). Set both to enable. ---
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")    # from @BotFather
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # from getUpdates

# --- Email (SMTP) fallback. Defaults to Gmail; use an App Password. ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")        # your gmail address
SMTP_PASS = os.environ.get("SMTP_PASS", "")        # gmail App Password
EMAIL_TO  = os.environ.get("EMAIL_TO", SMTP_USER)  # where to send alerts

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# State (dedup) helpers
# ---------------------------------------------------------------------------
def load_seen() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set) -> None:
    STATE_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=0), encoding="utf-8"
    )


def row_key(text: str) -> str:
    """Stable id for a listing row, robust to whitespace noise."""
    norm = "".join(text.split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Browser flow
# ---------------------------------------------------------------------------
def dump(page, name: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
        (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
        print(f"  [debug] saved debug/{name}.png and debug/{name}.html")
    except Exception as e:
        print(f"  [debug] dump failed: {e}")


def select_ward(page, ward: str) -> bool:
    """Find any <select> that has an option containing the ward name, pick it."""
    for sel in page.query_selector_all("select"):
        for opt in sel.query_selector_all("option"):
            label = (opt.inner_text() or "").strip()
            if ward in label:
                val = opt.get_attribute("value")
                try:
                    sel.select_option(value=val)
                except Exception:
                    sel.select_option(label=label)
                print(f"  selected ward via <select>: '{label}'")
                return True
    # Fallback: checkbox/radio list of wards
    for inp in page.query_selector_all("input[type=checkbox], input[type=radio]"):
        # label may be the next text node / associated <label>
        lid = inp.get_attribute("id")
        if lid:
            lab = page.query_selector(f'label[for="{lid}"]')
            if lab and ward in (lab.inner_text() or ""):
                inp.check()
                print(f"  selected ward via checkbox: {ward}")
                return True
    return False


def click_search(page) -> bool:
    """Click whatever element looks like the search/submit button (検索)."""
    selectors = ["input[type=submit]", "input[type=button]", "button", "a"]
    for css in selectors:
        for el in page.query_selector_all(css):
            txt = (el.get_attribute("value") or "") + " " + (el.inner_text() or "")
            if "検索" in txt or "けんさく" in txt:
                try:
                    el.click()
                    print(f"  clicked search: '{txt.strip()[:30]}'")
                    return True
                except Exception:
                    continue
    return False


def extract_listings(page) -> list:
    """
    Heuristic: a listing row contains a rent amount (e.g. '94,100円').
    Returns list of (key, text). Verify with --debug if rows look wrong.
    """
    listings = []
    for tr in page.query_selector_all("tr"):
        txt = (tr.inner_text() or "").strip()
        if "円" in txt and len(txt) > 8:
            listings.append((row_key(txt), " ".join(txt.split())))
    # de-dupe within the page
    seen, out = set(), []
    for k, t in listings:
        if k not in seen:
            seen.add(k)
            out.append((k, t))
    return out


def search_once(debug: bool = False, headed: bool = False) -> list:
    with sync_playwright() as p:
        # Always headless on servers/CI; --headed only for local visible debugging.
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="ja-JP")
        page = ctx.new_page()
        try:
            print(f"Opening {START_URL}")
            page.goto(START_URL, wait_until="networkidle", timeout=45000)
            if debug:
                dump(page, "1_start")

            if not select_ward(page, WARD):
                print(f"!! Could not find ward selector for '{WARD}'.")
                dump(page, "ward_not_found")
                print("   Run with --debug and share debug/ward_not_found.html")
                return []

            if not click_search(page):
                print("!! Could not find search button.")
                dump(page, "search_btn_not_found")
                return []

            page.wait_for_load_state("networkidle", timeout=45000)
            if debug:
                dump(page, "2_results")

            listings = extract_listings(page)
            print(f"  found {len(listings)} listing row(s) on results page")
            return listings
        except PWTimeout:
            print("!! Page timed out.")
            dump(page, "timeout")
            return []
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(new_rows: list):
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("!! Email not configured (SMTP_USER/SMTP_PASS/EMAIL_TO). Skipping send.")
        print("   New units found:")
        for _, t in new_rows:
            print("   -", t)
        return

    body_lines = [f"{len(new_rows)} new JKK vacancy(ies) in {WARD}:\n"]
    for _, t in new_rows:
        body_lines.append("• " + t)
    body_lines.append(f"\nSearch: {START_URL}")
    body = "\n".join(body_lines)

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"[JKK] {len(new_rows)} new vacancy in {WARD}"
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
    print(f"  emailed {len(new_rows)} new unit(s) to {EMAIL_TO}")


def send_telegram(new_rows: list):
    lines = [f"\U0001F3E0 {len(new_rows)} new JKK vacancy in {WARD}:\n"]
    for _, t in new_rows:
        lines.append("• " + t)
    lines.append(f"\n{START_URL}")
    text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "disable_web_page_preview": "true"}).encode()
    with urlopen(Request(url, data=data), timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram returned {resp.status}")
    print(f"  Telegram: sent {len(new_rows)} new unit(s)")


def notify(new_rows: list):
    """Prefer Telegram if configured, else email, else just print."""
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram(new_rows)
    elif SMTP_USER and SMTP_PASS and EMAIL_TO:
        send_email(new_rows)
    else:
        print("!! No notifier configured. New units found:")
        for _, t in new_rows:
            print("   -", t)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_once(debug: bool, first_run_silent: bool = False, headed: bool = False):
    listings = search_once(debug=debug, headed=headed)
    if not listings:
        return
    seen = load_seen()
    new_rows = [(k, t) for k, t in listings if k not in seen]

    if new_rows and not first_run_silent:
        notify(new_rows)
    elif new_rows:
        # First ever run: record baseline, don't spam you with everything.
        print(f"  baseline run: recording {len(new_rows)} existing unit(s), no alert")
    else:
        print("  no new units")

    for k, _ in listings:
        seen.add(k)
    save_seen(seen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true",
                    help="save screenshots/HTML to ./debug (works headless, CI-safe)")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser window (local only; needs a display)")
    ap.add_argument("--loop", type=int, metavar="MIN", default=0,
                    help="run forever, every MIN minutes (e.g. --loop 5)")
    args = ap.parse_args()

    first = not STATE_FILE.exists()
    if first:
        print("No state file yet → this run is a silent baseline (records current "
              "listings without alerting).")

    if args.loop:
        run_once(args.debug, first_run_silent=first, headed=args.headed)
        while True:
            print(f"\nSleeping {args.loop} min …")
            time.sleep(args.loop * 60)
            run_once(args.debug, first_run_silent=False, headed=args.headed)
    else:
        run_once(args.debug, first_run_silent=first, headed=args.headed)


if __name__ == "__main__":
    main()