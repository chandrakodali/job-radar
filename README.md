# Job Radar

Finds new **entry-friendly DevOps, SRE, cloud, platform and infrastructure jobs in the US** within about 30 minutes of posting, straight from companies' own career sites, and sends them to your phone.

**[Open the latest report →](REPORT.md)**

## What it does

- Every 30 minutes, GitHub Actions reads **about 6,700 company job boards** (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Workable, Oracle and more): every board, every run, including every Workday board back 7 days. These are the feeds the companies' own careers pages use.
- It also reads other job sources through allowed routes, and lists a job from them only when the employer's own board didn't already have it:
  - **Workable's public job search** (every company on Workable), every 3 hours
  - **Adzuna** (free API key, shared with the Auto Radar; credited "Jobs by Adzuna")
  - **USAJOBS** federal jobs (free API key); jobs needing US citizenship or a clearance are left out
  - **Hacker News "Who is hiring?"**, **Remote OK** and **We Work Remotely** (credited and linked back)
  - **job-alert emails** from LinkedIn, Indeed, Dice, ZipRecruiter, Glassdoor (you set up the alerts; nothing is scraped)
- It keeps only US roles whose titles fit: DevOps, SRE, platform, cloud, infrastructure, plus adjacent titles (sysadmin, network, systems, production, build/release engineer) **only when the job text names real DevOps/cloud work** (at least two of Kubernetes, Docker, Terraform, Ansible, CI/CD, AWS/Azure/GCP, monitoring, scripting...).
- **SRE titles** are matched in every common form (Site Reliability Engineer, SRE, Software/Infrastructure/Database/Cloud Reliability Engineer, "Software Engineer, Site Reliability", Amazon's Systems Development Engineer, Production Engineer for infrastructure, Cloud Resiliency Engineer), and a clear SRE or DevOps title is never dropped by a side word like "Voice", "SAP" or "Manufacturing". A plain "Reliability Engineer" (often a plant job) is kept only when the text shows SRE work: SLOs, postmortems, Kubernetes, Terraform, cloud, Linux...
- **Years of experience** are read from the job's requirements section (a "preferred" 5+ years doesn't count; "BS + 5 or MS + 3" counts as 3; ranges count by their low end). Jobs asking for **5+ years** are left out.
- **Senior titles** (Senior, Sr., III, level 3) are kept when they ask for under 5 years, and listed with the note "senior title, years not stated" when the text gives no number. Staff, Principal, Lead, Manager, Architect and level IV+ are skipped.
- Jobs needing a **security clearance or US citizenship** are left out (listed at the bottom of the report). "No sponsorship" jobs are **kept** and flagged. Contract and staffing-firm jobs are kept and flagged.
- **[REPORT.md](REPORT.md)** lists every match **posted in the last 7 days, newest first, one section per day**. The same job listed in several cities or on several sources is one row. A coverage line shows how many of the jobs other sources listed were already found on employer boards.
- Employers seen on other sources but not yet watched, and employers that filed H-1B LCAs for DevOps/SRE/cloud titles (public DOL data), are tried as new boards by the weekly board finder.
- It alerts you on strong matches and sends a **daily digest** at 8 AM Central time. No AI is used for this radar.

## IT Entry-Level Radar

**[Open the report →](REPORT_FRESHERS.md)** · page: `https://chandrakodali.github.io/job-radar/freshers/`

Every IT job in the US for **freshers and up to 1 year of experience**, in any area of IT: software, web/mobile,
data/BI/analytics, ML/AI, QA/test, IT support/help desk, network, systems, security/SOC, cloud/DevOps, DBA,
ERP/CRM (SAP, Salesforce, ServiceNow...), business/systems analyst, implementation and support engineers.
It reads the same 7,500+ employer boards as the DevOps radar (so it finds jobs that are never advertised on
job sites), plus Workable's job search and your job-alert emails, and runs every hour.

- A job is listed only when it **shows it's entry level**: an entry-level title (junior, associate, new grad,
  graduate, trainee, I/1...), entry-level wording in the text ("new grads", "entry-level", "0-2 years",
  "class of 2026"...), or 0–1 years asked. A plain "Software Engineer" with no sign of level is not listed.
- Asking **2+ years**, senior/lead/II+ titles, internships, and jobs needing a clearance or US citizenship are
  left out. Non-IT jobs (mechanical, nursing, sales, finance...) are skipped.
- Settings: `profiles/freshers/config.yaml`. Results: `data/freshers/`.

## Auto Radar: automotive embedded software jobs

A second search runs from this repo for **automotive embedded software roles in the US**, built from the
resume: embedded and controls software, model-based development (MATLAB / Simulink / Stateflow, Embedded Coder,
TargetLink), AUTOSAR (DaVinci), Polyspace and MISRA C, ISO 26262, ASPICE, MIL / SIL / HIL testing, CAN, and
steering / ADAS features.

**[Open the Auto Radar report →](REPORT_AUTOMOTIVE.md)** · page: `https://chandrakodali.github.io/job-radar/automotive/`

- It scans **142 automotive employers' own job boards** in depth ([the list](profiles/automotive/companies.csv)):
  OEMs, truck and off-highway makers, Tier-1 suppliers, automated-driving companies, automotive chip and tool
  vendors, engineering-service firms, plus every board the DevOps radar already watches (about 6,600).
- **Workday employers (GM, Ford, Magna, BorgWarner, Cummins, Oshkosh and 35 more) are read completely**: every
  US job they list, whatever its age. Other employers' boards are read newest-first **and searched by keyword
  in the full job text** (12 core keywords every run, 48 more in rotation: AUTOSAR, Simulink, TargetLink,
  Polyspace, ISO 26262, CAPL, BSW, MCAL, UDS, SOTIF, VectorCAST...), so a job titled "Engineer II" that asks
  for Simulink and AUTOSAR is found.
- Senior titles are kept; Staff / Principal / Architect are kept but marked as a stretch. Internships,
  management, technician, manufacturing, mechanical and web / cloud roles are left out. Jobs needing a
  clearance, US citizenship or ITAR status are listed separately at the bottom, not dropped. Nexteer (current
  employer) is skipped.
- It runs every 3 hours. A job whose text could not be read in one run waits for the next run instead of
  being judged without it.

### How jobs are scored (no AI)

Each job's full description is read the way a recruiter's screening software reads it
([radar/rank.py](radar/rank.py)):

1. Boilerplate (about the company, benefits, equal-opportunity text) is dropped, and the rest is split into
   **required**, **preferred** and **responsibilities**. A skill under "required" counts fully, under
   "preferred" half.
2. A dictionary of ~150 skills ([profiles/automotive/skills.yaml](profiles/automotive/skills.yaml)) includes
   the ones the resume does **not** have (Linux kernel, FPGA, HIL hardware, CAPL, UDS...). The score is about
   how much of what the job requires the resume covers, so a job that wants Simulink *and* kernel drivers
   *and* FPGA ranks below one that wants Simulink and AUTOSAR. The report shows **Has** (the resume's skills
   the job asks for) and **Missing** (required skills the resume does not show).
3. Years of experience are read as written: "BS + 5 years or MS + 3 years" means 3 for this resume.
4. Title grade, industry, word overlap with the resume (words rare across all postings count most),
   freshness and location make up the rest. Visa sponsorship moves the score by 2 points at most.

Scores of **78+** are "strong" (about the top 15%). Every saved job's text is kept, so when the dictionary or
the weights change, the whole history is rescored on the next run. **[SKILLS_AUTOMOTIVE.md](SKILLS_AUTOMOTIVE.md)**
lists the skills the current matches ask for most, split into the ones the resume shows and the ones it does
not (what to add to the resume, or learn).

**Teach the score your taste.** Open [`profiles/automotive/labels.csv`](profiles/automotive/labels.csv) on
GitHub (pencil icon), write `apply`, `maybe` or `no` in the first column for as many of the 60 jobs as you
like, and commit. Then `python -m radar.calibrate --profile automotive --tune` shows how well the score agrees
with you and the weights that would agree best. Jobs you mark `no` (and jobs marked Applied or Skipped on the
page, if you paste the page's marks into `profiles/automotive/marks.json`) are left out of the report.

An optional AI check (`ai: mode: resume` in the config, with free Gemini / Groq / NVIDIA keys) is switched off;
the DevOps radar does not use AI.

### More sources: job-alert emails and Adzuna

**Job-alert emails** bring in jobs that appear only on LinkedIn, Indeed, Dice, ZipRecruiter or Glassdoor
(common for small suppliers and engineering-service firms), without scraping those sites:

1. Create a new Gmail account used only for job alerts, turn on 2-Step Verification, then create an app
   password at https://myaccount.google.com/apppasswords.
2. On LinkedIn, Indeed, Dice etc., create job alerts (e.g. "AUTOSAR", "Simulink", "embedded software
   automotive", "controls engineer", Michigan or United States) sent to that Gmail address.
3. Add repository secrets `JOB_ALERTS_EMAIL` (the address) and `JOB_ALERTS_APP_PASSWORD` (the app password).

Each run reads the new alert emails (read-only), skips jobs the employer's own board already lists, and keeps
clear target titles, marked "description not read" (the sites behind the links cannot be read).

**Adzuna**, a job search engine with a free official API: register at https://developer.adzuna.com and add
`ADZUNA_APP_ID` and `ADZUNA_APP_KEY` as secrets. The radar makes about 80 calls a day, inside the free limits.

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

GitHub's own schedule often skips runs on busy days, so each DevOps run ends by waiting for the next half-hour slot and starting the next run itself (`scripts/next_run.py`), and starts the Auto Radar every 3 hours; the schedules stay on as a backup.

Turn the page on once: **Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main`, folder `/docs` → Save.**
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
| `profiles/automotive/` | Auto Radar: rules, skills dictionary, employer boards, coverage notes, labeling list, employers from DOL filings |
| `radar/rank.py`, `radar/calibrate.py` | Auto Radar's rule-based fit score and its calibration against your labels |
| `radar/sources.py` | Job-alert emails and Adzuna |
| `radar/ai_fit.py` | Optional AI check of each job description against the resume (off) |
| `SKILLS_AUTOMOTIVE.md`, `data/automotive/coverage.csv` | Skills the matching jobs ask for; jobs listed per employer board |
| `REPORT_AUTOMOTIVE.md`, `data/automotive/` | Auto Radar report, match history and state |

## Notes

- Runs free: GitHub Actions minutes are free for public repositories.
- GitHub may delay scheduled runs at busy times, and pauses schedules in repositories with no activity for 60 days. The radar commits its results regularly, which counts as activity.
- Workday boards are checked in rotation (each one every 2 hours) to stay polite to those sites.
- Always confirm visa-related details with your DSO. The clearance/sponsorship flags come from keyword checks and can be wrong, so skim the posting.

Run locally: `pip install -r requirements.txt && python -m radar.main --dry-run --only greenhouse --limit 20`. Tests: `python -m pytest -q`.
