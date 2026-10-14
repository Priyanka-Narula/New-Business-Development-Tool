# Middle East Recruitment Scraper

Collects public LinkedIn hiring posts and Jobs listings for a recruitment
agency focused on:

- healthcare
- green energy (renewable energy, solar energy, power generation)
- real estate
- construction and infrastructure
- hospitality
- retail
- education (schools and educational institutions)
- datacenters
- AI-related roles

The default markets are Saudi Arabia, the United Arab Emirates, Qatar,
Malaysia, Australia, the United Kingdom, Germany, Canada, and the
United States. Target roles
include specialists, functional experts, managers, senior managers,
department heads, directors, and leadership-level professionals. An optional
`--gcc` flag also adds Bahrain, Oman, and Kuwait. Collection keeps all
experience levels by default so later scoring can compare junior and senior
hiring volume.

## Responsible use

Use this project only where permitted by LinkedIn's terms and applicable law.
It deliberately uses a visible browser. Credentials are read only from
`LINKEDIN_EMAIL` and `LINKEDIN_PASSWORD` in the environment or a local `.env`
file. If either is absent, the scraper asks you to sign in manually. Complete
any verification prompt yourself; the code does not bypass checkpoints. Keep
volumes low and do not use the output for unsolicited bulk messaging.

## Requirements

- Windows with Python 3.11 or newer
- Google Chrome (local, visible browser)
- A LinkedIn account
- Optional: [Ollama](https://ollama.com/) running on this machine for extraction

The default local stack is a project `.venv`, SQLite file `aambridge.db`,
logs under `logs\`, and Ollama at `http://localhost:11434`. The shared team
database is Neon Postgres (**Aambridge Analytics**); set `DATABASE_URL` in
`.env` to use it.

## Setup

From this folder in PowerShell. Packages install into `.venv`, not the system
Python:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally if you want automatic form filling. Leave either credential
blank to use manual login. Set `APOLLO_API_KEY` to search Apollo people from
the dashboard and spend credits only when you disclose an email. Set
`DATABASE_URL` to the Neon pooled connection string to share data with the
team; leave it unset to use local SQLite. Runtime logs are written to the
console and rotating files under `logs/`.

## Shared Neon database

Team machines should all point at the **Aambridge Analytics** Neon project.

1. In the Neon console, open **Aambridge Analytics** → **Connect**.
2. Copy the **pooled** URL (hostname contains `-pooler`, usually
   `sslmode=require`).
3. Put it in this folder's `.env` as `DATABASE_URL=...`. Never commit `.env`.
4. Create tables, then optionally copy an existing local SQLite file:

```powershell
python -m aambridge init-db
python -m aambridge migrate-sqlite --source aambridge.db
```

Use `--replace` only if you intend to wipe Neon and reload from SQLite.
Only one scrape should run at a time against the shared database. Tests still
use temporary SQLite files and do not need Neon.

If activation is blocked, call the venv Python directly:

```powershell
.\.venv\Scripts\python.exe -m aambridge --help
```

## One-command run

`run_pipeline.py` runs the product end to end and prints a live banner before
each stage (module name and file path), then writes CSVs and opens the
dashboard.

Default **auto** mode scrapes LinkedIn only when `aambridge.db` has no
`source_record`, `job`, or `post` rows. If the database already has collected
data, it skips scrape and continues from extraction.

```powershell
# Auto: scrape if empty, otherwise clean → resolve → signals → score → export → dashboard
.\.venv\Scripts\python.exe run_pipeline.py

# Repeat a scan (required once the database already has rows)
.\.venv\Scripts\python.exe run_pipeline.py --scrape --mode both --time-filter week --descriptions --max-per-query 15 --no-dashboard

# Never scrape; fail if the database is empty
.\.venv\Scripts\python.exe run_pipeline.py --skip-scrape --no-dashboard
```

Do not crawl company websites or LinkedIn company pages. Employer-posted
jobs copy listing emails onto the company. Agency-posted jobs keep the
end-client company name only. Named people come from Apollo after you
choose a company on the dashboard.

Watch the flow while it runs:

- Terminal banners: `[1/8] SCRAPE` through `[8/8] DASHBOARD`, each naming the
  Python module and file about to execute, then a one-line stats summary.
- Same lines also go to `logs\aambridge.log`.
- Line-by-line stepping: open `run_pipeline.py` in Cursor, set breakpoints in
  the stage modules listed in the banners (`run_scraper.py`,
  `aambridge\extract\service.py`, `aambridge\resolve\companies.py`,
  `aambridge\signals\detect.py`, `aambridge\scoring\engine.py`), and use
  **Debug Python File** / F5. No extra code is required.

Outputs after a successful run:

- `output\linkedin_me_jobs.csv`
- `output\linkedin_me_posts.csv`
- `output\aambridge_top50.csv`
- Dashboard at `http://localhost:8501` unless you pass `--no-dashboard`

Unknown flags such as `--mode`, `--sectors`, and `--time-filter` are forwarded
to `run_scraper.py` when a scrape runs.

## Run

Both sources, all sectors, default markets, past week:

```powershell
.\.venv\Scripts\python.exe -m aambridge scrape --mode both
```

`run_scraper.py` remains supported for existing scripts. Scraped records are
committed incrementally. Set `DATABASE_URL` in `.env` to the shared Neon
database (or another SQLite file). Leave it unset to use local `aambridge.db`.

Examples:

```powershell
# Jobs only; healthcare and hospitality
.\.venv\Scripts\python.exe run_scraper.py --mode jobs --sectors healthcare hospitality

# Posts only; include the rest of the GCC and use the past month
.\.venv\Scripts\python.exe run_scraper.py --mode posts --gcc --time-filter month

# Collect full Jobs descriptions (slower)
.\.venv\Scripts\python.exe run_scraper.py --mode jobs --descriptions --max-per-query 10

# Limit LinkedIn Jobs to selected experience levels when required
.\.venv\Scripts\python.exe run_scraper.py --mode jobs --experience-levels mid_senior director executive

# Search posts for expansion and project-award signals
.\.venv\Scripts\python.exe run_scraper.py --mode posts --query-set expansion

# Search both hiring and expansion post query sets
.\.venv\Scripts\python.exe run_scraper.py --mode posts --query-set all
```

## Date window (3 or 6 months)

`--time-filter` defaults to `week` and currently accepts only `any`, `day`,
`week`, and `month`. `python -m aambridge scrape` forwards the same flag
through `run_scraper.py`.

Two LinkedIn parameters are involved. They are not interchangeable.

- **Jobs** use `f_TPR`, a rolling window in seconds, from `TIME_FILTER_CODES`
  in `config.py`: `day` = `r86400`, `week` = `r604800`, `month` = `r2592000`
  (30 days). `build_jobs_url` in `aambridge\sources\linkedin_jobs.py` copies
  that code into the search URL.
- **Posts** use `datePosted` in `build_content_url` in
  `aambridge\sources\linkedin_posts.py`: `past-24h`, `past-week`, and
  `past-month` only. LinkedIn content search has no token for 3 or 6 months.

The database does not filter by age. `time_filter` is stored as a short
string on query counts. Scoring, CSV export, and selectors do not need
changes.

### Files to change for a 90- or 180-day jobs window

Three places must agree. Do not change only one of them.

1. `config.py` — add codes next to the existing `TIME_FILTER_CODES` map:
   `quarter` = `r7776000` (90 × 86400) and `half_year` = `r15552000`
   (180 × 86400).
2. `run_scraper.py` — add those names to the `--time-filter` choices
   (`choices=("any", "day", "week", "month")` today).
3. `aambridge\sources\linkedin_posts.py` — the `datePosted` map has no
   matching value. Leave posts on `month`, or the posts URL builder will
   raise `KeyError` if you pass `quarter` or `half_year` while `--mode` is
   `posts` or `both`.

Example after that edit:

```powershell
.\.venv\Scripts\python.exe -m aambridge scrape --mode jobs --time-filter quarter
.\.venv\Scripts\python.exe -m aambridge scrape --mode jobs --time-filter half_year
```

Optional follow-ups, not required to run: one line in
`docs\DATA_DICTIONARY.md` for the new labels, and a URL assertion in
`tests\test_linkedin_jobs_adapter.py`.

### Limits

- LinkedIn job listings usually leave search after about 30 days. A 90- or
  180-day `f_TPR` often adds little, because closed jobs are already gone.
- Post search cannot ask for 3 or 6 months. Using `any` does not page
  reliably back that far; results are capped.
- To cover 3 or 6 months of market history, run `--time-filter week` or
  `month` on a schedule and keep `aambridge.db`. Do not run `reset-db`.
  Dedup keeps the first and last time each job or post was seen.
- A longer window means more pages and a higher chance LinkedIn slows or
  checkpoints the session. Keep `--max-per-query` modest.

Chrome opens and submits credentials from the environment or local `.env`.
If credentials are absent, or LinkedIn requests verification, complete login
manually. Add `--manual-login` to any command to always enter credentials
yourself.

Export CSV files from the database when needed:

```powershell
.\.venv\Scripts\python.exe -m aambridge export-csv output
# Or export automatically after a scan:
.\.venv\Scripts\python.exe -m aambridge scrape --mode both --export-csv output
```

The database de-duplicates canonical jobs and posts while retaining scan and
job-observation history. Query definitions and market vocabulary are in
`config.py`. LinkedIn selectors and experience-level codes are in
`config\selectors.yaml` and `config\jobs.yaml`; location and known-agency
aliases are in `taxonomies\`.

Posts retain source-dated author, category, location, and seniority evidence.
Email addresses and phone numbers are redacted before persistence. The
`has_contact_email` boolean records only that a post offered an email contact;
the address itself is never stored.

Database maintenance commands:

```powershell
# Create any missing tables (also happens automatically on scrape)
.\.venv\Scripts\python.exe -m aambridge init-db

# Pilot migration/reset approach: destructive, requires explicit confirmation
.\.venv\Scripts\python.exe -m aambridge reset-db --yes
```

`reset-db` drops all collected data. Back up or export first. The pilot uses
SQLAlchemy `create_all`; schema migrations will move to Alembic when preserving
production data across schema changes becomes necessary.

## Sector names

Use these values with `--sectors`:

`healthcare`, `green_energy`, `real_estate`,
`construction_infrastructure`, `hospitality`, `retail`,
`education`, `datacenters`, `ai`.

`green_energy` covers renewable energy, solar energy, solar farms, wind
energy, and power-generation / grid language. It does not search the
bare word `power`.

## Extract, resolve, detect signals, and score

Start Ollama and pull the default model:

```powershell
ollama pull llama3.1
ollama serve
```

In another PowerShell window, run each idempotent stage:

```powershell
.\.venv\Scripts\python.exe -m aambridge clean --workers 2
.\.venv\Scripts\python.exe -m aambridge resolve
.\.venv\Scripts\python.exe -m aambridge signals
.\.venv\Scripts\python.exe -m aambridge score
```

Or run the downstream stages together using existing database records:

```powershell
.\.venv\Scripts\python.exe -m aambridge run-all --skip-scrape
```

Without `--skip-scrape`, `run-all` starts a visible LinkedIn scrape first.
Extraction stores a fact/inference/unknown basis, exact evidence, confidence,
prompt version, and model for every field. Relevance is determined by the
deterministic scoring engine rather than the LLM.

Useful management-pipeline commands:

```powershell
# Review uncertain company matches
.\.venv\Scripts\python.exe -m aambridge resolve-review
.\.venv\Scripts\python.exe -m aambridge resolve-review --accept 1

# Show changes since the prior scan
.\.venv\Scripts\python.exe -m aambridge changes

# Evaluate extraction against the five-row template
.\.venv\Scripts\python.exe -m aambridge eval-extraction tests\fixtures\labelled_extraction_template.csv
```

The country/industry opportunity values in
`taxonomies\market_model.yaml` are clearly marked placeholders. Management
scores must not be treated as production-ready until those values and source
notes are replaced with dated market research.

## Decision-makers and public contacts

Companies can rank HIGH without a named person. Listing contacts come from
**employer-posted jobs** (company name, job URL, emails printed in the
description). **Agency-posted jobs** drop the recruiter contact and keep
the end-client company name when it can be parsed.

Named outreach people are added when you choose a company and run Apollo
(dashboard **Find Apollo people**, or the CLI below). Do not scrape company
LinkedIn About/People pages or company websites.

```powershell
# HIGH/MEDIUM companies that still need a verified contact
.\.venv\Scripts\python.exe -m aambridge decision-maker-tasks

# Add one record. Omit --verified-by until a person has confirmed the title.
.\.venv\Scripts\python.exe -m aambridge add-decision-maker --company-id 1 --name "Aisha Rahman" --designation "Head of Talent Acquisition" --source manual --verified-by recruiter --email "aisha@example.com"

# Import a spreadsheet. email/phone columns are stored when present.
.\.venv\Scripts\python.exe -m aambridge import-decision-makers tests\fixtures\decision_makers_template.csv

# List stored names, titles, ranks, and sources
.\.venv\Scripts\python.exe -m aambridge decision-makers

# Apollo people search (no credits). Uses company name and location.
.\.venv\Scripts\python.exe -m aambridge apollo-search --company-id 1

# Spend credits to reveal one stored Apollo person's work email.
.\.venv\Scripts\python.exe -m aambridge apollo-disclose --person-id 12
```

Set `APOLLO_API_KEY` in `.env`. Search stores names and titles only. Disclose
reveals one work email. The company brief has the same Find / dropdown /
Disclose flow. Phone numbers are not requested.

Allowed CSV columns: `company_id`, `company_name`, `name`, `designation`,
`department`, `profile_url`, `email`, `phone`, `source`, `verified_by`,
`confidence`, `notes`. After verified contacts are saved, re-run `score` so
`decision_maker_accessibility` uses the best relevance rank.

## Dashboard

The local Streamlit app is the Phase 10 acceptance surface: the 50 companies
to approach first, and why each one is on the list.

```powershell
.\.venv\Scripts\python.exe -m aambridge dashboard
.\.venv\Scripts\python.exe -m aambridge export-top-50 output\aambridge_top50.csv
.\.venv\Scripts\python.exe -m aambridge export-top-50 output\emiratisation_top50.csv --preset emiratisation
```

Open `http://localhost:8501`. Pages:

- **Where to focus** / **Companies** — ranked employers, **New requirements
  this scan**, and the company brief with **Find Apollo people**
- **Why this company** — factor bars, FACT/INFERENCE/UNKNOWN evidence, jobs,
  signals, score history, missing inputs, editable outreach status
- **What changed** — new companies, score movements, new signals
- **Market potential** — industry and region rankings from target-tier mix

Saved views include UAE + Riyadh and the Emiratisation list (mainland UAE,
50+ staff, nationality-restricted roles). Agencies are hidden by default.
Outreach status values: `new`, `researching`, `contacted`, `meeting`,
`client`, `not a fit`.

## CLI reference

| Command | Purpose |
| --- | --- |
| `scrape` | Visible LinkedIn jobs/posts scrape |
| `clean` | Extract from the database or a CSV |
| `eval-extraction` | Score a labelled fixture CSV |
| `resolve` | Resolve companies |
| `resolve-review` | List or accept/reject fuzzy merges |
| `import-company-groups` | Import parent groups |
| `import-company-overrides` | Import size/jurisdiction/industry/website |
| `signals` | Detect hiring and expansion signals |
| `changes` | Print what changed since the previous scan |
| `score` | Write deterministic lead scores |
| `decision-maker-tasks` | HIGH/MEDIUM companies missing a verified contact |
| `add-decision-maker` | Store one hiring contact; optional `--email` / `--phone` |
| `import-decision-makers` | CSV import; email/phone columns are stored |
| `decision-makers` | List stored names, ranks, and public contacts |
| `apollo-search` | List Apollo people for one company (no email credits) |
| `apollo-disclose` | Reveal one stored Apollo person's work email (paid) |
| `enrich-contacts` | Deprecated; do not crawl company sites or LinkedIn company pages |
| `dashboard` | Local Streamlit UI |
| `export-top-50` | Acceptance-test Top 50 CSV |
| `export-csv` | Raw jobs/posts CSV from the database |
| `run-all` | Scrape (optional) then clean → resolve → signals → score |
| `run_pipeline.py` | Auto scrape (if DB empty) then clean → score → export CSVs → dashboard |
| `init-db` | Create missing tables |
| `migrate-sqlite` | Copy a local SQLite file into `DATABASE_URL` |
| `reset-db --yes` | Drop and recreate the configured database |

## How to add a taxonomy entry

Edit the YAML under `taxonomies\`, then re-run `clean` (if keywords affect
extraction), `resolve`, `signals`, and `score`.

- Industries: `taxonomies\industries.yaml` — add a key, label, and keywords
- Functions: `taxonomies\functions.yaml`
- Seniority: `taxonomies\seniority.yaml` (`target: true` for AAMBRIDGE roles)
- Designations: `taxonomies\designations.yaml` (lower `rank` = more relevant)
- Locations / agencies: files under `taxonomies\` used by collectors
- Market opportunity: `taxonomies\market_model.yaml` (replace placeholders)

## How to change scoring weights

Edit `config\scoring.yaml` `weights` (they are percentages of 100) and
`priority` thresholds. Then:

```powershell
.\.venv\Scripts\python.exe -m aambridge score
```

A company’s total can be reproduced by hand: each factor is 0–100, multiplied
by its weight, then summed and rounded.

## Known limitations

- LinkedIn HTML changes will break selectors; update YAML, do not add stealth.
- Arabic extraction is rule- plus model-assisted; facts still need a quote.
- Market-model scores are placeholders until replaced with dated research.
- Named people come from Apollo or a manual/CSV entry; company pages are not scraped.
- Schema changes use `create_all` plus additive columns, not Alembic yet.
- pandas 2.3 imports pyarrow on startup. If Windows Application Control
  blocks a pyarrow DLL, CSV helpers fail. `run_pipeline.py` and database
  `clean` do not import pandas. The Streamlit dashboard still needs
  pyarrow; unblock the DLL or use `--no-dashboard`.

See `docs\DATA_DICTIONARY.md`.

## Notes

- LinkedIn changes its HTML regularly. If results stop parsing, card selectors
  in `config\selectors.yaml` and the adapters under `aambridge\sources\` may
  need updates.
- Collection does not discard cards for junior seniority, missing location
  terms, or apparent sector mismatch. Location and seniority helpers add hints
  only; later extraction and scoring stages classify the stored evidence.
- Selenium Manager downloads a compatible Chrome driver automatically when
  needed.

## Step-by-step: run everything locally

All Python commands below use the project virtual environment. Data stays in
this folder (`aambridge.db`, `logs\`, `output\`). Extraction talks only to
Ollama on `localhost`. For a single command that runs scrape (if needed)
through export and the dashboard, see **One-command run** and
`run_pipeline.py`.

### 1. Prerequisites

1. Install Python 3.11 or newer: `https://www.python.org/downloads/`
   (tick **Add python.exe to PATH**).
2. Install Google Chrome.
3. Optional for extraction: install Ollama from `https://ollama.com`.

### 2. Open the project and create the local venv

```powershell
cd "M:\LinkedIn Scrapper\middle-east-scrapper"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Your prompt should show `(.venv)`. If activation is blocked, skip it and use
`.\.venv\Scripts\python.exe` in place of `python` below.

### 3. Install packages into the venv

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Confirm the local interpreter:

```powershell
python -c "import sys; print(sys.executable)"
```

The path must end with `.venv\Scripts\python.exe`.

### 4. Local configuration

```powershell
Copy-Item .env.example .env
python -m aambridge init-db
python -m pytest tests -q
```

Edit `.env` in this folder if you want automatic LinkedIn login. Leave
`LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` blank for manual login. Do not set
`DATABASE_URL` unless you are using the shared Neon database or a different
local file; the default is `aambridge.db` in this folder. Keep
`OLLAMA_URL=http://localhost:11434/api/chat`.

### 5. Start local Ollama (extraction only)

In a **separate** PowerShell window, leave this running:

```powershell
ollama pull llama3.1
ollama serve
```

Skip this window if you only want to scrape and inspect raw jobs/posts.

### 6. Scrape into the local database

Back in the venv window:

```powershell
cd "M:\LinkedIn Scrapper\middle-east-scrapper"
.\.venv\Scripts\Activate.ps1
python -m aambridge scrape --mode both --export-csv output
```

1. Chrome opens on this machine. Credentials come from `.env`, or you sign in
   yourself.
2. If LinkedIn shows a checkpoint, complete it in Chrome, then return to
   PowerShell and press Enter.
3. Leave Chrome and PowerShell open until the scan finishes.
4. Rows are committed to `aambridge.db` as each card is parsed. CSV copies
   land in:

   - `output\linkedin_me_posts.csv`
   - `output\linkedin_me_jobs.csv`

Press `Ctrl+C` to stop. Parsed rows stay in the database and the scan is
marked `interrupted`.

Smaller local scrapes:

```powershell
python -m aambridge scrape --mode jobs --sectors healthcare hospitality --max-per-query 10
python -m aambridge scrape --mode posts --manual-login
python -m aambridge scrape --mode both --gcc --time-filter month
```

### 7. Extract, resolve, detect signals, and score

Ollama must still be serving on localhost.

```powershell
python -m aambridge clean --workers 2
python -m aambridge resolve
python -m aambridge signals
python -m aambridge score
```

Or one command after a scrape already in `aambridge.db`:

```powershell
python -m aambridge run-all --skip-scrape
```

Optional company-match review and change list:

```powershell
python -m aambridge resolve-review
python -m aambridge changes
```

Optional CSV-only extraction (writes `*_extracted.csv` next to the input):

```powershell
python -m aambridge clean output\linkedin_me_jobs.csv
python -m aambridge clean output\linkedin_me_posts.csv
```

### 8. Decision-makers and public contacts

Employer job emails are stored at resolve time. For named people, open the
dashboard, pick a company under **New requirements this scan**, then
**Find Apollo people**.

```powershell
python -m aambridge apollo-search --company-id 1
python -m aambridge decision-maker-tasks
python -m aambridge add-decision-maker --company-id 1 --name "Aisha Rahman" --designation "Head of Talent Acquisition" --source manual --verified-by recruiter
python -m aambridge import-decision-makers tests\fixtures\decision_makers_template.csv
python -m aambridge decision-makers
python -m aambridge score
```

Re-run `score` after a contact is saved so accessibility uses the title rank.

### 9. Export raw jobs and posts

```powershell
python -m aambridge export-csv output
```

### 10. Open the local dashboard (Phase 10)

```powershell
python -m aambridge dashboard
```

Chrome or Edge: `http://localhost:8501`

1. On **Pipeline**, confirm the ranked list (agencies hidden).
2. Use the **UAE and Riyadh** or **Emiratisation** saved view if needed.
3. Select a company and open **Why this company** to read the factor bars and
   source-dated evidence.
4. Set outreach status (`new` → `researching` → `contacted`, and so on).
5. Download **Export Top 50 CSV**, or from PowerShell:

   ```powershell
   python -m aambridge export-top-50 output\aambridge_top50.csv
   ```

6. Check **What changed** and **Executive summary**.

### 11. Verify the local install (Phase 11)

```powershell
python -m pytest tests -q
```

All tests are fixture-only (no live LinkedIn, no Ollama). Read
`docs\DATA_DICTIONARY.md` for every table and column.

Local artefacts:

| Path | What it is |
| --- | --- |
| `aambridge.db` | Local SQLite fallback (jobs, posts, companies, scores) |
| `logs\aambridge.log` | Rotating runtime log |
| `output\linkedin_me_jobs.csv` | Jobs export |
| `output\linkedin_me_posts.csv` | Posts export |
| `output\aambridge_top50.csv` | Ranked outreach list |
| `.venv\` | Isolated Python environment |

To wipe the local database and start again:

```powershell
python -m aambridge reset-db --yes
python -m aambridge init-db
```
