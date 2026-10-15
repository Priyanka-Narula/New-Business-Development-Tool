"""Streamlit dashboard for targeted AAMBRIDGE client acquisition."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import streamlit as st

from aambridge.contacts.apollo import ApolloError, disclose_person, search_company_people
from aambridge.dashboard.marketing import marketing_pack_zip, marketing_report_pdf
from aambridge.dashboard.report import REPORT_NAME
from aambridge.dashboard.queries import (
    OUTREACH_STATUSES,
    ROLLING_DAYS,
    EMPTY_SCAN_HINT,
    PipelineFilters,
    apply_pipeline_filters,
    company_detail,
    export_top50_csv,
    filter_options_from_rows,
    focus_snapshot,
    latest_completed_scan,
    pipeline_rows,
    rolling_city_chart,
    rolling_country_chart,
    rolling_hiring,
    rolling_industry_chart,
    rolling_region_chart,
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


@st.cache_data(ttl=180, show_spinner="Loading 30-day hiring from Neon...")
def _rolling_jobs(database_url: str) -> list[dict]:
    return rolling_hiring(
        _sessions(database_url),
        days=ROLLING_DAYS,
        hide_agencies=False,
    )


@st.cache_data(ttl=180, show_spinner="Preparing marketing pack...")
def _marketing_zip(database_url: str) -> bytes:
    return marketing_pack_zip(_sessions(database_url), hide_agencies=True)


@st.cache_data(ttl=180, show_spinner="Preparing PDF report...")
def _marketing_pdf(database_url: str) -> bytes:
    return marketing_report_pdf(_sessions(database_url), hide_agencies=True)


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
    hide_agencies = st.sidebar.checkbox("Hide agencies", value=True)
    score_min, score_max = st.sidebar.slider("Lead score", 0, 100, (0, 100))
    return PipelineFilters(
        countries=_multiselect("Country", options["countries"]),
        regions=_multiselect("Region", options["regions"]),
        cities=_multiselect("City", options["cities"]),
        industries=_multiselect("Industry", options["industries"]),
        seniorities=_multiselect("Seniority", options["seniorities"]),
        hiring_bands=_multiselect("Hiring activity band", options["hiring_bands"]),
        score_min=score_min,
        score_max=score_max,
        outreach_statuses=_multiselect("Outreach status", options["outreach_statuses"]),
        hide_agencies=hide_agencies,
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

    value_key = "job_count" if items and "job_count" in items[0] else "market_score"
    visible = [
        item
        for item in items
        if int(item.get(value_key) or 0) > 0
    ]
    if not visible:
        return None
    items = visible
    _chart_styles()
    top = max(int(item[value_key]) for item in items) or 1
    clicked: str | None = None
    for index, item in enumerate(items, start=1):
        label = str(item[label_key])
        score = int(item[value_key])
        width = round(100 * score / top) if top else 0
        if "job_count" in item:
            mix = f"{item['job_count']} jobs · {item['company_count']} employers"
        else:
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


def _last_scan_line(sessions) -> str:
    scan = latest_completed_scan(sessions)
    label = (scan or {}).get("label") or ""
    if not label:
        return "No completed scan yet"
    return f"Last completed scan: {label}"


def _empty_chart(sessions) -> None:
    st.info(f"{_last_scan_line(sessions)}. {EMPTY_SCAN_HINT}")


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

    if detail.get("website"):
        st.markdown(f"**Website**  {detail['website']}")
    st.markdown("**Public contacts**")
    contacts = detail.get("public_contacts") or []
    shown = [
        item
        for item in contacts
        if item.get("kind") in {"phone", "email", "address", "page_url"}
    ]
    if not shown:
        st.caption("No public company phone or email stored yet.")
    for item in shown:
        if item["kind"] == "page_url":
            continue
        st.markdown(
            f"- **{item['kind']}** · {item['label'] or '—'} · {item['value']}"
        )
        if item.get("source_url"):
            st.caption(item["source_url"])
    people = detail.get("people") or []
    st.markdown("**People**")
    if not people:
        st.caption("No named hiring contacts stored yet.")
    for person in people:
        verified = "verified" if person.get("verified") else "unverified"
        extra = " · ".join(
            part
            for part in (person.get("email"), person.get("phone"))
            if part
        )
        line = f"- {person['name']} · {person['designation']} · {verified}"
        if extra:
            line += f" · {extra}"
        elif person.get("apollo_person_id") and not person.get("disclosed"):
            status = person.get("email_status") or "unknown"
            line += f" · {status} · locked"
        st.markdown(line)
        if person.get("profile_url"):
            st.caption(person["profile_url"])
    _apollo_brief(sessions, company_id, people)

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


def _apollo_label(person: dict) -> str:
    parts = [
        person.get("name") or "Unknown",
        person.get("designation") or "",
        person.get("location") or "",
        person.get("email_status") or "unknown",
    ]
    return " · ".join(part for part in parts if part)


def _apollo_brief(sessions, company_id: int, people: list[dict]) -> None:
    st.markdown("**Apollo contacts**")
    flash_key = f"apollo-flash-{company_id}"
    if st.session_state.get(flash_key):
        kind, message = st.session_state.pop(flash_key)
        getattr(st, kind)(message)
    settings = Settings()
    if not settings.has_apollo_api_key:
        st.caption("Set APOLLO_API_KEY in .env to search Apollo and disclose emails.")
        return
    if st.button("Find Apollo people", key=f"apollo-search-{company_id}"):
        try:
            stats = search_company_people(sessions, company_id)
            places = ", ".join(stats.get("locations") or []) or "no location filter"
            st.session_state[flash_key] = (
                "success",
                f"Found {stats['found']} people for {stats['company']} "
                f"({places}). Stored {stats['stored']}.",
            )
            st.rerun()
        except (ApolloError, ValueError) as exc:
            st.error(str(exc))
            return
    undisclosed = [
        person
        for person in people
        if person.get("apollo_person_id") and not person.get("disclosed")
    ]
    if not undisclosed:
        st.caption(
            "Search uses company name and location. Emails stay locked until you disclose."
        )
        return
    labels = [_apollo_label(person) for person in undisclosed]
    chosen = st.selectbox(
        "People available to disclose",
        labels,
        key=f"apollo-pick-{company_id}",
    )
    verify = st.checkbox(
        "Also mark verified",
        key=f"apollo-verify-{company_id}",
    )
    if st.button("Disclose selected", key=f"apollo-disclose-{company_id}"):
        selected = undisclosed[labels.index(chosen)]
        try:
            result = disclose_person(
                sessions,
                int(selected["id"]),
                verified_by="recruiter" if verify else "",
            )
            if result.get("skipped"):
                st.session_state[flash_key] = (
                    "info",
                    "This contact was already disclosed.",
                )
            elif result.get("email"):
                st.session_state[flash_key] = (
                    "success",
                    f"Disclosed email for {selected['name']}.",
                )
            else:
                st.session_state[flash_key] = (
                    "warning",
                    f"Apollo returned no email for {selected['name']}. "
                    "This person will not be charged again.",
                )
            st.rerun()
        except (ApolloError, ValueError) as exc:
            st.error(str(exc))


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
        "hiring · contact · phone · email · outreach"
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
            f"{row['key_contact'] or 'No contact'}  ·  "
            f"{row.get('company_phone') or '—'}  ·  "
            f"{row.get('company_email') or '—'}  ·  {row['status']}"
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
    fresh = [row for row in rows if row.get("new_requirement")]
    if fresh:
        st.subheader("New requirements this scan")
        st.caption("Pick a company, then Find Apollo people to save names on Neon.")
        _company_list(sessions, fresh, "companies-new")
        st.subheader("All scored companies")
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


def _clear_focus_nav() -> None:
    st.session_state["focus_country"] = None
    st.session_state["focus_industry"] = None
    st.session_state["focus_city"] = None


def _focus_breadcrumbs(*, country: str | None = None, industry: str | None = None) -> None:
    crumb_all, crumb_country, crumb_industry, _rest = st.columns([1, 1, 1, 2])
    if crumb_all.button("All countries"):
        _clear_focus_nav()
        st.rerun()
    if country and crumb_country.button(country):
        st.session_state["focus_industry"] = None
        st.session_state["focus_city"] = None
        st.rerun()
    if industry and crumb_industry.button(industry):
        st.session_state["focus_city"] = None
        st.rerun()


def _apply_rolling_filters(facts: list[dict], filters: PipelineFilters) -> list[dict]:
    matched = facts
    if filters.hide_agencies:
        matched = [item for item in matched if not item.get("is_agency")]
    if filters.countries:
        matched = [item for item in matched if item.get("country") in filters.countries]
    if filters.regions:
        matched = [item for item in matched if item.get("region") in filters.regions]
    if filters.cities:
        matched = [item for item in matched if item.get("city") in filters.cities]
    if filters.industries:
        matched = [item for item in matched if item.get("industry") in filters.industries]
    return matched


def _focus_geo(sessions, rows: list[dict], rolling: list[dict]) -> None:
    country = st.session_state.get("focus_country")
    industry = st.session_state.get("focus_industry")
    city = st.session_state.get("focus_city")
    caption = (
        f"Unique jobs seen in the last {ROLLING_DAYS} days "
        f"(all completed scans on Neon). {_last_scan_line(sessions)}"
    )
    if country and industry and city:
        st.subheader(city)
        st.caption(f"{industry} in {country}")
        _focus_breadcrumbs(country=country, industry=industry)
        scoped = [
            row
            for row in rows
            if row.get("country") == country
            and row.get("industry") == industry
            and row.get("city") == city
        ]
        scoped.sort(key=lambda row: (-int(row["lead_score"]), row["company"]))
        st.caption(f"{len(scoped)} companies, strongest lead score first.")
        _company_list(sessions, scoped, f"focus-{country}-{industry}-{city}")
        return
    if country and industry:
        st.subheader(industry)
        st.caption(f"{caption} {country}, {industry}.")
        _focus_breadcrumbs(country=country, industry=industry)
        chart = rolling_city_chart(rolling, country=country, industry=industry)
        clicked = _ranking_chart(
            chart["rankings"], "city", f"focus-city-{country}-{industry}"
        )
        if clicked:
            st.session_state["focus_city"] = clicked
            st.rerun()
        if not chart["rankings"]:
            _empty_chart(sessions)
        return
    if country:
        st.subheader(country)
        st.caption(f"{caption} Ranked sectors in {country}.")
        _focus_breadcrumbs(country=country)
        scoped = [item for item in rolling if item.get("country") == country]
        chart = rolling_industry_chart(scoped)
        clicked = _ranking_chart(
            chart["rankings"], "industry", f"focus-industry-{country}"
        )
        if clicked:
            st.session_state["focus_industry"] = clicked
            st.session_state["focus_city"] = None
            st.rerun()
        if not chart["rankings"]:
            _empty_chart(sessions)
        return
    st.subheader("Countries")
    st.caption(f"{caption} Pick a country, then a booming sector, then a city.")
    chart = rolling_country_chart(rolling)
    clicked = _ranking_chart(chart["rankings"], "country", "focus-country")
    if clicked:
        st.session_state["focus_country"] = clicked
        st.session_state["focus_industry"] = None
        st.session_state["focus_city"] = None
        st.rerun()
    if not chart["rankings"]:
        _empty_chart(sessions)


def _focus_page(sessions, rows: list[dict], rolling: list[dict]) -> None:
    st.title("Where to focus")
    snapshot = focus_snapshot(rows)
    leader = snapshot["leading_industry"]
    uncontacted = snapshot["high_uncontacted"]
    if leader:
        st.markdown(
            f"**{uncontacted}** high-tier "
            f"{'company has' if uncontacted == 1 else 'companies have'} "
            f"not been contacted yet. **{leader}** leads the latest-scan "
            f"outreach ranking with a score of {snapshot['leading_score']}."
        )
    else:
        st.markdown(f"{_last_scan_line(sessions)}. {EMPTY_SCAN_HINT}")
    totals = snapshot["totals_by_band"]
    columns = st.columns(4)
    columns[0].metric("Uncontacted high", uncontacted)
    columns[1].metric("High", totals["High"])
    columns[2].metric("Medium", totals["Medium"])
    columns[3].metric("Low", totals["Low"])

    _focus_geo(sessions, rows, rolling)

    st.subheader("New requirements this scan")
    st.caption(
        "Employers with a newly seen job. Open a company and use Find Apollo people "
        "when you want named outreach contacts."
    )
    newcomers = [row for row in rows if row.get("new_requirement")]
    if newcomers:
        _company_list(sessions, newcomers, "new-req")
    else:
        st.info("No newly seen jobs on the latest completed scan.")

    st.subheader("First calls")
    st.caption("High tier, still new or being researched, strongest percentile first.")
    if snapshot["first_calls"]:
        _company_list(sessions, snapshot["first_calls"], "first-calls")
    else:
        st.info("No high-tier companies are still uncontacted.")


def _market_page(sessions, rows: list[dict], rolling: list[dict]) -> None:
    industry = st.session_state.get("market_industry")
    region = st.session_state.get("market_region")
    if industry and region:
        _region_companies_page(sessions, rows, industry, region)
        return
    if industry:
        _industry_regions_page(sessions, rolling, industry)
        return

    st.title("Market potential")
    st.caption(
        f"Unique jobs seen in the last {ROLLING_DAYS} days (all completed scans on Neon). "
        f"{_last_scan_line(sessions)}"
    )
    pdf_col, pack_col, _rest = st.columns([1, 1, 2])
    pdf_col.download_button(
        "Download PDF report",
        data=_marketing_pdf(_database_url()),
        file_name=REPORT_NAME,
        mime="application/pdf",
        help="Charts for the weekly pulse, industries, locations, roles, seniority, and themes.",
    )
    pack_col.download_button(
        "Download marketing pack",
        data=_marketing_zip(_database_url()),
        file_name="aambridge_marketing.zip",
        mime="application/zip",
        help="PDF briefing plus CSV tables. No people or contact details.",
    )
    breakdown = rolling_industry_chart(rolling)
    clicked = _ranking_chart(breakdown["rankings"], "industry", "market-industry")
    if clicked:
        _open_industry(clicked)
        st.rerun()
    if not breakdown["rankings"]:
        _empty_chart(sessions)


def _industry_regions_page(sessions, rolling: list[dict], industry: str) -> None:
    st.title(industry)
    if st.button("All industries"):
        st.session_state["market_industry"] = None
        st.session_state["market_region"] = None
        st.rerun()
    st.caption(
        f"Unique jobs in {industry} seen in the last {ROLLING_DAYS} days."
    )
    breakdown = rolling_region_chart(rolling, industry)
    clicked = _ranking_chart(breakdown["rankings"], "region", f"market-region-{industry}")
    if clicked:
        st.session_state["market_region"] = clicked
        st.rerun()
    if not breakdown["rankings"]:
        _empty_chart(sessions)


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
    if page != "Where to focus":
        _clear_focus_nav()
    if page != "Market potential":
        st.session_state["market_industry"] = None
        st.session_state["market_region"] = None
    options = filter_options_from_rows(scored)
    filters = _filters(options)
    rows = apply_pipeline_filters(scored, filters)
    rolling = _apply_rolling_filters(_rolling_jobs(_database_url()), filters)
    if page == "Where to focus":
        _focus_page(sessions, rows, rolling)
    elif page == "Companies":
        _companies_page(sessions, filters, rows)
    elif page == "Market potential":
        _market_page(sessions, rows, rolling)
    else:
        _changes_page(sessions, scored)


if __name__ == "__main__":
    main()
