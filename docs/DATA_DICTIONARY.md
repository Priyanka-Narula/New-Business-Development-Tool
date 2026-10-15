# AAMBRIDGE data dictionary

Default store is local SQLite (`aambridge.db`). Set `DATABASE_URL` to use the
shared Neon Postgres database. Emails and phone numbers from raw LinkedIn
jobs and posts are still redacted. Public company channels and optional
decision-maker email/phone collected from official websites are stored.

## scan_run

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| started_at | datetime | When the scrape began |
| finished_at | datetime | When the scrape ended or was interrupted |
| mode | string | `jobs`, `posts`, or `both` |
| args_json | json | Sanitised CLI arguments for that run |
| status | string | `running`, `completed`, `interrupted`, or `failed` |
| stats_json | json | Counts and parse-failure tallies |

## company_group

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| name | string | Parent-group label imported or assigned during resolution |

## company

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| canonical_name | string | Resolved English name |
| canonical_name_ar | string | Optional Arabic name |
| linkedin_url | string | Unique company LinkedIn URL when known |
| website | string | Official website from a CSV override if provided |
| industry_key | string | Taxonomy industry key |
| hq_country | string | Headquarters country |
| size_band | string | Headcount band, for example `50-249`. Stored and CSV-overridable; not used in scoring |
| jurisdiction | string | `mainland`, `free_zone`, or `unknown` |
| is_agency | boolean | True when the company is a recruitment agency |
| outreach_status | string | `new`, `researching`, `contacted`, `meeting`, `client`, or `not a fit` |
| lead_score_percentile | float | Mid-rank percentile of the latest lead score among scored non-agency companies. Empty for agencies and unscored companies |
| acquisition_band | string | `High`, `Medium`, or `Low` from that percentile: top third, middle third, bottom third. Empty when the percentile is empty |
| group_id | integer | Optional foreign key to `company_group` |

## company_alias

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| company_id | integer | Parent company |
| alias | string | Observed name spelling |
| alias_normalised | string | Suffix-stripped match key |
| source | string | `linkedin`, `manual`, or import source |

## company_merge_review

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| source_company_id | integer | Company being reviewed |
| candidate_company_id | integer | Possible duplicate |
| similarity | float | Fuzzy score 0–100 |
| status | string | `pending`, `accepted`, or `rejected` |
| created_at | datetime | When the review was opened |
| reviewed_at | datetime | When it was accepted or rejected |

## source_record

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| scan_run_id | integer | Scan that first stored this card |
| source | string | `linkedin_job` or `linkedin_post` |
| source_key | string | Canonical job id, post URN, or text hash |
| source_url | string | Public listing URL |
| observed_at | datetime | When the card was parsed |
| raw_text | text | Redacted visible text |
| raw_fields_json | json | Redacted structured fields (`has_contact_email` is a boolean only) |
| content_hash | string | SHA-256 of normalised text |
| processing_status | string | `new`, `extracted`, or `failed` |
| extraction_attempts | integer | Cleaner attempts so far |
| processing_error | text | Last extraction error |

## job

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| source_key | string | Unique job identity |
| linkedin_job_id | string | Numeric LinkedIn job id when parsed |
| title | string | Job title as collected |
| company_name_raw | string | Name on the card before resolution |
| company_linkedin_url | string | Company URL from the card |
| company_id | integer | Resolved company |
| location_raw | string | Location string from the card |
| city | string | Parsed city |
| region | string | Parsed region |
| country | string | Parsed country |
| first_seen_at | datetime | First scan that stored the job |
| last_seen_at | datetime | Most recent scan that still saw it |
| is_active | boolean | False after consecutive missing scans |

## job_observation

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| job_id | integer | Parent job |
| scan_run_id | integer | Scan that produced this snapshot |
| observed_at | datetime | Snapshot time |
| posted_date | date | Parsed posting date |
| posted_date_precision | string | `exact`, `relative`, or `unknown` |
| is_reposted | boolean | Repost flag from the listing |
| applicant_count | integer | Applicant count when visible |
| is_promoted | boolean | Promoted listing flag |
| raw_listing_json | json | Redacted listing payload |

## post

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| source_key | string | Unique post identity |
| post_url | string | Public post URL |
| author_name | string | Author display name |
| author_url | string | Author profile or company URL |
| author_headline | string | Headline text |
| author_type | string | `company`, `person`, or `unknown` |
| author_is_agency | boolean | Author looks like an agency |
| company_id | integer | Resolved company |
| text | text | Redacted post body |
| post_date | date | Parsed post date |
| post_date_precision | string | Date precision |
| post_category | string | `hiring`, `expansion`, `project_award`, `leadership_change`, or `other`. `other` means unclassified theme (body lacked those phrases), not a missing industry. Expansion scoring uses only `expansion` / `project_award` |
| first_seen_at | datetime | First stored |
| last_seen_at | datetime | Last seen |

## query_result_count

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| scan_run_id | integer | Parent scan |
| source | string | `linkedin_job` or `linkedin_post` |
| keywords | text | Query keywords |
| location | string | Query location |
| time_filter | string | `week`, `month`, or similar |
| total_results | integer | LinkedIn-reported result count when visible |
| observed_at | datetime | When the count was read |

## extraction

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| source_record_id | integer | Source card that was cleaned |
| cache_key | string | Hash used to reuse prior results |
| prompt_version | string | Prompt file version |
| model_name | string | Ollama model or `fixture` |
| result_json | json | Field objects with value, evidence, basis, confidence |
| status | string | `completed` or `failed` |
| error | text | Failure message |
| created_at | datetime | When the row was written |

## signal

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| company_id | integer | Company the signal belongs to |
| scan_run_id | integer | Scan used for detection |
| signal_type | string | Acceleration, slowdown, new location, and similar |
| strength | float | 0–1 strength |
| window | string | Comparison window label |
| basis | string | `fact`, `inference`, or `unknown` |
| created_at | datetime | Detection time |

## evidence

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| signal_id | integer | Optional parent signal |
| source_record_id | integer | Source card quoted |
| quoted_text | text | Evidence quote (already redacted at source) |
| source_url | string | Link back to the listing or post |
| observed_at | datetime | Source observation time |
| basis | string | `fact`, `inference`, or `unknown` |

## company_contact

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| company_id | integer | Employer |
| kind | string | `phone`, `email`, `address`, or `page_url` |
| value | string | Public channel as printed on the company site |
| value_normalised | string | Dedup key (casefolded email, digits-only phone) |
| label | string | `switchboard`, `careers`, `hr`, `info`, `contact`, or similar |
| source_url | string | Page the value was read from |
| source | string | `job_listing`, `website_contact`, `website_careers`, `website_leadership`, or `website_home` |
| is_organizational | boolean | True for switchboards and generic inboxes (`hr@`, `careers@`) |
| captured_at | datetime | When the row was written |

## decision_maker

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| company_id | integer | Employer |
| name | string | Person name (emails/phones in this field are redacted) |
| designation | string | Job title |
| department | string | Optional department |
| relevance_rank | integer | Partnership-control rank; 1 is best, 99 unmatched |
| profile_url | string | Optional public profile URL |
| email | string | Optional public email from a company page, CSV, or an Apollo disclose |
| phone | string | Optional public phone from a company page or CSV |
| source | string | How the row was captured (`manual`, `linkedin_people`, `website_leadership`, `apollo`) |
| verified_by | string | Who confirmed the title |
| verified_at | datetime | Set only when `verified_by` is present |
| confidence | float | 0–1 optional confidence |
| notes | text | Notes; emails/phones in this field are redacted. Apollo search stores city/country here |
| apollo_person_id | string | Unique Apollo person id used to disclose without searching again |
| email_status | string | Apollo email availability before disclose (`verified`, `unavailable`, and similar) |
| disclosed_at | datetime | When a paid Apollo email reveal was requested |

## lead_score

| Column | Type | Meaning |
| --- | --- | --- |
| id | integer | Primary key |
| company_id | integer | Scored company |
| scan_run_id | integer | Scan the score belongs to |
| total_score | integer | Weighted 0–100 total |
| priority | string | `HIGH`, `MEDIUM`, or `LOW` |
| factor_results_json | json | Per-factor score, reasons, basis, missing inputs |
| explanation_json | json | Factor copy plus missing-input list |
| explanation_text | text | One-paragraph “why this company” text |
| created_at | datetime | When the score was written |
