# JKK 江東区 Vacancy Monitor

Watches JKK東京's vacancy search (jhomes.to-kousya.or.jp) for new rental
listings in **江東区 (Koto-ku)** and sends a **Telegram** message the moment a
new one appears. Runs entirely on GitHub Actions — your computer is never
involved.

---

## What you need
- A free GitHub account.
- A Telegram bot token (from @BotFather) and your chat id (from @userinfobot).

## Files in this repo
- `jkk_monitor.py` — the monitor script.
- `.github/workflows/jkk-monitor.yml` — the schedule + runner.
- `jkk_seen.json` — created automatically; remembers what's already been seen.

---

## Setup (all in the browser, ~10 min)

### 1. Create the repo
GitHub → **New repository** → name it `jkk-monitor` → **Private** → **Create**.

### 2. Add the two files
Use **Add file → Create new file** for each:
- `jkk_monitor.py` → paste the script, commit.
- `.github/workflows/jkk-monitor.yml` → type that exact path (the `/`
  characters create the folders), paste the workflow, commit.

### 3. Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add these two (nothing else):
| Name | Value |
|------|-------|
| `TELEGRAM_TOKEN` | your bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | your numeric chat id |

Secrets stay encrypted and are never shown again — that's why they're safe.

### 4. Verify the scraper (do this first!)
**Actions** tab → select **JKK Koto-ku vacancy monitor** → **Run workflow** →
tick the **debug** box → **Run**.
When it finishes, open the run and check:
- The log line `found N listing row(s)` — N should be > 0.
- A downloadable **jkk-debug** artifact (page HTML + screenshots) at the bottom.

If N is 0 or you see `Could not find ward selector`, the page layout needs a
selector fix — grab the `jkk-debug` artifact for troubleshooting.

### 5. Go live
Do one normal **Run workflow** (debug **un**ticked). This first run is a silent
**baseline** — it records the units currently listed without messaging you.
After that the schedule takes over automatically.

---

## Schedule & cost
- Runs **every 15 minutes** during JKK's update window
  (Mon–Sat, ~09:30–17:00 JST). Outside that window JKK doesn't update, so there's
  nothing to check.
- Estimated ~1,700–1,900 GitHub Actions minutes/month — under the **2,000 free**
  minutes for a private repo, with browser caching to help.
- **Recommended:** set your billing spending limit to **$0**
  (Settings → Billing) so it can never charge you; it simply pauses if it ever
  exceeds the free quota.

### Want it faster than 15 min?
5-minute polling does **not** fit a free private repo. Options:
1. Make the repo **public** (Actions minutes are unlimited & free there; your
   secrets stay hidden). Then change the cron to `*/5 0-8 * * 1-6`.
2. Pay for extra Actions minutes.

---

## How it behaves
- Sends a Telegram message **only when a new 江東区 unit appears.**
- First run is silent (baseline).
- "Seen" state is committed back to the repo so it remembers across runs.
- No JKK login needed — searching is public; an account is only required to
  actually *apply* for a unit once you're alerted.

## Change the ward
Edit `JKK_WARD: "江東区"` in the workflow to any ward name (e.g. `"江戸川区"`).