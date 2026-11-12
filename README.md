# Job Radar

Finds new **entry-friendly DevOps, SRE, cloud, platform and infrastructure jobs in the US** within about 30 minutes of posting, straight from companies' own career sites, and sends them to your phone.

**[Open the latest report →](REPORT.md)**

## What it does

- Every 30 minutes, GitHub Actions checks about **3,800 company job boards** on Greenhouse, Lever, Ashby, SmartRecruiters and Workday. These are the feeds the companies' own careers pages use, so jobs show up here before they're buried on LinkedIn.
- It keeps only US roles whose titles fit you: DevOps, SRE, platform, cloud, infrastructure (main targets), plus bridge roles such as Linux admin, NOC, cloud/technical support and data center technician.
- It skips senior titles (Senior, Staff, Lead, Manager, Architect, III and up) and internships.
- It reads each new job's description and:
  - **hides** jobs that need a security clearance, US citizenship or ITAR (F-1/OPT holders can't meet these). They're still listed at the bottom of the report.
  - flags **E-Verify** (needed for STEM OPT), **"no sponsorship"** wording, years of experience required, and likely **H-1B cap-exempt** employers (universities).
- It scores each job 0–100 (title fit, entry-level signals, years required, freshness, on-site/hybrid, E-Verify, your priority companies) and alerts you on strong matches (score 45+).
- It also sends a **daily digest** at 8 AM Central time.
- The company list refreshes every Monday from the public SimplifyJobs new-grad and internship datasets, so companies that start hiring early-career people get added automatically.

## Auto Radar: automotive embedded software jobs

A second search runs from this repo for **automotive embedded software roles in the US**, built from the
resume: embedded and controls software, model-based development (MATLAB / Simulink / Stateflow, Embedded Coder,
TargetLink), AUTOSAR (DaVinci), Polyspace and MISRA C, ISO 26262, ASPICE, MIL / SIL / HIL testing, CAN, and
steering / ADAS features.

**[Open the Auto Radar report →](REPORT_AUTOMOTIVE.md)** · page: `https://chandrakodali.github.io/job-radar/automotive/`

- It scans **137 automotive employers' own job boards** in depth ([the list](profiles/automotive/companies.csv)):
  OEMs, truck and off-highway makers, Tier-1 suppliers, automated-driving companies, automotive chip and tool
  vendors, plus every board the DevOps radar already watches.
- On those boards it does more than read titles: it **searches the full job text** for 25 keywords from the
  resume (AUTOSAR, Simulink, TargetLink, Polyspace, ISO 26262, steer-by-wire, HIL...), so a job titled
  "Engineer II" that asks for Simulink and AUTOSAR is found.
- Every candidate job's description is read and scored on how many of the resume's tools it names. Generic
  titles are kept only when the text shows those tools. The Notes column lists the tools found.
- Senior titles are kept; Staff / Principal / Architect are kept but marked as a stretch. Internships,
  management, technician, manufacturing, mechanical and web / cloud roles are left out.
- Jobs needing a clearance, US citizenship or ITAR status are listed separately at the bottom, not dropped.
- Nexteer (current employer) is skipped.
- It runs every 3 hours. A job whose text could not be read in one run waits for the next run instead of
  being judged without it.

**AI check against the resume.** Each match's full job description is also read by a free AI model next to a
summary of the resume (skills and experience only, no name or contact details). It gives a 0–100 fit score,
the resume skills the job asks for ("has"), what the job requires that the resume lacks ("missing"), and a
short reason, shown in the report's **AI** and **AI check** columns. The final score is 60% the AI's score and
40% the keyword score. Jobs the AI judges a different kind of work, or that need a clearance/citizenship or 10+
years, move to the hidden list with the reason. Visa sponsorship does not lower the AI score. It uses every key
it finds and moves to the next service when one runs out of free requests for the day:
`NVIDIA_API_KEY` (build.nvidia.com), `GEMINI_API_KEY` (aistudio.google.com), `GROQ_API_KEY` (console.groq.com),
and GitHub Models through the workflow's own token (no key). Matches found before the AI check are checked over
the next runs, best first. **Actions → radar-auto → Run workflow → "Only test the AI keys"** shows which keys
work. The DevOps radar does not use AI.

**What it cannot see** is listed in [profiles/automotive/COVERAGE.md](profiles/automotive/COVERAGE.md), with a
link to each of those employers' careers pages. Tesla and MathWorks, for example, block automated readers.

Change the rules in [`profiles/automotive/config.yaml`](profiles/automotive/config.yaml); add an employer by
adding a row to `profiles/automotive/companies.csv`. **Actions → auto-boards** re-checks every board weekly.
Alerts go to the same phone channel as the DevOps radar unless you add `NTFY_TOPIC_AUTO` (or
`TELEGRAM_BOT_TOKEN_AUTO` / `TELEGRAM_CHAT_ID_AUTO`) secrets.

## Get alerts on your phone (pick one, about 3 minutes)

### Option A: ntfy (easiest, no account)
1. Install the **ntfy** app (iPhone or Android).
2. In the app tap **+** and subscribe to a topic name only you know, for example `job-radar-chandu-7f3k9q` (random letters keep it private).
3. On GitHub open this repo → **Settings → Secrets and variables → Actions → New repository secret**. Name: `NTFY_TOPIC`, value: your topic name. Save.

### Option B: Telegram
1. In Telegram, message **@BotFather**, send `/newbot`, pick a name, and copy the token it gives you.
2. Open your new bot and send it `/start`.
3. Add a repository secret named `TELEGRAM_BOT_TOKEN` with that token. The radar finds your chat automatically.

You'll get a "Job Radar is connected" message on the next run (within 30 minutes). To test right away: **Actions → radar → Run workflow**.

## See all results on one page

Every run rewrites `docs/index.html`: a phone-friendly page with every match, filters (date, work type,
sponsor-friendly, strong only), and Save / Applied / Skip buttons that remember your choices in your browser.

Turn it on once: **Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main`, folder `/docs` → Save.**
It then lives at `https://chandrakodali.github.io/job-radar/`.

## AI check (optional, free)

Currently switched off for the DevOps radar: its workflow passes no AI keys (they are used by the Auto Radar
only). To turn it back on, add the key lines back to `.github/workflows/radar.yml`.

Each new match can be reviewed by an AI model before it is saved or sent to your phone: it drops roles that
aren't DevOps / SRE / platform / cloud / infrastructure / DevSecOps (or close), drops 5+ year roles and
clearance/citizenship roles, and adds a fit score and a one-line reason. Rejected jobs go to `data/rejected.csv`.

Turn it on by adding **one** repository secret (Settings → Secrets and variables → Actions → New repository secret):
`GEMINI_API_KEY` (free key from https://aistudio.google.com/apikey), or `OPENROUTER_API_KEY`, `GROQ_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. Optional variable `AI_MODEL` picks a specific model.

## Make it yours

- **Add companies** (your friend's company, dream employers): edit [`companies_custom.csv`](companies_custom.csv). Use `auto` as the ats and the company's job-board name (usually the last part of its careers URL) and the radar figures out the rest. Priority companies get a +10 score boost.
- **Change titles, keywords, alert threshold or digest time**: edit [`config.yaml`](config.yaml).
- **Pause it**: Actions → radar → ⋯ → Disable workflow.

## Companies that hire without a job board

Some companies only post jobs on their own website and ask for a CV by email, so the radar can't see them.
[`careers_scan/`](careers_scan/README.md) finds those companies once a month across ~6,000 startups, reads
their careers pages twice a day, and lists their cloud / DevOps / SRE / platform / infrastructure roles with
one-click email apply links in [careers_scan/ROLES.md](careers_scan/ROLES.md) (alerts for new ones). Companies
from those lists that use a tracking system the radar didn't know are added to the radar automatically.

## Files

| File | What it is |
|---|---|
| `REPORT.md` | Latest matches: last 24 hours, the rest of the week, and hidden clearance jobs |
| `data/matches.csv` | Every match ever found (opens in Excel or Google Sheets) |
| `data/state.json` | What the radar has already seen (don't edit) |
| `companies.csv` | Auto-built list of company job boards (rebuilt weekly) |
| `companies_custom.csv` | Your own companies (never overwritten) |
| `config.yaml` | Matching and scoring rules |
| `profiles/automotive/` | Auto Radar: its rules, employer boards, coverage notes and the resume summary for the AI check |
| `radar/ai_fit.py` | Auto Radar's AI check of each job description against the resume |
| `REPORT_AUTOMOTIVE.md`, `data/automotive/` | Auto Radar report, match history and state |

## Notes

- Runs free: GitHub Actions minutes are free for public repositories.
- GitHub may delay scheduled runs at busy times, and pauses schedules in repositories with no activity for 60 days. The radar commits its results regularly, which counts as activity.
- Workday boards are checked in rotation (each one every 2 hours) to stay polite to those sites.
- Always confirm visa-related details with your DSO. The clearance/sponsorship flags come from keyword checks and can be wrong, so skim the posting.

Run locally: `pip install -r requirements.txt && python -m radar.main --dry-run --only greenhouse --limit 20`. Tests: `python -m pytest -q`.
