"""Streamlit dashboard for targeted AAMBRIDGE client acquisition."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import streamlit as st

from aambridge.dashboard.queries import (
    OUTREACH_STATUSES,
    PipelineFilters,
    apply_pipeline_filters,
    company_detail,
    export_top50_csv,
    filter_options_from_rows,
    focus_snapshot,
    industry_rankings,
    pipeline_rows,
    region_rankings,
    set_outreach_status,
)
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.scoring.bands import refresh_acquisition_bands
from aambridge.settings import Settings
from aambridge.signals.detect import what_changed as scan_changes


PAGES = (
    "Where to focus",
    "Companies",
    "Market potential",
    "What changed",
)


def _database_url() -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--database-url")
    args, _remaining = parser.parse_known_args()
    return args.database_url or Settings().database_url


@st.cache_resource
def _sessions(database_url: str):
    engine = create_database_engine(database_url)
    initialise_database(engine, scrub_personal_contacts=False)
    return create_session_factory(engine)


@st.cache_data(ttl=180, show_spinner="Loading companies from Neon...")
def _scored_rows(database_url: str) -> list[dict]:
    sessions = _sessions(database_url)
    refresh_acquisition_bands(sessions)
    return pipeline_rows(
        sessions,
        PipelineFilters(hide_agencies=False),
    )


def _cell(value: object) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def _show_table(rows: list[dict]) -> None:
    """Render a table without Streamlit's Arrow/pyarrow path."""

    if not rows:
        return
    columns = list(rows[0])
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = "\n".join(
        "| " + " | ".join(_cell(row.get(column, "")) for column in columns) + " |"
        for row in rows
    )
    st.markdown(f"{header}\n{divider}\n{body}")


def _show_list(values: Sequence[object] | None, empty: str = "None") -> None:
    """Render a list as Markdown. st.write(list) imports pyarrow."""

    items = [str(item) for item in (values or []) if str(item).strip()]
    if not items:
        st.caption(empty)
        return
    st.markdown("\n".join(f"- {item}" for item in items))


def _multiselect(label: str, options: list[str]) -> tuple[str, ...]:
    if not options:
        return ()
    selected = st.sidebar.multiselect(label, options)
    return tuple(selected)


def _filters(options: dict[str, list[str]]) -> PipelineFilters:
    st.sidebar.header("Filters")
    preset = st.sidebar.selectbox(
        "Saved view",
        (
            "All scored companies",
            "Top approach list: UAE and Riyadh",
            "Emiratisation: mainland UAE, 50+ staff, nationality-restricted",
        ),
    )
    preset_key = {
        "Top approach list: UAE and Riyadh": "uae_riyadh",
        "Emiratisation: mainland UAE, 50+ staff, nationality-restricted": "emiratisation",
    }.get(preset)
    hide_agencies = st.sidebar.checkbox("Hide agencies", value=True)
    score_min, score_max = st.sidebar.slider("Lead score", 0, 100, (0, 100))
    return PipelineFilters(
        countries=_multiselect("Country", options["countries"]),
        regions=_multiselect("Region", options["regions"]),
        cities=_multiselect("City", options["cities"]),
        industries=_multiselect("Industry", options["industries"]),
        seniorities=_multiselect("Seniority", options["seniorities"]),
        hiring_bands=_multiselect("Hiring activity band", options["hiring_bands"]),
        size_bands=_multiselect("Company size", options["size_bands"]),
        score_min=score_min,
        score_max=score_max,
        outreach_statuses=_multiselect("Outreach status", options["outreach_statuses"]),
        hide_agencies=hide_agencies,
        preset=preset_key,
        acquisition_bands=_multiselect("Target tier", options["acquisition_bands"]),
    )


def _chart_styles() -> None:
    st.markdown(
        """
<style>
.aam-chart { font-family: sans-serif; font-size: 0.9rem; }
.aam-bar-row { display: flex; align-items: center; gap: 0.6rem; margin: 0.15rem 0; }
.aam-bar-label { width: 14rem; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.aam-bar-track { flex: 1; height: 1.1rem; background: #e6eaf1; border-radius: 4px; overflow: hidden; }
.aam-bar-fill { height: 100%; background: #3b6fd8; }
.aam-bar-score { width: 2.6rem; text-align: right; font-variant-numeric: tabular-nums; }
</style>
""",
        unsafe_allow_html=True,
    )


def _ranking_chart(items: list[dict], label_key: str, key_prefix: str) -> str | None:
    """Clickable ranking. Bar length is relative to the leader in this chart."""

    if not items:
        return None
    _chart_styles()
    top = max(int(item["market_score"]) for item in items) or 1
    clicked: str | None = None
    for index, item in enumerate(items, start=1):
        label = str(item[label_key])
        score = int(item["market_score"])
        width = round(100 * score / top)
        mix = f"{item['high']} High · {item['medium']} Medium · {item['low']} Low"
        name_col, bar_col = st.columns([2, 5])
        if name_col.button(f"{index}. {label}", key=f"{key_prefix}-{index}-{label}"):
            clicked = label
        bar_col.markdown(
            "<div class='aam-bar-row'><div class='aam-bar-track'><div class='aam-bar-fill' "
            + f"style='width:{width}%;'></div></div>"
            + f"<div class='aam-bar-score'>{score}</div></div>",
            unsafe_allow_html=True,
        )
        st.caption(mix)
    return clicked


def _percentile_label(value: object) -> str:
    if value is None or value == "":
        return "—"
    return f"{float(value):.0f}"


def _company_brief(sessions, company_id: int) -> None:
    detail = company_detail(sessions, company_id)
    if detail is None:
        st.error("Company not found.")
        return
    st.markdown(f"#### {detail['company']}")
    st.markdown(detail["explanation"] or "No score explanation yet.")
    columns = st.columns(4)
    columns[0].metric("Target tier", detail["acquisition_band"] or "—")
    columns[1].metric("Percentile", _percentile_label(detail["lead_score_percentile"]))
    columns[2].metric(
        "Lead score",
        detail["lead_score"] if detail["lead_score"] is not None else "—",
    )
    columns[3].metric("Fit gate", detail["priority"] or "—")
    status = st.selectbox(
        "Outreach status",
        OUTREACH_STATUSES,
        index=OUTREACH_STATUSES.index(detail["outreach_status"])
        if detail["outreach_status"] in OUTREACH_STATUSES
        else 0,
        key=f"outreach-{company_id}",
    )
    if status != detail["outreach_status"]:
        set_outreach_status(sessions, company_id, status)
        _scored_rows.clear()
        st.success(f"Saved outreach status: {status}")
        st.rerun()

    st.markdown("**Why this score**")
    if detail["missing_inputs"]:
        st.warning("Missing inputs: " + ", ".join(detail["missing_inputs"]))
    if not detail["factors"]:
        st.caption("No factor breakdown stored.")
    for factor in detail["factors"]:
        st.markdown(
            f"**{factor['factor'].replace('_', ' ').title()}** "
            f"{factor['score_0_100']:.0f}/100 [{factor['basis'].upper()}]"
        )
        for reason in factor["reasons"]:
            st.caption(reason)

    st.markdown("**Evidence**")
    if not detail["evidence"]:
        st.caption("No stored evidence quotes for this company.")
    for item in detail["evidence"]:
        observed = (item["observed_at"] or "")[:10] or "undated"
        st.markdown(f"- **{item['basis']}** · {observed} · {item['quoted_text']}")
        if item["source_url"]:
            st.caption(item["source_url"])


def _toggle_company(company_id: int) -> None:
    current = st.session_state.get("expanded_company_id")
    st.session_state["expanded_company_id"] = (
        None if current == company_id else company_id
    )


def _company_list(sessions, rows: list[dict], key_prefix: str) -> None:
    if not rows:
        st.info("No scored companies match the current filters.")
        return
    st.caption(
        "Target tier · percentile · lead score · industry · region · location · "
        "hiring · contact · outreach"
    )
    expanded = st.session_state.get("expanded_company_id")
    for row in rows:
        company_id = int(row["company_id"])
        label = (
            f"{row['company']}  ·  {row.get('acquisition_band') or '—'}  ·  "
            f"{_percentile_label(row.get('lead_score_percentile'))}  ·  "
            f"{row['lead_score']}  ·  {row['industry'] or '—'}  ·  "
            f"{row['region'] or '—'}  ·  {row['country_city'] or '—'}  ·  "
            f"{row['hiring_activity_label']}  ·  "
            f"{row['key_contact'] or 'No contact'}  ·  {row['status']}"
        )
        if st.button(label, key=f"{key_prefix}-{company_id}"):
            _toggle_company(company_id)
            st.rerun()
        if expanded == company_id:
            _company_brief(sessions, company_id)


def _companies_page(sessions, filters: PipelineFilters, rows: list[dict]) -> None:
    st.title("Companies")
    st.caption(
        f"{len(rows)} companies after filters. "
        "Target tier compares each company with every scored company. "
        "Agencies stay hidden until the sidebar checkbox is cleared."
    )
    _company_list(sessions, rows, "companies")
    if st.button("Prepare Top 50 CSV"):
        st.session_state["top50_csv"] = export_top50_csv(sessions, filters=filters)
    if st.session_state.get("top50_csv"):
        st.download_button(
            "Download Top 50 CSV",
            data=st.session_state["top50_csv"],
            file_name="aambridge_top50.csv",
            mime="text/csv",
        )


def _open_industry(industry: str) -> None:
    st.session_state["nav_jump"] = "Market potential"
    st.session_state["market_industry"] = industry
    st.session_state["market_region"] = None


def _focus_page(sessions, rows: list[dict]) -> None:
    st.title("Where to focus")
    snapshot = focus_snapshot(rows)
    leader = snapshot["leading_industry"]
    uncontacted = snapshot["high_uncontacted"]
    if leader:
        st.markdown(
            f"**{uncontacted}** high-tier "
            f"{'company has' if uncontacted == 1 else 'companies have'} "
            f"not been contacted yet. **{leader}** leads the market ranking "
            f"with a score of {snapshot['leading_score']}."
        )
    else:
        st.markdown("Score a scan to see which industry and companies to approach first.")
    totals = snapshot["totals_by_band"]
    columns = st.columns(4)
    columns[0].metric("Uncontacted high", uncontacted)
    columns[1].metric("High", totals["High"])
    columns[2].metric("Medium", totals["Medium"])
    columns[3].metric("Low", totals["Low"])

    st.subheader("Strongest industries")
    st.caption("Score = 3 × High + 1 × Medium. Low companies are counted but do not raise the rank.")
    clicked = _ranking_chart(snapshot["top_industries"], "industry", "focus-industry")
    if clicked:
        _open_industry(clicked)
        st.rerun()
    if not snapshot["top_industries"]:
        st.info("No industries match the current filters.")

    st.subheader("First calls")
    st.caption("High tier, still new or being researched, strongest percentile first.")
    if snapshot["first_calls"]:
        _company_list(sessions, snapshot["first_calls"], "first-calls")
    else:
        st.info("No high-tier companies are still uncontacted.")


def _market_page(sessions, rows: list[dict]) -> None:
    industry = st.session_state.get("market_industry")
    region = st.session_state.get("market_region")
    if industry and region:
        _region_companies_page(sessions, rows, industry, region)
        return
    if industry:
        _industry_regions_page(rows, industry)
        return

    st.title("Market potential")
    st.caption(
        "Industries ranked by target-tier mix: 3 × High + 1 × Medium. "
        "Bar length is relative to the leading industry. "
        "Sidebar filters choose which companies are counted. "
        "They do not change who is High."
    )
    breakdown = industry_rankings(rows)
    if breakdown["excluded"]:
        st.caption(
            f"{breakdown['excluded']} companies have no industry or no target tier "
            "and are left out of this ranking."
        )
    clicked = _ranking_chart(breakdown["rankings"], "industry", "market-industry")
    if clicked:
        _open_industry(clicked)
        st.rerun()
    if not breakdown["rankings"]:
        st.info("No industries match the current filters.")


def _industry_regions_page(rows: list[dict], industry: str) -> None:
    st.title(industry)
    if st.button("All industries"):
        st.session_state["market_industry"] = None
        st.session_state["market_region"] = None
        st.rerun()
    st.caption(
        f"Regions in {industry}, ranked the same way: 3 × High + 1 × Medium."
    )
    breakdown = region_rankings(rows, industry)
    if breakdown["excluded"]:
        st.caption(
            f"{breakdown['excluded']} companies in {industry} have no region "
            "or no target tier and are left out of this ranking."
        )
    clicked = _ranking_chart(breakdown["rankings"], "region", f"market-region-{industry}")
    if clicked:
        st.session_state["market_region"] = clicked
        st.rerun()
    if not breakdown["rankings"]:
        st.info(f"No regions in {industry} match the current filters.")


def _region_companies_page(
    sessions,
    rows: list[dict],
    industry: str,
    region: str,
) -> None:
    st.title(f"{region}")
    st.caption(industry)
    crumb_all, crumb_industry, _rest = st.columns([1, 1, 2])
    if crumb_all.button("All industries"):
        st.session_state["market_industry"] = None
        st.session_state["market_region"] = None
        st.rerun()
    if crumb_industry.button(industry):
        st.session_state["market_region"] = None
        st.rerun()
    scoped = [
        row
        for row in rows
        if row.get("industry") == industry and row.get("region") == region
    ]
    scoped.sort(key=lambda row: (-int(row["lead_score"]), row["company"]))
    st.caption(f"{len(scoped)} companies, strongest lead score first.")
    _company_list(sessions, scoped, f"region-{industry}-{region}")


def _changes_page(sessions, scored: list[dict]) -> None:
    st.title("What changed")
    changes = scan_changes(sessions)
    by_id = {row["company_id"]: row for row in scored}
    by_name = {row["company"]: row for row in scored}
    st.subheader("New companies")
    newcomers = []
    for name in changes.get("new_companies") or []:
        band = (by_name.get(name) or {}).get("acquisition_band") or "—"
        newcomers.append(f"{name} · {band}")
    _show_list(newcomers, empty="None since the previous completed scan.")
    st.subheader("Biggest score changes")
    movements = []
    for item in changes.get("score_changes") or []:
        row = by_id.get(item["company_id"]) or {}
        movements.append(
            {
                "Company": row.get("company") or item["company_id"],
                "Target tier": row.get("acquisition_band") or "—",
                "Previous": item["previous"],
                "Current": item["current"],
                "Change": item["change"],
            }
        )
    if movements:
        _show_table(movements)
    else:
        st.caption("Score a second completed scan to see movements.")
    st.subheader("New signals")
    if changes.get("new_signals"):
        _show_table(changes["new_signals"])
    else:
        st.caption("No signals on the latest completed scan.")


def main() -> None:
    st.set_page_config(page_title="AAMBRIDGE acquisition", layout="wide")
    sessions = _sessions(_database_url())
    scored = _scored_rows(_database_url())
    if st.session_state.get("nav_page") not in PAGES:
        st.session_state["nav_page"] = "Where to focus"
    jump = st.session_state.pop("nav_jump", None)
    if jump in PAGES:
        st.session_state["nav_page"] = jump
    page = st.sidebar.radio("Page", PAGES, key="nav_page")
    if page != "Market potential":
        st.session_state["market_industry"] = None
        st.session_state["market_region"] = None
    options = filter_options_from_rows(scored)
    filters = _filters(options)
    rows = apply_pipeline_filters(scored, filters)
    if page == "Where to focus":
        _focus_page(sessions, rows)
    elif page == "Companies":
        _companies_page(sessions, filters, rows)
    elif page == "Market potential":
        _market_page(sessions, rows)
    else:
        _changes_page(sessions, scored)


if __name__ == "__main__":
    main()
