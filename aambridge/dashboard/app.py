"""Streamlit dashboard for the ranked AAMBRIDGE company pipeline."""

from __future__ import annotations

import argparse
import html
from collections import defaultdict
from collections.abc import Sequence

import streamlit as st

from aambridge.dashboard.queries import (
    GRAPH_EXCLUDED_FACTORS,
    OUTREACH_STATUSES,
    PipelineFilters,
    apply_pipeline_filters,
    company_detail,
    executive_summary,
    export_top50_csv,
    filter_options_from_rows,
    pipeline_rows,
    potential_from_rows,
    set_outreach_status,
)
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.settings import Settings
from aambridge.signals.detect import what_changed as scan_changes


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
    return pipeline_rows(
        _sessions(database_url),
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
    )


def _pipeline_page(sessions, filters: PipelineFilters, rows: list[dict]) -> None:
    st.title("Companies to approach first")
    display = [
        {
            "Priority": row["priority"],
            "Company": row["company"],
            "Industry": row["industry"],
            "Country/City": row["country_city"],
            "Hiring Activity": row["hiring_activity_label"],
            "Roles Hiring": row["roles_hiring"],
            "Seniority": row["seniority"],
            "Key Contact": row["key_contact"],
            "Designation": row["designation"],
            "Lead Score": row["lead_score"],
            "Status": row["status"],
            "Last Updated": row["last_updated"],
        }
        for row in rows
    ]
    st.caption(f"{len(display)} companies after filters. Agencies hidden by default.")
    if display:
        _show_table(display)
    else:
        st.info("No scored companies match the current filters.")

    if st.button("Prepare Top 50 CSV"):
        st.session_state["top50_csv"] = export_top50_csv(sessions, filters=filters)
    if st.session_state.get("top50_csv"):
        st.download_button(
            "Download Top 50 CSV",
            data=st.session_state["top50_csv"],
            file_name="aambridge_top50.csv",
            mime="text/csv",
        )
    names = {row["company"]: row["company_id"] for row in rows}
    if names:
        selected = st.selectbox("Open company", list(names))
        if selected:
            st.session_state["company_id"] = names[selected]
        if st.button("Open why page"):
            st.session_state["page"] = "Why this company"
            st.rerun()


def _company_page(sessions) -> None:
    company_id = st.session_state.get("company_id")
    if not company_id:
        st.info("Choose a company from the pipeline table.")
        return
    detail = company_detail(sessions, company_id)
    if detail is None:
        st.error("Company not found.")
        return
    st.title("Why is this company on my list?")
    st.subheader(detail["company"])
    st.markdown(detail["explanation"] or "No score explanation yet.")
    left, right = st.columns(2)
    left.metric("Lead score", detail["lead_score"] if detail["lead_score"] is not None else "—")
    right.metric("Priority", detail["priority"] or "—")
    status = st.selectbox(
        "Outreach status",
        OUTREACH_STATUSES,
        index=OUTREACH_STATUSES.index(detail["outreach_status"])
        if detail["outreach_status"] in OUTREACH_STATUSES
        else 0,
    )
    if status != detail["outreach_status"]:
        set_outreach_status(sessions, company_id, status)
        _scored_rows.clear()
        st.success(f"Saved outreach status: {status}")

    st.markdown("### Score breakdown")
    if detail["missing_inputs"]:
        st.warning("Missing inputs: " + ", ".join(detail["missing_inputs"]))
    for factor in detail["factors"]:
        st.markdown(
            f"**{factor['factor'].replace('_', ' ').title()}** "
            f"{factor['score_0_100']:.0f}/100 [{factor['basis'].upper()}]"
        )
        st.progress(min(1.0, factor["score_0_100"] / 100))
        for reason in factor["reasons"]:
            st.caption(reason)

    st.markdown("### Evidence")
    if not detail["evidence"]:
        st.caption("No stored evidence quotes for this company.")
    for item in detail["evidence"]:
        st.markdown(
            f"- **{item['basis']}** · {item['observed_at'][:10] or 'undated'} · "
            f"{item['quoted_text']}"
        )
        if item["source_url"]:
            st.caption(item["source_url"])

    st.markdown("### Active jobs")
    if detail["active_jobs"]:
        _show_table(detail["active_jobs"])
    else:
        st.caption("No active jobs stored.")

    st.markdown("### Signals timeline")
    if detail["signals"]:
        _show_table(detail["signals"])
    else:
        st.caption("No signals stored.")

    st.markdown("### Score history")
    if detail["score_history"]:
        _show_table(detail["score_history"])


def _changes_page(sessions) -> None:
    st.title("What changed")
    changes = scan_changes(sessions)
    st.subheader("New companies")
    _show_list(
        changes.get("new_companies"),
        empty="None since the previous completed scan.",
    )
    st.subheader("Biggest score changes")
    if changes.get("score_changes"):
        _show_table(changes["score_changes"])
    else:
        st.caption("Score a second completed scan to see movements.")
    st.subheader("New signals")
    if changes.get("new_signals"):
        _show_table(changes["new_signals"])
    else:
        st.caption("No signals on the latest completed scan.")


PAGES = (
    "Pipeline",
    "Why this company",
    "What changed",
    "Executive summary",
    "Market potential",
)


def _bar_chart(
    rows: list[dict],
    *,
    category: str,
    facet: str,
    facet_title: str,
) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row[facet])].append(row)
    facet_order = sorted(
        grouped,
        key=lambda name: (
            -max(int(item["avg_score"]) for item in grouped[name]),
            name,
        ),
    )
    sections: list[str] = []
    for facet_value in facet_order:
        items = sorted(
            grouped[facet_value],
            key=lambda item: (-int(item["avg_score"]), str(item[category])),
        )
        bars: list[str] = []
        for item in items:
            score = max(0, min(100, int(item["avg_score"])))
            tooltip = (
                f"{item[facet]} / {item[category]}: "
                f"{item['avg_score']}/100 average potential, "
                f"{item['company_count']} companies"
            )
            bars.append(
                "<div class='aam-bar-row' title='"
                + html.escape(tooltip, quote=True)
                + "'><div class='aam-bar-label'>"
                + html.escape(str(item[category]))
                + "</div><div class='aam-bar-track'><div class='aam-bar-fill' "
                + f"style='width:{score}%;'></div></div>"
                + f"<div class='aam-bar-score'>{score}</div></div>"
            )
        sections.append(
            "<div class='aam-facet'><div class='aam-facet-title'>"
            + html.escape(facet_title)
            + ": "
            + html.escape(facet_value)
            + "</div>"
            + "".join(bars)
            + "</div>"
        )
    st.markdown(
        """
<style>
.aam-chart { font-family: sans-serif; font-size: 0.9rem; }
.aam-facet { margin: 0 0 1.25rem 0; }
.aam-facet-title { font-weight: 600; margin-bottom: 0.4rem; }
.aam-bar-row { display: flex; align-items: center; gap: 0.6rem; margin: 0.25rem 0; }
.aam-bar-label { width: 12rem; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aam-bar-track { flex: 1; height: 1rem; background: #e6eaf1; border-radius: 4px; overflow: hidden; }
.aam-bar-fill { height: 100%; background: #3b6fd8; }
.aam-bar-score { width: 2.2rem; text-align: right; font-variant-numeric: tabular-nums; }
</style>
"""
        + "<div class='aam-chart'>"
        + "".join(sections)
        + "</div>",
        unsafe_allow_html=True,
    )


def _potential_page(rows: list[dict], filters: PipelineFilters) -> None:
    st.title("Market potential")
    excluded = ", ".join(
        name.replace("_", " ") for name in sorted(GRAPH_EXCLUDED_FACTORS)
    )
    st.caption(
        "Charts ignore "
        + excluded
        + " and rescale the remaining factor weights to 100. "
        "Sidebar filters apply. Companies without a region, industry, country, "
        "or city are left out of the matching chart."
    )
    breakdown = potential_from_rows(rows)
    st.subheader("Industry potential by region")
    region_rows = breakdown["by_region_industry"]
    if region_rows:
        _bar_chart(
            region_rows,
            category="industry",
            facet="region",
            facet_title="Region",
        )
    else:
        st.info("No region and industry pairs match the current filters.")

    st.subheader("City potential by country")
    city_rows = breakdown["by_country_city"]
    if city_rows:
        _bar_chart(
            city_rows,
            category="city",
            facet="country",
            facet_title="Country",
        )
        for item in sorted(
            breakdown["top_city_by_country"],
            key=lambda row: (-int(row["avg_score"]), str(row["country"])),
        ):
            st.caption(
                f"Highest potential in {item['country']}: {item['city']} "
                f"({item['avg_score']}/100, {item['company_count']} companies)"
            )
    else:
        st.info("No country and city pairs match the current filters.")


def _summary_page(sessions) -> None:
    st.title("Executive summary")
    summary = executive_summary(sessions)
    columns = st.columns(4)
    columns[0].metric("HIGH", summary["totals_by_priority"]["HIGH"])
    columns[1].metric("MEDIUM", summary["totals_by_priority"]["MEDIUM"])
    columns[2].metric("LOW", summary["totals_by_priority"]["LOW"])
    columns[3].metric("Decision-makers identified", summary["decision_makers_identified"])
    st.markdown(f"Scored client companies: {summary['scored_companies']}")
    st.subheader("New prospects")
    _show_list(summary["new_prospects"])
    st.subheader("Accelerating companies")
    _show_list(summary["accelerating_companies"])
    st.subheader("Top industries")
    _show_list(summary["top_industries"])
    st.subheader("Top cities")
    _show_list(summary["top_cities"])


def main() -> None:
    st.set_page_config(page_title="AAMBRIDGE pipeline", layout="wide")
    sessions = _sessions(_database_url())
    scored = _scored_rows(_database_url())
    if "page" not in st.session_state:
        st.session_state["page"] = "Pipeline"
    page = st.sidebar.radio(
        "Page",
        PAGES,
        index=PAGES.index(st.session_state["page"])
        if st.session_state["page"] in PAGES
        else 0,
    )
    st.session_state["page"] = page
    options = filter_options_from_rows(scored)
    filters = _filters(options)
    rows = apply_pipeline_filters(scored, filters)
    if page == "Pipeline":
        _pipeline_page(sessions, filters, rows)
    elif page == "Why this company":
        _company_page(sessions)
    elif page == "What changed":
        _changes_page(sessions)
    elif page == "Market potential":
        _potential_page(rows, filters)
    else:
        _summary_page(sessions)


if __name__ == "__main__":
    main()
