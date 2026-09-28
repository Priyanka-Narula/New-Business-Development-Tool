# AAMBRIDGE pilot — final report

## What was built

A local, explainable company pipeline for AAMBRIDGE. Collection stays in a
visible Chrome window. Scoring is deterministic. Personal emails and phone
numbers are stripped at the storage boundary.

| Phase | Delivered |
| --- | --- |
| 0 | `.env` settings, rotating logs, no browser-evasion flags |
| 1 | SQLAlchemy models, incremental scan commits, SQLite default |
| 2 | Canonical job/post keys and shared helpers |
| 3–4 | Jobs and posts collectors with YAML selectors |
| 5 | Deterministic extraction plus optional local Ollama |
| 6 | Company resolution, groups, overrides, merge review |
| 7 | Signals and “what changed” |
| 8 | Weighted lead scores with factor reasons |
| 9 | Manual decision-makers (no profile scrape, no contact columns) |
| 10 | Streamlit dashboard: pipeline, company “why”, changes, summary, Top 50 export, outreach status |
| 11 | README run book, data dictionary, fixture tests, this report |

Management path: open the dashboard, sort by priority and score, then open
**Why is this company on my list?** for factor bars, source-dated evidence,
jobs, signals, and score history.

## Test results

`python -m pytest tests -q` from the project `.venv` (no live LinkedIn or
Ollama):

- 81 passed after Phases 10–11
- Fixture path covers records → extraction → companies → signals → scores →
  dashboard rows, company detail, Emiratisation view, and Top 50 CSV
- Privacy tests confirm emails/phones are not stored or exported

## Open risks

- **LinkedIn layout changes.** Card selectors in YAML and the collectors will
  break when LinkedIn ships markup changes. There is no anti-detection work;
  the browser is meant to look like a normal user session.
- **Arabic extraction quality.** Deterministic rules cover common phrases.
  Ollama quality depends on the local `llama3.1` model and still cannot invent
  facts: evidence must appear in the source text.
- **Placeholder market-model values.** Scores in
  `taxonomies/market_model.yaml` are marked placeholders. Do not treat HIGH /
  MEDIUM lists as production BD advice until those figures are replaced with
  dated research.
- **Decision-makers are manual.** Titles are ranked only after a person enters
  them. Unverified rows do not lift the accessibility factor.
- **Local `.env` credentials.** Copying `.env.example` to `.env` and adding a
  password will make `Settings()` see those values. Tests isolate `_env_file`.
  Do not commit `.env`.
- **Windows Application Control.** Some machines block Streamlit’s PyArrow
  renderer. The dashboard draws tables in Markdown so it still opens locally.

## Suggested next steps

1. Replace market-model placeholders with dated country/industry research.
2. Move schema changes from `create_all` + additive alters to Alembic before
   the database is treated as production.
3. Add an outreach-activity log (who changed status, when) if more than one
   recruiter uses the dashboard.
4. Review Top 50 exports with BD before first client outreach.
5. Keep scrape volumes low and stay inside LinkedIn terms and local law.
