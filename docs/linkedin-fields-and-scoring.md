
# LinkedIn fields, lead scores, and the dashboard

This document traces **what is scraped** from LinkedIn jobs and posts, **which fields feed which scores**, **which files compute them**, and **how the dashboard shows the result**.

Scores are **company-level**, not per listing. Jobs and posts are collected first, then cleaned, resolved to a company, turned into signals, and only then scored.

```
LinkedIn card
  → scrape (sources)
  → source_record + job/post tables (storage)
  → extraction (deterministic, optional LLM)
  → company resolution
  → signals
  → lead score (7 factors → total + priority)
  → dashboard / CSV
```

Pipeline files: `run_pipeline.py` and `python -m aambridge` (`aambridge/__main__.py`).

| Stage | Module | File |
| --- | --- | --- |
| Scrape jobs | `aambridge.sources.linkedin_jobs` | `aambridge/sources/linkedin_jobs.py` |
| Scrape posts | `aambridge.sources.linkedin_posts` | `aambridge/sources/linkedin_posts.py` |
| Persist | `aambridge.db.storage` | `aambridge/db/storage.py` |
| Extract | `aambridge.extract.service` | `aambridge/extract/service.py` |
| Resolve companies | `aambridge.resolve.companies` | `aambridge/resolve/companies.py` |
| Signals | `aambridge.signals.detect` | `aambridge/signals/detect.py` |
| Score | `aambridge.scoring.engine` + `factors` | `aambridge/scoring/engine.py`, `aambridge/scoring/factors.py` |
| Dashboard | Streamlit | `aambridge/dashboard/app.py`, `aambridge/dashboard/queries.py` |
| Weights / gates | YAML | `config/scoring.yaml` |

Column-level storage is also listed in `docs/DATA_DICTIONARY.md`.

---

## 1. What is scraped from a LinkedIn job

Parser: `JobLead` in `aambridge/sources/linkedin_jobs.py`. CSS selectors live in `config/selectors.yaml` under `linkedin_jobs`. The full `JobLead` dict is stored as `source_record.raw_fields_json` and `job_observation.raw_listing_json`.

| Scraped field | Where it comes from on LinkedIn | Canonical storage | Used later for |
| --- | --- | --- | --- |
| `linkedin_job_id` / `source_key` | `/jobs/view/{id}` or card `data-job-id` | `job.linkedin_job_id`, `job.source_key` | Dedup, evidence join |
| `job_url` | Built from job id | `source_record.source_url` | Evidence, CSV |
| `job_title` | Card title | `job.title` | Seniority, functions, evidence quote, dashboard “Roles Hiring” |
| `company_name` | Card company | `job.company_name_raw` | Company resolve, agency detect |
| `company_linkedin_url` | Company link on card | `job.company_linkedin_url` | Company resolve |
| `job_location` | Card location text | `job.location_raw` | Location parse |
| `city`, `region`, `country` | Mapped from `taxonomies/locations.yaml` | `job.city` / `region` / `country` | Company HQ, `distinct_locations`, dashboard country/city, market model |
| `location_unmatched` | True if location not in taxonomy | raw JSON only | Not scored directly |
| `seniority_hint` | First matched seniority/excluded term in title+location+metadata+description | raw JSON | Seniority classification (also title + description) |
| `description` | Detail pane (only if scrape used `--descriptions`) | raw JSON `description` | Seniority, functions, nationality phrases, extraction |
| `posted_date`, `posted_date_precision`, `posted_date_raw` | Time text or `datetime` attribute | `job_observation` | Repeated-vacancy age uses `job.first_seen_at`, not this date |
| `is_reposted` | “Reposted” in card metadata | `job_observation.is_reposted` | Signal `repeated_vacancy` |
| `is_promoted` | “Promoted” in metadata | `job_observation.is_promoted` | Stored only |
| `easy_apply` | “Easy Apply” in metadata | raw JSON | Stored only |
| `applicant_count`, `applicant_count_raw` | “N applicants” in metadata | `job_observation.applicant_count` | Agency potential if senior and ≥ 100 |
| `workplace_type` | On-site / Hybrid / Remote in metadata | raw JSON | Extraction `work_mode`; not a score factor |
| `job_criteria` | Detail pane criteria (seniority, employment type, function, industries) | raw JSON | Extraction; scoring uses title/description keywords, not this dict directly |
| `nationality_restriction`, `nationality_restriction_evidence` | Phrases in `config/jobs.yaml` | raw JSON | Signal `localisation_pressure`; agency-potential points |
| `posted_by_agency`, `agency_evidence` | Company name vs `taxonomies/known_agencies.yaml` + agency terms | raw JSON | `company.is_agency`; agencies are **not scored** |
| `end_client_hint` | Phrases in `config/jobs.yaml` | raw JSON | Extraction only |
| `industry` | **Search sector**, not LinkedIn’s industry label | raw JSON `industry` | Company `industry_key` if set at resolve |
| `query`, `search_location` | Query that found the card | raw JSON | Audit only |
| `source` | Always `"job"` | raw JSON | Adapter identity |

Scan-level extras (not per card): page-one result count in `query_result_count`.

Observation flags set by storage, not LinkedIn:

- `job.first_seen_at` / `last_seen_at` — first/latest scrape
- `job.is_active` — closed after missing `closed_after_missing_scans` (default 2) completed scans (`aambridge/signals/detect.py`)
- `job_observation` row per scan — “active this scan” for scoring

---

## 2. What is scraped from a LinkedIn post

Parser: `PostLead` in `aambridge/sources/linkedin_posts.py`. Selectors: `config/selectors.yaml` → `linkedin_posts`. Categories: `config/posts.yaml`.

| Scraped field | Where it comes from | Canonical storage | Used later for |
| --- | --- | --- | --- |
| `source_key` | Activity URN, or hash of text | `post.source_key` | Dedup, evidence join |
| `post_url` | Feed update URL | `post.post_url` | Evidence |
| `post_text` | Visible card text (emails/phones redacted) | `post.text`, `source_record.raw_text` | Category, extraction, expansion quotes |
| `author_name` | Author name | `post.author_name` | Company resolve (company authors) |
| `author_url` | Profile or company URL | `post.author_url` | Company LinkedIn URL if `author_type` is company |
| `author_headline` | Headline | `post.author_headline` | Agency detect |
| `author_type` | `company` if `/company/`, `person` if `/in/` | `post.author_type` | Resolve path |
| `author_is_agency`, `agency_evidence` | Agency name/terms on author | `post.author_is_agency` | Skip scoring if company is an agency |
| `post_date`, precision, raw | Relative or exact time | `post.post_date` | Stored; not a score input |
| `post_category` | Phrase match: `hiring`, `expansion`, `project_award`, `leadership_change`, else `other` | `post.post_category` | **`expansion` / `project_award` → signal `expansion`** |
| `job_title` | Regex after “we are hiring…” | raw JSON | Extraction vacancy title |
| `location_hint` | First location alias in text | raw JSON | Extraction city/country |
| `seniority_hint` | First seniority term in text | raw JSON | Extraction |
| `has_contact_email` | Boolean only (address not stored) | raw JSON | Privacy flag; not scored |
| `industry`, `query`, `query_set`, `search_location` | Search that found the post | raw JSON | Audit |

Posts **do not** increment `active_jobs`. They affect scores only after they are linked to a company and (for expansion) produce a signal.

Person-authored posts can still attach to a client company if extraction finds `company_name_clean` (`aambridge/resolve/companies.py`).

---

## 3. Fields extracted after scrape (not scraped as structured data)

File: `aambridge/extract/deterministic.py` (always), optional LLM in `aambridge/extract/llm.py`. Schema: `aambridge/extract/schema.py`. Stored in `extraction.result_json`.

These fields refine later stages; scoring mostly re-reads job title, description, and company rows rather than `extraction.result_json`.

| Extracted field | Main inputs | Scoring relevance |
| --- | --- | --- |
| `job_title_clean`, `company_name_clean` | Scraped title / company / author | Company resolve for person posts |
| `industry`, `function`, `seniority` | Taxonomy keywords in text | Resolve industry; scoring re-classifies seniority/functions from job text |
| `country`, `city` | Scraped location fields | Company HQ when resolving posts |
| `nationality_restriction` | Same phrases as scrape | Duplicate of job raw field |
| `is_agency_posting` | `posted_by_agency` / `author_is_agency` | Agency flag |
| `post_category` | Scraped category + phrase evidence | Expansion signal uses `post.post_category` |
| `employment_type`, `work_mode`, `experience_years_min`, `openings_count`, `end_client_hint` | Text rules | Not used in factor formulas |

---

## 4. Company fields that are **not** scraped from cards

Set at resolve or by a later override (`aambridge/resolve/companies.py`):

| Company field | Default from scrape | Score factor |
| --- | --- | --- |
| `industry_key` | Job search `industry`, or post extraction | Industry opportunity, AAMBRIDGE fit, HIGH gate |
| `hq_country` | Job `country` | Market model lookup |
| `size_band` | **Always `None` at scrape** | Company potential (0 if unknown) |
| `group_id` | **None** until grouped | +10 company potential |
| `is_agency` | Agency detect on name | Company skipped by `score_companies` |
| `jurisdiction` | `unknown` | Dashboard Emiratisation preset only |
| `website` | CSV override if provided. Company pages are not crawled | Not scored |
| Decision makers | Apollo (owner-triggered) or manual/CSV import | Accessibility; HIGH does not require a verified contact |

---

## 5. Signals (bridge between listings and scores)

File: `aambridge/signals/detect.py`. Thresholds: `config/scoring.yaml` → `signals`.

| Signal | Trigger | Feeds factor |
| --- | --- | --- |
| `hiring_acceleration` | Target-seniority job count vs previous scan: Δ ≥ 3 **and** ≥ 50% | Hiring activity +20 |
| `hiring_slowdown` | Opposite drop ≥ 3 jobs and ≥ 40% | Stored; **not** added to hiring activity |
| `new_senior_vacancy` | First observation of a director/VP/C-level job | Timeline only; applicant count still uses the same jobs |
| `repeated_vacancy` | `is_reposted` **or** job age ≥ 30 days | Hiring activity +10; agency potential +25 |
| `new_location` | City/location not seen on previous scan | Hiring activity +5 |
| `localisation_pressure` | Nationality-restriction evidence on a job | Agency potential +15 (inference) |
| `expansion` | LinkedIn **post** category `expansion` or `project_award` this scan | Hiring activity +5; also allows scoring a company with **zero** active jobs |

---

## 6. How the lead score is calculated

### 6.1 Who is scored

`score_companies` in `aambridge/scoring/engine.py`:

1. Skip `company.is_agency`.
2. Build `CompanyScoreContext` from jobs observed on the **latest completed scan**, plus that scan’s signals and verified decision-makers.
3. Skip if `active_jobs == 0` **and** there is no `expansion` signal.
4. Run seven factor functions in `aambridge/scoring/factors.py`.
5. Write `lead_score` (`total_score`, `priority`, `factor_results_json`, `explanation_text`).

### 6.2 Context counters (`build_context`)

For each job with an observation on this scan:

| Context field | How it is counted |
| --- | --- |
| `active_jobs` | Jobs seen this scan |
| `new_jobs` | Those jobs with only one observation ever |
| `target_jobs` | Seniority in `taxonomies/seniority.yaml` where `target: true` (upper junior through C-level) |
| `senior_jobs` | `director`, `vp`, `c_level` |
| `junior_jobs` | `intern`, `entry`, `junior` |
| `specialist_jobs` | `specialist`, `functional_expert` |
| `high_applicant_senior_jobs` | Senior jobs with `applicant_count >= 100` |
| `nationality_restricted_jobs` | raw `nationality_restriction` true |
| `distinct_locations` | Unique `city` or `location_raw` |
| `functions` | Keyword hits from `taxonomies/functions.yaml` in title + description + seniority hint |
| `industry` | `company.industry_key`, else first job `industry` (`green_energy` → `renewable_energy`, `hospitality` → `hospitality_tourism`, `ai` → `ai_data`) |
| `industry_opportunity` | `taxonomies/market_model.yaml` score for `(hq_country, industry)` or country `other` |
| `decision_maker_rank` | Lowest `relevance_rank` among **verified** decision-makers |

Seniority is classified by `_seniority` in `aambridge/signals/detect.py` (longest/highest-order keyword match).

### 6.3 Factor formulas (each 0–100)

Weights in `config/scoring.yaml` (must sum to 100):

| Factor | Weight | File |
| --- | --- | --- |
| `hiring_activity` | 25 | `aambridge/scoring/factors.py` → `hiring_activity` |
| `industry_opportunity` | 20 | `industry_opportunity` |
| `seniority_match` | 15 | `seniority_match` |
| `company_potential` | 10 | `company_potential` |
| `aambridge_fit` | 15 | `aambridge_fit` |
| `external_agency_potential` | 10 | `external_agency_potential` |
| `decision_maker_accessibility` | 5 | `decision_maker_accessibility` |

**Total** (`score_context` in `engine.py`):

```
total = round( Σ (factor_score_0_100 × weight / 100) )
```

#### Hiring activity (weight 25)

If `active_jobs == 0` → **0** (missing `active_jobs`). Expansion-only companies therefore get 0 here except signal bonuses are not applied because the function returns early. (They can still be scored because of the expansion exception in `score_companies`, but hiring activity stays 0.)

Otherwise, cap 100:

| Piece | Points |
| --- | --- |
| Target-seniority jobs | `min(40, target_jobs × 4)` |
| Newly observed jobs | `min(20, new_jobs × 4)` |
| Signal `hiring_acceleration` | +20 |
| Signal `repeated_vacancy` | +10 |
| Signal `new_location` | +5 |
| Signal `expansion` | +5 |

**Scraped inputs:** job title, description, seniority hint, first/last seen, location, plus posts that created `expansion`.

#### Industry opportunity (weight 20)

Lookup table, not LinkedIn:

`score = market_model[country][industry].score`  
If country or industry missing → **0**.

**Scraped inputs:** job `country` / search `industry` (via company). **Config:** `taxonomies/market_model.yaml` (placeholders until research is filled in).

#### Seniority match (weight 15)

```
share = target_jobs / (target_jobs + junior_jobs)
score = share × 100
```

Jobs classified `unknown`, specialist, manager, etc. that are **not** junior are in `target_jobs` if `target: true`. Specialist/manager/director are target; intern/entry/junior are junior. If both counts are 0 → **0**.

**Scraped inputs:** title, description, seniority hint.

#### Company potential (weight 10)

If `size_band` unknown → **0**.

```
score = size_score × 0.6
      + min(30, distinct_locations × 10)
      + (10 if company has a group else 0)
```

Size map: `1-49` → 20, `50-249` → 60, `250-999` → 80, `1000+` → 100.

**Scraped inputs:** location only. Size and group are **not** scraped.

#### AAMBRIDGE fit (weight 15)

If industry unknown → **0**.

```
score = (70 if industry in capability list else 20)
      + min(30, overlapping_functions × 10)
```

Capability lists: `config/scoring.yaml` → `aambridge_capabilities`.

**Scraped inputs:** search/extracted industry; job text for functions.

#### External agency potential (weight 10)

Start at 0; if nothing fires → **0**.

| Indicator | Points |
| --- | --- |
| Signal `repeated_vacancy` | +25 |
| Each senior job with ≥ 100 applicants | +10 each, max 25 |
| Each nationality-restricted job | +15 each, max 25 |
| Signal `localisation_pressure` | +15 |
| Each specialist / functional-expert job | +5 each, max 10 |

**Scraped inputs:** repost flag, first seen date, applicant count, nationality phrases, title/description seniority.

#### Decision-maker accessibility (weight 5)

No verified decision-maker → **0**.

```
score = max(30, 100 − (relevance_rank − 1) × 5)
```

Rank 1 → 100, rank 2 → 95, … floor 30. **Not scraped.**

### 6.4 Priority (HIGH / MEDIUM / LOW)

After total is rounded:

**HIGH** only if **all** of these hold (`config/scoring.yaml` → `priority`):

| Gate | Default |
| --- | --- |
| Total score | ≥ 75 |
| Hiring activity factor | ≥ 60 |
| Seniority match factor | ≥ 50 |
| External agency potential factor | ≥ 40 |
| Industry is an AAMBRIDGE capability **or** a market-model score exists | — |
| A verified decision-maker exists | `require_decision_maker_for_high: false` (off so ranking works before Apollo) |

Else **MEDIUM** if total ≥ 50, else **LOW**.

A company can rank HIGH without a named person. Accessibility stays 0 until Apollo or a manual contact is saved.

### 6.5 Explanation text

`_explanation` in `engine.py` concatenates the top four non-zero factors’ first reasons, plus the first evidence quote’s date and URL. Stored as `lead_score.explanation_text`.

---

## 7. Field → factor map (quick reference)

| Scraped / derived field | Hiring | Industry | Seniority | Company pot. | Fit | Agency pot. | DM |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Job title | ✓ seniority | | ✓ | | ✓ functions | ✓ seniority | |
| Description | ✓ | | ✓ | | ✓ | ✓ | |
| Seniority hint | ✓ | | ✓ | | | ✓ | |
| City / location | ✓ new loc. | | | ✓ distinct | | | |
| Country | | ✓ market | | | | | |
| Search `industry` | | ✓ | | | ✓ | | HIGH |
| Applicant count | | | | | | ✓ if senior ≥100 | |
| Reposted / first seen | ✓ repeated | | | | | ✓ | |
| Nationality phrases | | | | | | ✓ + signal | |
| Post category expansion / award | ✓ +5 | | | | | | (score without jobs) |
| Company size / group | | | | ✓ | | | |
| Verified decision-maker | | | | | | | ✓ HIGH |
| Agency company name | *skipped entirely* | | | | | | |

---

## 8. Worked numeric example

Using the HIGH fixture in `tests/test_scoring.py`:

- 10 active jobs, 8 target, 5 new, 2 junior, 2 locations, healthcare, market 85, size `250-999`, in a group, functions clinical + hr_talent, 2 high-applicant senior jobs, 2 nationality-restricted, 2 specialist, rank-1 DM, signals: acceleration, repeated vacancy, new location, expansion, localisation.

| Factor | 0–100 | × weight | Contribution |
| --- | --- | --- | --- |
| Hiring | 32 + 20 + 20 + 10 + 5 + 5 = **92** | 25 | 23.00 |
| Industry | **85** | 20 | 17.00 |
| Seniority | 8 / 10 × 100 = **80** | 15 | 12.00 |
| Company | 80×0.6 + 20 + 10 = **78** | 10 | 7.80 |
| Fit | 70 + 20 = **90** | 15 | 13.50 |
| Agency | 25 + 20 + 25 + 15 + 10 = **95** | 10 | 9.50 |
| Decision-maker | **100** | 5 | 5.00 |
| **Total** | | | **round(88)** |

Priority HIGH: 88 ≥ 75, hiring 92 ≥ 60, seniority 80 ≥ 50, agency 95 ≥ 40, healthcare is a capability, DM present.

---

## 9. How scores are displayed on the dashboard

App: `aambridge/dashboard/app.py`. Data: `aambridge/dashboard/queries.py`. Reads **SQLite/Postgres only** — no live LinkedIn.

### Pipeline (“Companies to approach first”)

Latest completed scan’s `lead_score` rows, agencies hidden by default, sorted HIGH → MEDIUM → LOW then score descending.

| Column on screen | Source |
| --- | --- |
| Priority | `lead_score.priority` |
| Company | `company.canonical_name` |
| Industry | `company.industry_key` |
| Country/City | HQ country or mode of job countries; mode of job cities |
| Hiring Activity | Band from **hiring_activity factor**: ≥60 high, ≥30 medium, else low, plus active job count, e.g. `high (8 jobs)` |
| Roles Hiring | Up to 5 active job titles |
| Seniority | Mode of classified job seniorities |
| Key Contact / Designation | Lowest-rank **verified** decision-maker |
| Lead Score | `lead_score.total_score` (the weighted total) |
| Status | `company.outreach_status` |
| Last Updated | `lead_score.created_at` |

Sidebar **Lead score** slider filters `total_score`. Hiring-activity band filter uses the factor, not the total.

### Why this company

- Explanation paragraph (`explanation_text`)
- Metrics: lead score and priority
- Outreach status editor
- **Score breakdown:** every factor 0–100 as a progress bar, `[FACT]` / `[INFERENCE]` / `[UNKNOWN]`, and reason lines from `factor_results_json`
- Missing inputs warning from `explanation_json.missing_inputs`
- Evidence quotes (jobs and posts), active jobs table, signals timeline, score history across scans

### What changed

`what_changed` in `aambridge/signals/detect.py`: new companies (first-seen jobs this scan), score deltas vs previous scan, new signals.

### Executive summary

Counts of HIGH / MEDIUM / LOW, verified decision-maker companies, new prospects, names with `hiring_acceleration`, top industries and cities.

### Market potential charts

**Not** the lead score. `graph_potential` in `queries.py` drops `company_potential`, `external_agency_potential`, and `decision_maker_accessibility`, then **rescales the remaining weights to 100**:

Kept weights: hiring 25 + industry 20 + seniority 15 + fit 15 = 75.

```
graph_potential = round( Σ (kept_factor × weight) / 75 )
```

For the worked example: (92×25 + 85×20 + 80×15 + 90×15) / 75 ≈ **87**.

Bars average that value by region×industry and country×city.

### Export

Pipeline **Export Top 50 CSV** uses the same columns plus an evidence summary (`export_top50_csv`). Pipeline also writes `output/aambridge_top50.csv` from `run_pipeline.py`.

---

## 10. Config knobs

| File | What it changes |
| --- | --- |
| `config/scoring.yaml` | Factor weights, HIGH/MEDIUM gates, signal thresholds, AAMBRIDGE capability industries/functions |
| `config/jobs.yaml` | Nationality phrases, agency terms, end-client phrases, experience-level search codes |
| `config/posts.yaml` | Post category phrases |
| `config/selectors.yaml` | LinkedIn CSS (what can be scraped at all) |
| `taxonomies/seniority.yaml` | Target vs junior classification |
| `taxonomies/functions.yaml` | Fit overlap |
| `taxonomies/industries.yaml` | Industry keywords in extraction |
| `taxonomies/locations.yaml` | City/region/country parse |
| `taxonomies/market_model.yaml` | Industry-opportunity 0–100 |
| `taxonomies/known_agencies.yaml` | Agency skip list |
| `taxonomies/designations.yaml` | Decision-maker rank |

After changing weights or taxonomies, re-run extract (if keywords changed), resolve, signals, then `python -m aambridge score`. The dashboard reads the stored `lead_score` row; it does not recompute factors.
