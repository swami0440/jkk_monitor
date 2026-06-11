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

    # JKK uses checkboxes where labels share mismatched `for` attributes and
    # all inputs share `id="ku"`. Find the label by text, then click the
    # preceding sibling checkbox (the pattern on this site is <input><label>).
    for label_el in page.query_selector_all("label"):
        if ward in (label_el.inner_text() or ""):
            # Try the label's for= → getElementById path first
            for_id = label_el.get_attribute("for")
            if for_id:
                inp = page.query_selector(f"#{for_id}")
                if inp:
                    inp.check()
                    print(f"  selected ward via label[for=#{for_id}]: {ward}")
                    return True
            # Fallback: the checkbox is the immediately preceding sibling
            clicked = label_el.evaluate("""el => {
                const prev = el.previousElementSibling;
                if (prev && (prev.type === 'checkbox' || prev.type === 'radio')) {
                    prev.click();
                    return true;
                }
                return false;
            }""")
            if clicked:
                print(f"  selected ward via adjacent checkbox: {ward}")
                return True

    # Last resort: checkbox whose associated label (matched by id) contains the ward
    for inp in page.query_selector_all("input[type=checkbox], input[type=radio]"):
        lid = inp.get_attribute("id")
        if lid:
            lab = page.query_selector(f'label[for="{lid}"]')
            if lab and ward in (lab.inner_text() or ""):
                inp.check()
                print(f"  selected ward via checkbox id match: {ward}")
                return True
    return False


def click_search(page) -> bool:
    """Click the listing search button (検索する), not the map-search link."""
    selectors = ["input[type=submit]", "input[type=button]", "button", "a"]
    for css in selectors:
        for el in page.query_selector_all(css):
            val = el.get_attribute("value") or ""
            txt = el.inner_text() or ""
            # JKK uses <a><img alt="検索する"></a> — check child img alt too
            img_alt = ""
            try:
                img = el.query_selector("img")
                if img:
                    img_alt = img.get_attribute("alt") or ""
            except Exception:
                pass
            combined = val + " " + txt + " " + img_alt
            # Require "検索する" (list search) and exclude "エリアで検索" (map view)
            if ("検索する" in combined or "けんさく" in combined) and "エリア" not in combined:
                try:
                    el.click()
                    print(f"  clicked search: '{combined.strip()[:30]}'")
                    return True
                except Exception:
                    continue
    return False


_COLS = ("name", "area", "priority", "type", "layout", "size", "rent", "fee", "units")


def extract_listings(page) -> list:
    """
    Parse result table into structured dicts.
    Columns (TD indices 1-9): 住宅名, 地域, 優先種別, 住宅種別, 間取り,
    床面積[m2], 家賃[円], 共益費[円], 募集戸数.
    Returns list of (key, dict).
    """
    listings = []
    seen = set()
    for tr in page.query_selector_all("tr"):
        tds = tr.query_selector_all("td")
        if len(tds) < 10:
            continue
        cells = [(td.inner_text() or "").strip() for td in tds]
        name, area, priority, kind, layout, size, rent, fee, units = cells[1:10]
        if not rent or not any(c.isdigit() for c in rent):
            continue
        row = dict(zip(_COLS, (name, area, priority, kind, layout, size, rent, fee, units)))
        k = row_key(name + layout + rent)
        if k not in seen:
            seen.add(k)
            listings.append((k, row))
    return listings


def _fmt_row(row: dict) -> str:
    """Plain-text summary of one listing (used for email / console fallback)."""
    return (
        f"{row['name']}  ({row['area']})\n"
        f"  間取り: {row['layout']}  床面積: {row['size']} m²\n"
        f"  家賃: {row['rent']} 円  共益費: {row['fee']} 円  空室: {row['units']} 戸\n"
        f"  種別: {row['type']}"
    )


def search_once(debug: bool = False, headed: bool = False) -> list:
    with sync_playwright() as p:
        # Always headless on servers/CI; --headed only for local visible debugging.
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="ja-JP")
        page = ctx.new_page()
        try:
            print(f"Opening {START_URL}")
            # The start page's onload opens a popup window ("JKKnet") and submits
            # the session form into it. The actual search UI lives in that popup.
            with page.expect_popup(timeout=45000) as popup_info:
                page.goto(START_URL, wait_until="domcontentloaded", timeout=45000)

            popup = popup_info.value
            popup.wait_for_load_state("networkidle", timeout=45000)
            if debug:
                dump(popup, "1_start")

            if not select_ward(popup, WARD):
                print(f"!! Could not find ward selector for '{WARD}'.")
                dump(popup, "ward_not_found")
                print("   Run with --debug and share debug/ward_not_found.html")
                return []

            if not click_search(popup):
                print("!! Could not find search button.")
                dump(popup, "search_btn_not_found")
                return []

            popup.wait_for_load_state("networkidle", timeout=45000)
            if debug:
                dump(popup, "2_results")

            listings = extract_listings(popup)
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
        for _, row in new_rows:
            print(_fmt_row(row))
        return

    body_lines = [f"{len(new_rows)} new JKK vacancy(ies) in {WARD}:\n"]
    for _, row in new_rows:
        body_lines.append(_fmt_row(row))
        body_lines.append("")
    body_lines.append(f"Search: {START_URL}")
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
    blocks = [f"\U0001F3E0 {len(new_rows)} new JKK vacancy in {WARD}"]
    for _, row in new_rows:
        blocks.append(
            f"\U0001F4CD {row['name']}\n"
            f"  {row['area']}  ·  {row['type']}\n"
            f"  \U0001F6CF {row['layout']}  ・  \U0001F4D0 {row['size']} m²\n"
            f"  \U0001F4B4 {row['rent']} 円  /  管理費 {row['fee']} 円\n"
            f"  空室 {row['units']} 戸"
        )
    blocks.append(f"\U0001F517 {START_URL}")
    text = "\n\n".join(blocks)

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
        for _, row in new_rows:
            print(_fmt_row(row))
            print()


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
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test notification with current listings (ignores seen state)")
    args = ap.parse_args()

    if args.test_notify:
        listings = search_once(debug=args.debug, headed=args.headed)
        if listings:
            print(f"  --test-notify: sending {len(listings)} listing(s) as a test alert")
            notify(listings)
        else:
            print("  --test-notify: no listings found to send")
        return

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