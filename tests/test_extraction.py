from __future__ import annotations

from sqlalchemy import func, select

from aambridge.db.models import Extraction, SourceRecord
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.db.storage import ScanStore
from aambridge.extract.deterministic import deterministic_extract
from aambridge.extract.llm import extract_with_llm, truncate_input
from aambridge.extract.service import evaluate_labelled_csv, process_database


def _database(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'extract.db').as_posix()}")
    initialise_database(engine)
    return create_session_factory(engine)


def test_deterministic_prepass_extracts_english_and_arabic_facts() -> None:
    text = (
        "Healthcare Operations Manager in Riyadh. Minimum 5 years experience. "
        "دوام كامل، خبرة 5 سنوات."
    )
    result = deterministic_extract(
        "linkedin_job",
        text,
        {
            "job_title": "Operations Manager",
            "company_name": "Example Hospital",
            "job_location": "Riyadh, Saudi Arabia",
            "city": "Riyadh",
            "country": "Saudi Arabia",
        },
    )

    assert result.experience_years_min.value == 5
    assert result.experience_years_min.basis == "fact"
    assert result.seniority.value == "manager"
    assert result.industry.value == "healthcare"
    assert result.employment_type.value == "Full-Time"


def test_llm_hallucinated_facts_and_invalid_taxonomies_are_rejected() -> None:
    text = "Hiring a Finance Manager in Dubai."
    deterministic = deterministic_extract("linkedin_job", text, {})

    def fake_call(_prompt: str, _model: str, _timeout: int):
        return {
            "company_name_clean": {
                "value": "Imaginary Corp",
                "evidence": "Imaginary Corp",
                "basis": "fact",
                "confidence": 0.99,
            },
            "industry": {
                "value": "made_up_sector",
                "evidence": "Finance",
                "basis": "fact",
                "confidence": 0.9,
            },
            "function": {
                "value": "finance",
                "evidence": "Finance",
                "basis": "fact",
                "confidence": 0.9,
            },
        }

    result, version, rejected = extract_with_llm(
        source="linkedin_job",
        input_text=text,
        deterministic=deterministic,
        model="fixture",
        timeout=1,
        max_chars=1000,
        call=fake_call,
    )

    assert version == "job_v1"
    assert result.company_name_clean.basis == "unknown"
    assert result.industry.basis == "unknown"
    assert result.function.value == "finance"
    assert rejected == 2


def test_long_input_keeps_start_and_requirements_section() -> None:
    text = "START " + ("x" * 2000) + " Requirements: five years. END"
    truncated = truncate_input(text, 1000)

    assert truncated.startswith("START")
    assert "Requirements: five years" in truncated
    assert len(truncated) <= 1020


def test_database_extraction_saves_result_and_reuses_cache(tmp_path) -> None:
    sessions = _database(tmp_path)
    first = ScanStore.start(sessions, mode="jobs", args={})
    values = {
        "job_title": "Finance Manager",
        "company_name": "Example Bank",
        "job_location": "Dubai, United Arab Emirates",
        "job_url": "https://www.linkedin.com/jobs/view/1001",
        "description": "Full-time Finance Manager role in Dubai.",
    }
    assert first.save_job(values)
    first.finish("completed")

    calls = 0

    def fake_call(_prompt: str, _model: str, _timeout: int):
        nonlocal calls
        calls += 1
        return {}

    stats = process_database(
        sessions,
        model="fixture",
        workers=2,
        timeout=1,
        max_chars=2000,
        call=fake_call,
    )
    assert stats["processed"] == 1
    assert calls == 1

    second = ScanStore.start(sessions, mode="jobs", args={})
    assert second.save_job(values)
    second.finish("completed")
    cached = process_database(
        sessions,
        model="fixture",
        workers=2,
        timeout=1,
        max_chars=2000,
        call=fake_call,
    )

    assert cached["cached"] == 1
    assert calls == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Extraction)) == 2
        statuses = list(session.scalars(select(SourceRecord.processing_status)))
        assert statuses == ["extracted", "extracted"]
        payloads = list(session.scalars(select(Extraction.result_json)))
        assert (
            payloads[0]["job_title_clean"]["value"]
            == payloads[1]["job_title_clean"]["value"]
        )


def test_extraction_reuses_result_when_content_hash_matches(tmp_path) -> None:
    sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    shared = {
        "job_title": "Solar Engineer",
        "company_name": "Sun Co",
        "job_location": "Toronto, Canada",
        "description": "Full-time solar energy role.",
    }
    assert scan.save_job({**shared, "job_url": "https://www.linkedin.com/jobs/view/2001"})
    scan.finish("completed")
    calls = 0

    def fake_call(_prompt: str, _model: str, _timeout: int):
        nonlocal calls
        calls += 1
        return {}

    process_database(
        sessions,
        model="fixture",
        workers=1,
        timeout=1,
        max_chars=2000,
        call=fake_call,
    )
    later = ScanStore.start(sessions, mode="jobs", args={})
    assert later.save_job(
        {
            **shared,
            "job_url": "https://www.linkedin.com/jobs/view/2002",
            "query": "solar energy",
        }
    )
    later.finish("completed")
    cached = process_database(
        sessions,
        model="fixture",
        workers=1,
        timeout=1,
        max_chars=2000,
        call=fake_call,
    )
    assert cached["cached"] == 1
    assert calls == 1


def test_failed_extraction_remains_retryable_with_attempt_count(tmp_path) -> None:
    sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="posts", args={})
    assert scan.save_post(
        {
            "post_text": "We are expanding in Riyadh.",
            "post_url": "https://linkedin.com/feed/update/urn:li:activity:55",
        }
    )
    scan.finish("completed")

    def fail(_prompt: str, _model: str, _timeout: int):
        raise RuntimeError("Ollama unavailable")

    stats = process_database(
        sessions,
        model="fixture",
        workers=1,
        timeout=1,
        max_chars=2000,
        call=fail,
    )

    assert stats["failed"] == 1
    with sessions() as session:
        record = session.scalar(select(SourceRecord))
        assert record is not None
        assert record.processing_status == "failed"
        assert record.extraction_attempts == 1
        assert "Ollama unavailable" in record.processing_error


def test_eval_extraction_reports_per_field_accuracy_without_ollama(
    tmp_path,
) -> None:
    labelled = tmp_path / "labelled.csv"
    labelled.write_text(
        "source,text,expected_industry,expected_seniority,expected_city\n"
        'linkedin_job,"Healthcare Operations Manager in Riyadh. Minimum 5 years.",healthcare,manager,Riyadh\n',
        encoding="utf-8",
    )

    report = evaluate_labelled_csv(
        labelled,
        model="fixture",
        timeout=1,
        max_chars=2000,
        call=lambda *_args: {},
    )

    assert report["rows"] == 1
    assert report["per_field_accuracy"]["industry"] == 1.0
    assert report["per_field_accuracy"]["seniority"] == 1.0
    assert report["per_field_accuracy"]["city"] == 0.0
    assert 0 < report["overall_accuracy"] < 1
