# Marketing reports

Use this pack for social posts. After each weekly pipeline, send marketing
`output/marketing/` — not the sales downloads.

The pack is counts, themes, and a **market insight** score. Market insight is
hiring activity, industry opportunity, and seniority mix only. It is **not**
the Aambridge lead score and it does **not** include `aambridge_fit`, agency
potential, or decision-maker access.

## Do not publish

Keep these with BD and recruitment. They contain people, contacts, or personal
posts.

- `output/aambridge_top50.csv` — names, emails, phones, outreach status, lead score
- `output/linkedin_me_posts.csv` — author names, profile URLs, full post text
- Dashboard company brief: People, Apollo, public contacts
- CLI `decision-makers` and Apollo disclose output
- Raw `output/linkedin_me_jobs.csv` as a contact or hit list
- Aambridge fit, lead score, or acquisition band on any named employer

## What to send marketing

`python -m aambridge export-marketing output/marketing`

The same folder is written by `run_pipeline.py`. On the dashboard, open
**Market potential** and use **Download PDF report** (`aambridge_market_report.pdf`)
or **Download marketing pack** (`aambridge_marketing.zip`, CSVs plus the PDF).

| File | Columns | Social use |
| --- | --- | --- |
| `aambridge_market_report.pdf` | Weekly pulse KPIs (including market insight), bar charts for industries, locations, roles, seniority, and themes; employer names only in the last table | One-pager or carousel source |
| `marketing_weekly_pulse.csv` | 15-day and 30-day job/employer counts, leading industry and city, new employers, post and signal counts, plus `avg_market_insight`, `avg_hiring_activity`, `avg_industry_opportunity`, `avg_seniority_match` | Weekly carousel: “This week in hiring” |
| `marketing_industry_30d.csv` | `industry`, `job_count`, `employer_count`, and the four market-insight averages | Hottest-sector graphic |
| `marketing_location_30d.csv` | `country`, `region`, `city`, `job_count`, `employer_count`, and the four market-insight averages | City or country comparison |
| `marketing_roles_30d.csv` | `job_title`, `count`, `top_industry`, `top_country` (top 50 titles) | Most advertised roles |
| `marketing_seniority_30d.csv` | `seniority`, `job_count`, `share_pct` | Director+ vs specialist mix |
| `marketing_themes_30d.csv` | `kind` (`post_category` or `signal_type`), `name`, `count`. Post `other` is labelled “Unclassified (not hiring, expansion, award, or leadership)” — not a missing sector | Expansion, awards, localisation |
| `marketing_employer_highlights.csv` | `company`, `industry`, `country_city`, `active_jobs`, `roles_sample` | Optional “who is hiring” names only |

Job and employer counts are the last 30 days of unique jobs. Market-insight
averages come from the latest completed scan’s factor scores (agencies hidden).

`marketing_employer_highlights.csv` is the only named-employer file. Treat names
as public labour-market facts. It stays **score-free**: no lead score, no
Aambridge fit, no market insight, no contacts, no outreach status.

None of these files include person names, emails, phones, profile URLs, post
bodies, lead scores, Aambridge fit, or outreach status.

## How often

There is no scheduler. Produce the pack after the scrape you already run
(`--time-filter week` by default). Keep the database. Do not run `reset-db`.

| Rhythm | Produce | Publish |
| --- | --- | --- |
| Weekly | All seven CSVs plus the PDF after `--scrape --time-filter week` | 2–4 LinkedIn posts |
| Monthly | Same files; compare the last four weekly pulses | One carousel or one-pager |
| Ad hoc | Filter a CSV to one sector | Campaign posts |

Do not post daily. The rolling window is 30 days; daily posts repeat the same
numbers.

Suggested weekly set, copied from these CSVs only:

1. Sector leader plus job, employer, and market-insight averages
2. City or country comparison
3. Top five role titles
4. One theme: expansion, project awards, or localisation pressure. Default scrape `--query-set hiring` stores unmatched posts as unclassified (`other`). To collect expansion/award posts, scrape with `--query-set all` or `--query-set expansion`.
