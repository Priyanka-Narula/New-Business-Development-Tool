"""Extraction orchestration for database and ad-hoc CSV inputs."""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import Extraction, SourceRecord
from aambridge.extract.deterministic import deterministic_extract
from aambridge.extract.llm import (
    PROMPT_VERSIONS,
    extract_with_llm,
    ollama_call,
)
from aambridge.extract.schema import ExtractionResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceInput:
    id: int
    source: str
    raw_text: str
    raw_fields: dict[str, Any]
    input_text: str
    cache_key: str
    prompt_version: str


def extraction_cache_key(input_text: str, prompt_version: str, model: str) -> str:
    value = f"{input_text}\0{prompt_version}\0{model}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_input(record: SourceRecord, model: str) -> SourceInput:
    input_text = (
        f"{record.raw_text}\nRAW_FIELDS:\n"
        + json.dumps(record.raw_fields_json, ensure_ascii=False, default=str)
    )
    prompt_version = PROMPT_VERSIONS[record.source]
    return SourceInput(
        id=record.id,
        source=record.source,
        raw_text=record.raw_text,
        raw_fields=dict(record.raw_fields_json),
        input_text=input_text,
        cache_key=extraction_cache_key(input_text, prompt_version, model),
        prompt_version=prompt_version,
    )


def extract_one(
    item: SourceInput,
    *,
    model: str,
    timeout: int,
    max_chars: int,
    call: Callable[[str, str, int], dict[str, Any]] = ollama_call,
) -> tuple[ExtractionResult, int]:
    deterministic = deterministic_extract(
        item.source,
        item.input_text,
        item.raw_fields,
    )
    result, _version, rejected = extract_with_llm(
        source=item.source,
        input_text=item.input_text,
        deterministic=deterministic,
        model=model,
        timeout=timeout,
        max_chars=max_chars,
        call=call,
    )
    return result, rejected


def _save_success(
    session: Session,
    item: SourceInput,
    *,
    model: str,
    result: ExtractionResult,
    rejected: int,
) -> None:
    record = session.get(SourceRecord, item.id)
    if record is None:
        return
    payload = result.model_dump(mode="json")
    payload["_meta"] = {"hallucinations_rejected": rejected}
    existing = session.scalar(
        select(Extraction).where(
            Extraction.source_record_id == item.id,
            Extraction.prompt_version == item.prompt_version,
            Extraction.model_name == model,
        )
    )
    if existing is None:
        session.add(
            Extraction(
                source_record_id=item.id,
                cache_key=item.cache_key,
                prompt_version=item.prompt_version,
                model_name=model,
                result_json=payload,
                status="completed",
                error=None,
                created_at=datetime.now(UTC),
            )
        )
    else:
        existing.cache_key = item.cache_key
        existing.result_json = payload
        existing.status = "completed"
        existing.error = None
        existing.created_at = datetime.now(UTC)
    record.processing_status = "extracted"
    record.extraction_attempts += 1
    record.processing_error = None


def _save_failure(session: Session, item: SourceInput, error: Exception) -> None:
    record = session.get(SourceRecord, item.id)
    if record is None:
        return
    record.processing_status = "failed"
    record.extraction_attempts += 1
    record.processing_error = str(error)[:2000]


def _mark_cached(
    session: Session,
    item: SourceInput,
    *,
    model: str,
    payload: dict[str, Any],
) -> None:
    record = session.get(SourceRecord, item.id)
    if record is None:
        return
    existing = session.scalar(
        select(Extraction).where(
            Extraction.source_record_id == item.id,
            Extraction.prompt_version == item.prompt_version,
            Extraction.model_name == model,
        )
    )
    if existing is None:
        session.add(
            Extraction(
                source_record_id=item.id,
                cache_key=item.cache_key,
                prompt_version=item.prompt_version,
                model_name=model,
                result_json=payload,
                status="completed",
                error=None,
                created_at=datetime.now(UTC),
            )
        )
    record.processing_status = "extracted"
    record.extraction_attempts += 1
    record.processing_error = None


def process_database(
    session_factory: sessionmaker[Session],
    *,
    model: str = "llama3.1",
    workers: int = 2,
    timeout: int = 180,
    max_chars: int = 12_000,
    limit: int | None = None,
    retry_failed: bool = False,
    max_attempts: int = 3,
    call: Callable[[str, str, int], dict[str, Any]] = ollama_call,
) -> dict[str, int]:
    statuses = ["new", "failed"] if retry_failed else ["new"]
    with session_factory() as session:
        statement = (
            select(SourceRecord)
            .where(
                SourceRecord.processing_status.in_(statuses),
                SourceRecord.extraction_attempts < max_attempts,
            )
            .order_by(SourceRecord.id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        items = [source_input(record, model) for record in session.scalars(statement)]
        cache_keys = {item.cache_key for item in items}
        cached_rows = list(
            session.scalars(
                select(Extraction).where(
                    Extraction.cache_key.in_(cache_keys),
                    Extraction.status == "completed",
                )
            )
        ) if cache_keys else []
    cached = {row.cache_key: row.result_json for row in cached_rows}
    stats = {"processed": 0, "cached": 0, "failed": 0, "hallucinations_rejected": 0}
    pending: list[SourceInput] = []

    for item in items:
        payload = cached.get(item.cache_key)
        if payload:
            with session_factory.begin() as session:
                _mark_cached(session, item, model=model, payload=payload)
            stats["cached"] += 1
        else:
            pending.append(item)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                extract_one,
                item,
                model=model,
                timeout=timeout,
                max_chars=max_chars,
                call=call,
            ): item
            for item in pending
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                result, rejected = future.result()
                with session_factory.begin() as session:
                    _save_success(
                        session,
                        item,
                        model=model,
                        result=result,
                        rejected=rejected,
                    )
                stats["processed"] += 1
                stats["hallucinations_rejected"] += rejected
            except Exception as exc:
                with session_factory.begin() as session:
                    _save_failure(session, item, exc)
                stats["failed"] += 1
                log.error("Extraction failed for source_record %s: %s", item.id, exc)
    return stats


def _row_source(row: Any) -> str:
    value = str(row.get("source", "")).casefold()
    return "linkedin_post" if "post" in value else "linkedin_job"


def _pandas():
    """Import pandas only for ad-hoc CSV helpers, not the database pipeline."""

    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError(
            "pandas failed to import (often Windows Application Control "
            "blocking a pyarrow DLL). Database extraction does not need "
            "pandas; CSV clean/eval does. Unblock pyarrow or use "
            "`python -m aambridge clean` against the database."
        ) from exc
    return pd


def clean_csv(
    input_path: Path,
    output_path: Path,
    *,
    model: str,
    timeout: int,
    max_chars: int,
    limit: int | None = None,
    call: Callable[[str, str, int], dict[str, Any]] = ollama_call,
) -> int:
    pd = _pandas()
    frame = pd.read_csv(input_path)
    if limit is not None:
        frame = frame.head(limit)
    rows: list[dict[str, Any]] = []
    for index, row in frame.iterrows():
        raw_fields = {
            key: value for key, value in row.to_dict().items() if pd.notna(value)
        }
        text = " ".join(str(value) for value in raw_fields.values())
        source = _row_source(row)
        version = PROMPT_VERSIONS[source]
        item = SourceInput(
            id=int(index),
            source=source,
            raw_text=text,
            raw_fields=raw_fields,
            input_text=text,
            cache_key=extraction_cache_key(text, version, model),
            prompt_version=version,
        )
        result, _rejected = extract_one(
            item,
            model=model,
            timeout=timeout,
            max_chars=max_chars,
            call=call,
        )
        flattened: dict[str, Any] = {}
        for name in ExtractionResult.model_fields:
            value = getattr(result, name)
            flattened[name] = value.value
            flattened[f"{name}_evidence"] = value.evidence
            flattened[f"{name}_basis"] = value.basis
            flattened[f"{name}_confidence"] = value.confidence
        rows.append({**raw_fields, **flattened})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output_path, index=False, encoding="utf-8-sig")
    return len(rows)


def evaluate_labelled_csv(
    path: Path,
    *,
    model: str,
    timeout: int,
    max_chars: int,
    call: Callable[[str, str, int], dict[str, Any]] = ollama_call,
) -> dict[str, Any]:
    pd = _pandas()
    frame = pd.read_csv(path).fillna("")
    totals: dict[str, int] = {}
    correct: dict[str, int] = {}
    for index, row in frame.iterrows():
        text = str(row["text"])
        source = str(row.get("source") or "linkedin_job")
        version = PROMPT_VERSIONS[source]
        item = SourceInput(
            id=int(index),
            source=source,
            raw_text=text,
            raw_fields={},
            input_text=text,
            cache_key=extraction_cache_key(text, version, model),
            prompt_version=version,
        )
        result, _rejected = extract_one(
            item,
            model=model,
            timeout=timeout,
            max_chars=max_chars,
            call=call,
        )
        for column in frame.columns:
            if not column.startswith("expected_") or row[column] == "":
                continue
            field = column.removeprefix("expected_")
            if field not in ExtractionResult.model_fields:
                continue
            totals[field] = totals.get(field, 0) + 1
            actual = getattr(result, field).value
            if str(actual).casefold() == str(row[column]).casefold():
                correct[field] = correct.get(field, 0) + 1
    per_field = {
        field: correct.get(field, 0) / count for field, count in totals.items()
    }
    overall_total = sum(totals.values())
    return {
        "rows": len(frame),
        "per_field_accuracy": per_field,
        "overall_accuracy": (
            sum(correct.values()) / overall_total if overall_total else 0.0
        ),
    }
