"""Marketing-safe PDF briefing with the same trend charts as the dashboard."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

from aambridge.dashboard.queries import PULSE_DAYS, ROLLING_DAYS


REPORT_NAME = "aambridge_market_report.pdf"
NAVY = HexColor("#1e3a5f")
BLUE = HexColor("#3b6fd8")
TRACK = HexColor("#e6eaf1")
INK = HexColor("#1a1f2b")
MUTED = HexColor("#5b6475")
CARD = HexColor("#f4f6fa")
LINE = HexColor("#d5dbe6")
MARGIN = 16 * mm
FOOTER = 14 * mm
HEADER = 18 * mm
CHART_LIMIT = 12
HIGHLIGHT_LIMIT = 15


def _label(value: object) -> str:
    text = " ".join(str(value or "").replace("_", " ").split())
    if not text:
        return "—"
    return text[:1].upper() + text[1:]


def _number(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _score(value: object) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "0.0"


def render_marketing_report(
    tables: dict[str, list[dict[str, Any]]],
    *,
    now: datetime | None = None,
) -> bytes:
    """Draw the weekly pulse and ranking charts from marketing_tables()."""

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    builder = _Report(tables, moment)
    builder.draw()
    return builder.bytes()


class _Report:
    def __init__(self, tables: dict[str, list[dict[str, Any]]], now: datetime) -> None:
        self.tables = tables
        self.now = now
        self.buffer = io.BytesIO()
        self.width, self.height = A4
        self.canvas = Canvas(self.buffer, pagesize=A4, pageCompression=0)
        self.canvas.setTitle("AAMBRIDGE market pulse")
        self.canvas.setAuthor("AAMBRIDGE")
        self.y = self.height - HEADER
        self.page_no = 1

    def bytes(self) -> bytes:
        return self.buffer.getvalue()

    def draw(self) -> None:
        self._header()
        self._cover()
        self._pulse()
        self._industry_chart()
        self._location_chart()
        self._role_chart()
        self._seniority_chart()
        self._theme_charts()
        self._highlights()
        self._footer()
        self.canvas.save()

    def _content_width(self) -> float:
        return self.width - (2 * MARGIN)

    def _header(self) -> None:
        self.canvas.setFillColor(NAVY)
        self.canvas.rect(0, self.height - 10 * mm, self.width, 10 * mm, fill=1, stroke=0)
        self.canvas.setFillColor(white)
        self.canvas.setFont("Helvetica-Bold", 9)
        self.canvas.drawString(MARGIN, self.height - 6.6 * mm, "AAMBRIDGE  ·  Market pulse")
        self.canvas.setFont("Helvetica", 8)
        self.canvas.drawRightString(
            self.width - MARGIN,
            self.height - 6.6 * mm,
            f"Last {ROLLING_DAYS} days",
        )
        self.y = self.height - HEADER - 4 * mm

    def _footer(self) -> None:
        self.canvas.setFillColor(LINE)
        self.canvas.rect(MARGIN, 9 * mm, self._content_width(), 0.4, fill=1, stroke=0)
        self.canvas.setFillColor(MUTED)
        self.canvas.setFont("Helvetica", 7.5)
        self.canvas.drawString(
            MARGIN,
            5.5 * mm,
            "Market insight only — no Aambridge fit, lead score, people, or contacts.",
        )
        self.canvas.drawRightString(
            self.width - MARGIN,
            5.5 * mm,
            f"{self.page_no}",
        )

    def _new_page(self) -> None:
        self._footer()
        self.canvas.showPage()
        self.page_no += 1
        self._header()

    def _ensure(self, needed: float) -> None:
        if self.y - needed < FOOTER + 4 * mm:
            self._new_page()

    def _gap(self, size: float = 6) -> None:
        self.y -= size

    def _cover(self) -> None:
        self.canvas.setFillColor(INK)
        self.canvas.setFont("Helvetica-Bold", 20)
        self.canvas.drawString(MARGIN, self.y, "Hiring trends briefing")
        self.y -= 16
        self.canvas.setFillColor(MUTED)
        self.canvas.setFont("Helvetica", 10)
        self.canvas.drawString(
            MARGIN,
            self.y,
            f"Generated {self.now.strftime('%d %B %Y')}  ·  "
            f"{ROLLING_DAYS}-day rolling window plus a {PULSE_DAYS}-day pulse",
        )
        self.y -= 18

    def _pulse(self) -> None:
        pulse = (self.tables.get("marketing_weekly_pulse") or [{}])[0]
        self._section("Weekly pulse")
        cards = (
            ("Jobs · 15 days", pulse.get("jobs_15d")),
            ("Jobs · 30 days", pulse.get("jobs_30d")),
            ("Employers · 15 days", pulse.get("employers_15d")),
            ("Employers · 30 days", pulse.get("employers_30d")),
            ("New employers", pulse.get("new_employers")),
            ("Leading industry", _label(pulse.get("leading_industry"))),
            ("Leading city", _label(pulse.get("leading_city"))),
            ("Market insight", _score(pulse.get("avg_market_insight"))),
            ("Hiring activity", _score(pulse.get("avg_hiring_activity"))),
            ("Industry opportunity", _score(pulse.get("avg_industry_opportunity"))),
            ("Seniority match", _score(pulse.get("avg_seniority_match"))),
        )
        self._kpi_cards(cards)
        self._gap(10)

    def _section(self, title: str, caption: str = "") -> None:
        self._ensure(28)
        self.canvas.setFillColor(NAVY)
        self.canvas.setFont("Helvetica-Bold", 12)
        self.canvas.drawString(MARGIN, self.y, title)
        self.y -= 14
        if caption:
            self.canvas.setFillColor(MUTED)
            self.canvas.setFont("Helvetica", 8)
            self.canvas.drawString(MARGIN, self.y, caption)
            self.y -= 12

    def _kpi_cards(self, cards: tuple[tuple[str, object], ...]) -> None:
        columns = 4
        gap = 6
        width = (self._content_width() - (gap * (columns - 1))) / columns
        height = 36
        for index, (title, value) in enumerate(cards):
            column = index % columns
            if column == 0:
                self._ensure(height + 8)
            x = MARGIN + column * (width + gap)
            y = self.y - height
            self.canvas.setFillColor(CARD)
            self.canvas.roundRect(x, y, width, height, 3, fill=1, stroke=0)
            self.canvas.setFillColor(MUTED)
            self.canvas.setFont("Helvetica", 7.5)
            self.canvas.drawString(x + 8, y + 22, title)
            self.canvas.setFillColor(NAVY)
            self.canvas.setFont("Helvetica-Bold", 12)
            text = value if isinstance(value, str) else _number(value)
            self.canvas.drawString(x + 8, y + 8, text[:28])
            if column == columns - 1 or index == len(cards) - 1:
                self.y -= height + 6

    def _industry_chart(self) -> None:
        rows = [
            item
            for item in self.tables.get("marketing_industry_30d") or []
            if int(item.get("job_count") or 0) > 0
        ]
        if not rows:
            return
        self._section(
            "Strongest industries",
            f"Unique jobs seen in the last {ROLLING_DAYS} days. "
            "Market insight is hiring, industry, and seniority — not Aambridge fit.",
        )
        self._bars(
            rows[:CHART_LIMIT],
            label_key="industry",
            value_key="job_count",
            extra=lambda item: (
                f"{_number(item['job_count'])} jobs  ·  "
                f"{_number(item.get('employer_count', 0))} employers  ·  "
                f"insight {_score(item.get('avg_market_insight'))}"
            ),
        )

    def _location_chart(self) -> None:
        rows = [
            item
            for item in self.tables.get("marketing_location_30d") or []
            if int(item.get("job_count") or 0) > 0
        ]
        if not rows:
            return
        self._section(
            "Cities and countries",
            f"Unique jobs by location in the last {ROLLING_DAYS} days. "
            "Caption shows market insight (no Aambridge fit).",
        )
        labelled = []
        for item in rows[:CHART_LIMIT]:
            place = ", ".join(
                part
                for part in (item.get("city"), item.get("country"))
                if part
            )
            labelled.append({**item, "place": place or item.get("region") or "—"})
        self._bars(
            labelled,
            label_key="place",
            value_key="job_count",
            extra=lambda item: (
                f"{_number(item['job_count'])} jobs  ·  "
                f"{_number(item.get('employer_count', 0))} employers  ·  "
                f"insight {_score(item.get('avg_market_insight'))}"
            ),
        )

    def _role_chart(self) -> None:
        rows = [
            item
            for item in self.tables.get("marketing_roles_30d") or []
            if int(item.get("count") or 0) > 0
        ]
        if not rows:
            return
        self._section(
            "Most advertised roles",
            f"Top titles in the last {ROLLING_DAYS} days.",
        )
        self._bars(
            rows[:CHART_LIMIT],
            label_key="job_title",
            value_key="count",
            extra=lambda item: (
                f"{_number(item['count'])}  ·  "
                f"{_label(item.get('top_industry'))}  ·  "
                f"{_label(item.get('top_country'))}"
            ),
        )

    def _seniority_chart(self) -> None:
        rows = [
            item
            for item in self.tables.get("marketing_seniority_30d") or []
            if int(item.get("job_count") or 0) > 0
        ]
        if not rows:
            return
        self._section(
            "Seniority mix",
            "Share of unique jobs in the rolling window.",
        )
        self._bars(
            rows,
            label_key="seniority",
            value_key="job_count",
            extra=lambda item: (
                f"{_number(item['job_count'])} jobs  ·  {item.get('share_pct', 0)}%"
            ),
        )

    def _theme_charts(self) -> None:
        themes = self.tables.get("marketing_themes_30d") or []
        posts = [
            item
            for item in themes
            if item.get("kind") == "post_category" and int(item.get("count") or 0) > 0
        ]
        signals = [
            item
            for item in themes
            if item.get("kind") == "signal_type" and int(item.get("count") or 0) > 0
        ]
        if posts:
            self._section(
                "Post themes",
                f"Public post categories in the last {ROLLING_DAYS} days.",
            )
            self._bars(posts, label_key="name", value_key="count")
        if signals:
            self._section(
                "Hiring signals",
                "Employers with each signal type in the rolling window.",
            )
            self._bars(signals, label_key="name", value_key="count")

    def _highlights(self) -> None:
        rows = self.tables.get("marketing_employer_highlights") or []
        if not rows:
            return
        self._section(
            "Employers hiring",
            "Public labour-market names only. No scores or contacts.",
        )
        columns = (
            ("company", "Company", 0.28),
            ("industry", "Industry", 0.18),
            ("country_city", "Location", 0.20),
            ("active_jobs", "Jobs", 0.08),
            ("roles_sample", "Roles", 0.26),
        )
        self._table(rows[:HIGHLIGHT_LIMIT], columns)

    def _bars(
        self,
        rows: list[dict[str, Any]],
        *,
        label_key: str,
        value_key: str,
        extra=None,
    ) -> None:
        if not rows:
            return
        values = [max(0, int(item.get(value_key) or 0)) for item in rows]
        top = max(values) or 1
        label_w = 42 * mm
        value_w = 14 * mm
        track_w = self._content_width() - label_w - value_w - 10
        row_h = 22 if extra else 16
        for item, value in zip(rows, values, strict=True):
            self._ensure(row_h + 2)
            y = self.y - 11
            self.canvas.setFillColor(INK)
            self.canvas.setFont("Helvetica-Bold", 8)
            self.canvas.drawString(
                MARGIN,
                y + (4 if extra else 0),
                _label(item.get(label_key))[:36],
            )
            track_x = MARGIN + label_w
            self.canvas.setFillColor(TRACK)
            self.canvas.roundRect(track_x, y - 1, track_w, 10, 2, fill=1, stroke=0)
            fill = track_w * (value / top) if top else 0
            if fill > 0:
                self.canvas.setFillColor(BLUE)
                self.canvas.roundRect(track_x, y - 1, max(fill, 3), 10, 2, fill=1, stroke=0)
            self.canvas.setFillColor(NAVY)
            self.canvas.setFont("Helvetica-Bold", 8)
            self.canvas.drawRightString(
                MARGIN + self._content_width(),
                y + 1,
                _number(value),
            )
            if extra:
                self.canvas.setFillColor(MUTED)
                self.canvas.setFont("Helvetica", 7)
                self.canvas.drawString(track_x, y - 11, extra(item))
            self.y -= row_h
        self._gap(8)

    def _table(
        self,
        rows: list[dict[str, Any]],
        columns: tuple[tuple[str, str, float], ...],
    ) -> None:
        header_h = 16
        row_h = 18
        width = self._content_width()
        self._ensure(header_h + row_h)
        self._table_header(columns, width, header_h)
        for index, row in enumerate(rows):
            self._ensure(row_h)
            y = self.y - row_h
            if index % 2 == 0:
                self.canvas.setFillColor(CARD)
                self.canvas.rect(MARGIN, y, width, row_h, fill=1, stroke=0)
            x = MARGIN
            self.canvas.setFillColor(INK)
            self.canvas.setFont("Helvetica", 7.5)
            for key, _title, share in columns:
                col_w = width * share
                raw = row.get(key)
                text = _number(raw) if key == "active_jobs" else _label(raw)
                self.canvas.drawString(x + 4, y + 5, text[: int(col_w / 4.2)])
                x += col_w
            self.y -= row_h
        self._gap(8)

    def _table_header(
        self,
        columns: tuple[tuple[str, str, float], ...],
        width: float,
        height: float,
    ) -> None:
        y = self.y - height
        self.canvas.setFillColor(NAVY)
        self.canvas.rect(MARGIN, y, width, height, fill=1, stroke=0)
        self.canvas.setFillColor(white)
        self.canvas.setFont("Helvetica-Bold", 7.5)
        x = MARGIN
        for _key, title, share in columns:
            col_w = width * share
            self.canvas.drawString(x + 4, y + 5, title)
            x += col_w
        self.y -= height
