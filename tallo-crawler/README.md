# Tallo job crawler

Find recent U.S. DevOps, SRE, cloud, platform, infrastructure, and adjacent support jobs from Tallo's Technology browse pages. Required experience of zero to four years qualifies, and missing requirements are flagged. No-sponsorship and no-OPT wording lowers the score. Clearance, citizenship, residency, and export-control signals put a match in the separate blocked section for review.

## Install locally

Use Python 3.11 or later. Open a terminal in the extracted `tallo-crawler` directory.

**macOS or Linux**

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

On a fresh supported Linux host, install Chromium and its system libraries with `python -m playwright install --with-deps chromium`. System package installation can require administrator privileges.

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

On Windows, use `.\.venv\Scripts\python.exe` wherever the steps below use `python`.

## Probe first

No email is needed: the bot identifies itself with the project URL in `config.yaml` (`contact_url`). Run:

```sh
python tallo_crawler.py --probe
```

A successful probe reads four Tallo documents in order: category, state, city, job. It reads robots.txt once before those documents. The default prefers Texas and Austin if discovered, with a fallback to the largest linked option. Review the terminal output and `data/probe/` captures for city links, card text, the View More control, and job fields. A denied URL, failed navigation, or unusable layout can stop the probe early. A 5xx retry consumes one of the four allowed attempts.

Once the probe produces the expected fields:

```sh
python tallo_crawler.py
python tallo_crawler.py --states tx ca dc --max-pages 150 --since-hours 48
python tallo_crawler.py --dry-run --max-pages 25 --verbose
python -m pytest -q
```

`--categories` and `--states` take space-separated URL segments. `--core-cities` sets the core allocation and `--rotation` controls the number of slices. `--config` selects a YAML file and uses the `data/` directory next to that file. A dry run still accesses the allowed pages and uses any existing checkpoint and job history. Its crawl records stay in memory. The test suite uses synthetic pages, mock HTTP responses, and a local offline Chromium renderer. The Chromium fixture test skips if the browser is missing.

## Daily use

The tree caches discovered state and city links by Chicago calendar day. The crawler gives the first five categories priority, with software developer/engineer discovery and crawling after their scheduled cities. Within each group, it takes the highest job-count pages as core, followed by that rotation slice in descending job-count order. The first group gets the core allocation first. The rotation slot for a non-core URL is stable while the slice count is fixed.

A completed sweep advances the slice. If a budget or error interrupts a sweep, the next invocation continues the saved queue, including the remaining job links for a city already read. Unfinished discovery can continue across midnight. A sweep can span several invocations. Changing categories, states, core size, or rotation starts a fresh plan and retains the seen database. For a fixed discoverable city set, every non-core city is scheduled within six completed sweeps at the default rotation setting.

| File | Purpose |
| --- | --- |
| `data/tallo_matches.csv` | Append-only export with a UTF-8 BOM for Excel. Contains alert, quiet, blocked, and repost rows. |
| `data/tallo_latest.md` | Phone-readable report with Chicago timestamps, score ordering within age bands, and blocked matches at the bottom. |
| `data/seen.sqlite` | Seen IDs and durable match records. The seen table prunes entries after 120 days. Exported IDs and repost keys are retained for deduplication. |
| `data/tree.json` and `data/checkpoint.json` | Atomically replaced discovery cache and resume state. |
| `data/probe/` | Four probe captures on success and snapshots of cities that unexpectedly yielded zero links. |

Keep the SQLite database and CSV together when moving the data directory. If the CSV is missing, the next run reconstructs it from the database. Use the run summary to see skipped senior matches, budget stops, and blocked runs. A normal finish or page/time budget exit returns zero. Blocks, layout failures, unresolved navigation failures, and incomplete probes return a nonzero exit code.

For phone alerts, set the optional `NTFY_TOPIC` environment variable to a hard-to-guess topic and subscribe to it on your phone. The crawler sends the 15 highest-scoring new alerts at most, plus one overflow count when applicable. Notifications are best-effort after report generation. A hard stop or timeout preserves the records for the report. [ntfy publishing documentation](https://docs.ntfy.sh/publish/)

## Google Colab

Upload the extracted project folder to Google Drive. Run the crawler as a subprocess so it uses its own asyncio event loop. Keeping the project in Drive also preserves crawl progress across Colab sessions.

```python
from google.colab import drive
drive.mount("/content/drive")
```

```python
%cd "/content/drive/MyDrive/tallo-crawler"
!python -m pip install -r requirements.txt
!python -m playwright install --with-deps chromium
```

Then run:

```python
import os
from google.colab import userdata

```

```python
!python tallo_crawler.py --probe
```

After inspecting the probe, run `!python tallo_crawler.py` in a new cell.

## GitHub Actions

Place these files at the repository root, including `.github/workflows/tallo.yml`. Optionally add the repository Actions secret `NTFY_TOPIC`. Run and inspect a probe locally or dispatch the **Tallo job crawler** workflow with the probe option enabled. Manual probes and failed runs publish any captured HTML as a workflow artifact.

The workflow runs at 07:17 and 19:17 UTC on the default branch, installs Chromium with system dependencies, serializes runs, and commits persistent `data/` files after the crawl step, including after a blocked or error exit. The target branch must allow the workflow token to push. Probe HTML and transient lock/journal files stay out of commits. [GitHub Actions workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)

## Assumptions and limits

- The DOM selectors assume readable job links and field labels, optional schema.org attributes, and browse counts like `Austin 425 jobs`. The job title and description selectors, empty-state text, and button names can be tuned in `config.yaml`. The probe verifies the current page shape.
- The HTTP client reads robots.txt, checks every Tallo document against the hard URL allowlist and robots rules, and disables redirects. Chromium renders each fetched document offline. Inline scripts can reveal already-delivered content, including a View More or Read more control. External scripts, API calls, and offsite applications are blocked. If Tallo requires extra requests to show the cards or description, the crawler cannot obtain that content under this allowlist. [Playwright API request documentation](https://playwright.dev/python/docs/api/class-apirequestcontext)
- The crawler requires permission from `urllib.robotparser` and also checks the applicable robots rules for wildcard, end-of-path, and longest-match restrictions. [Python robotparser documentation](https://docs.python.org/3.11/library/urllib.robotparser.html), [Robots Exclusion Protocol](https://www.rfc-editor.org/rfc/rfc9309.html)
- A positive city count and three consecutive city snapshots without job links trigger the layout stop. Review `data/probe/layout/`, run `--probe`, and adjust the selectors to match visible content. A robots.txt retrieval error stops the run before any document requests. A 5xx document response is retried once, with a 30-second minimum wait.
- The 48-hour cutoff, page cap, city rotation, and 15-click cap limit coverage. Newest-first detection uses the observed dates on a city page. Ambiguous or missing card dates keep pagination open within the click cap. Relative ages are approximate. Calendar dates use midnight in Chicago, and unknown Posted values use the first-seen time. Last Updated is shown in probe diagnostics only.
- Experience, location, and authorization rules use the available posting text. Title clearance exclusions go to the blocked section when the title otherwise fits. A C2C-only restriction is flagged separately. These labels identify language in the posting and should be reviewed against the original job description.
