"""Seal the deterministic 420-turn Policy-v4 controlled holdout question set."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .artifacts import read_model, write_model
from .grounding_repair_v4_protocol import (
    EncryptedBlob,
    EncryptedQuestionEntry,
    EncryptedQuestionSet,
    HistoricalExclusionManifest,
    Preregistration,
    QuestionIndexItem,
    QuestionManifest,
    normalized_request_hash,
    sha256_text,
    stamp_v4,
    verify_v4,
)
from src.observability.payload_crypto import decrypt_payload, encrypt_payload
from .grounding_repair_v4_question_variants import repair_recovery_questions

_PROJECT_TEMPLATES = (
    "For project {project_id}, summarize the recorded status, milestone, ball ownership, latest activity, and next action. Cite every factual statement.",
    "For project {project_id}, list the open work, blockers, unanswered items, and recorded owner. Use only Workspace records and cite each fact.",
    "What does the Workspace record for project {project_id} about progress, pending actions, and recent communications? Cite every factual statement.",
    "Build a concise evidence-backed briefing for project {project_id}, covering milestone, status, risks, next action, and relevant dates.",
    "For project {project_id}, identify recorded follow-ups and explain which side currently owns each action, with source citations.",
    "Summarize the recorded timeline for project {project_id} and flag gaps or contradictions without filling them by inference.",
    "What unresolved work is recorded for project {project_id}? Include the evidence date and cite every factual claim.",
    "Prepare a read-only project health summary for {project_id} using only recorded Workspace evidence and exact citations.",
    "For project {project_id}, compare the recorded milestone, next action, latest message, and follow-up state; label any inference.",
    "Which recorded facts for project {project_id} require operator attention, and what evidence supports each one?",
    "For project {project_id}, report the latest recorded activity and any unanswered external thread, without asserting unrecorded completion.",
    "Give an evidence-only handover note for project {project_id}, including status, owner, pending work, and source dates.",
)

_PORTFOLIO_TEMPLATES = (
    "Across the portfolio, list projects with recorded blockers or overdue follow-ups and cite the evidence for each item.",
    "Which projects require attention based on recorded Workspace evidence? Show project, reason, owner side, and source citation.",
    "Summarize unresolved portfolio work, grouped by owner side, using only cited Workspace records.",
    "List projects with unanswered external threads and show the recorded date and supporting source for each.",
    "Provide an evidence-backed portfolio briefing of blockers, pending actions, and stale activity. Cite each factual statement.",
    "Which recorded project actions are waiting on internal teams versus external parties? Cite every classification.",
    "Show the portfolio items with the most recent recorded follow-up need, but do not infer facts absent from sources.",
    "Identify projects whose recorded milestone, status, and next action may need operator review; cite the relevant records.",
    "Build a read-only portfolio risk digest from Workspace records, preserving source dates and explicit uncertainty.",
    "What open project work is recorded across the Workspace, and which evidence supports each listed item?",
    "Summarize recorded project activity gaps and unresolved actions across the portfolio with exact citations.",
    "Create an evidence-only portfolio handover: project, recorded state, action owner, next action, and source date.",
)

_EVIDENCE_LENSES = (
    "Emphasize blockers and ownership.",
    "Emphasize unanswered communications.",
    "Emphasize recorded follow-up dates.",
    "Emphasize milestone evidence.",
    "Emphasize next-action evidence.",
    "Emphasize status contradictions.",
    "Emphasize stale activity signals.",
    "Emphasize external dependencies.",
    "Emphasize internal dependencies.",
    "Emphasize explicit uncertainty.",
    "Emphasize source recency boundaries.",
    "Emphasize missing requested fields.",
    "Emphasize owner-side classification.",
    "Emphasize actionability without invention.",
    "Emphasize evidence dates and provenance.",
    "Emphasize unresolved work only.",
    "Emphasize read-only operator review needs.",
    "Emphasize cross-record consistency.",
    "Emphasize recorded communication gaps.",
    "Emphasize concise evidence coverage.",
)

_MODIFIERS = (
    "Keep scope boundaries explicit.",
    "Prefer a compact table when it improves clarity.",
    "Separate recorded facts from recommendations.",
    "State when the Workspace lacks evidence for a requested field.",
    "Do not treat silence as completion.",
    "Preserve exact project identifiers and dates from cited records.",
    "Use concise bullets and label uncertainty.",
    "Include only claims that can be tied to a source ID.",
    "Call out contradictory records instead of resolving them by guesswork.",
    "Avoid uncited ranking or completion claims.",
)

_REPAIR_ENRICHMENT_TEMPLATE_INDEXES = (5, 11, 3, 0)
_REPAIR_ENRICHMENT_FORMATS = (
    "Use separate sections for recorded facts, evidence gaps, and operator recommendations.",
    "Return a compact table and then a clearly labelled recommendation section.",
    "List factual findings first, then provide follow-up suggestions separately.",
    "For every row, distinguish the recorded state from the proposed next action.",
    "Organize the answer as evidence-backed findings, uncertainties, and recommended follow-up.",
    "Add a short executive summary before the detailed evidence table, with recommendations separate.",
    "End with a concise operator-attention list after the cited evidence.",
    "Use headings for recorded facts, missing evidence, and recommended action.",
    "Give a one-line takeaway, then detailed cited findings and separate recommendations.",
    "Present a handover-ready summary followed by evidence details and recommendation notes.",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def database_fingerprint(path: Path) -> str:
    """Return a stable hash of the project snapshot used to derive questions."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    digest = hashlib.sha256()
    with _connect_readonly(resolved) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone()
        if row is None:
            raise ValueError("database clone does not contain projects")
        digest.update(str(row[0] or "").encode("utf-8"))
        columns = [item[1] for item in conn.execute("PRAGMA table_info(projects)")]
        order = ", ".join(f'"{item}"' for item in columns)
        for record in conn.execute(f'SELECT {order} FROM projects ORDER BY {order}'):
            digest.update(json.dumps(list(record), ensure_ascii=False, default=str).encode("utf-8"))
            digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _walk_hash_fields(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"request_hash", "question_hash"} and isinstance(child, str):
                if child.startswith("sha256:") and len(child) == 71:
                    yield child
            yield from _walk_hash_fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_hash_fields(child)


def _artifact_tree_hash(root: Path) -> str:
    resolved = root.resolve()
    if resolved.is_file():
        return sha256_text(resolved.name + ":" + _file_sha256(resolved))
    entries = []
    if resolved.is_dir():
        for path in sorted(resolved.rglob("*.json")):
            entries.append(f"{path.relative_to(resolved).as_posix()}:{_file_sha256(path)}")
    return sha256_text("\n".join(entries))


def _artifact_request_hashes(roots: Iterable[Path]) -> tuple[set[str], list[str]]:
    hashes: set[str] = set()
    root_hashes: list[str] = []
    for root in sorted({Path(item).resolve() for item in roots}, key=str):
        if not root.exists():
            continue
        root_hashes.append(_artifact_tree_hash(root))
        paths = [root] if root.is_file() else sorted(root.rglob("*.json"))
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            hashes.update(_walk_hash_fields(value))
    return hashes, root_hashes


def collect_historical_exclusions(
    *, database: Path, artifact_roots: Iterable[Path], generated_at: datetime | None = None
) -> HistoricalExclusionManifest:
    """Collect prior request hashes without persisting request plaintext."""
    historical, root_hashes = _artifact_request_hashes(artifact_roots)
    unreadable = 0
    with _connect_readonly(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(trace_events)")}
        if columns:
            rows = conn.execute(
                "SELECT metadata_json, payload_ciphertext, payload_nonce, payload_key_version "
                "FROM trace_events WHERE event_type='run.start'"
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                historical.update(_walk_hash_fields(metadata))
                if not row["payload_ciphertext"]:
                    continue
                try:
                    payload = decrypt_payload(
                        row["payload_ciphertext"], row["payload_nonce"], row["payload_key_version"]
                    )
                except Exception:
                    unreadable += 1
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("user_input"), str):
                    historical.add(normalized_request_hash(payload["user_input"]))
                historical.update(_walk_hash_fields(payload))
    if unreadable:
        raise ValueError(
            f"historical exclusion scan could not decrypt {unreadable} encrypted run.start events"
        )
    return stamp_v4(HistoricalExclusionManifest(
        generated_at=generated_at or _utcnow(),
        database_fingerprint=database_fingerprint(database),
        artifact_root_hashes=sorted(root_hashes),
        request_hashes=sorted(historical),
        unreadable_encrypted_run_start_count=0,
    ))


def _project_ids(database: Path) -> list[str]:
    with _connect_readonly(database) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "projects" not in tables:
            raise ValueError("database clone does not contain projects")
        return [
            str(row[0]) for row in conn.execute(
                "SELECT project_id FROM projects ORDER BY project_id"
            ).fetchall() if str(row[0]).strip()
        ]


def _repair_enriched_questions() -> Iterator[dict[str, Any]]:
    """Yield natural portfolio requests enriched for grounding-repair evaluation."""
    for template_index in _REPAIR_ENRICHMENT_TEMPLATE_INDEXES:
        template = _PORTFOLIO_TEMPLATES[template_index]
        for lens in _EVIDENCE_LENSES:
            for output_format in _REPAIR_ENRICHMENT_FORMATS:
                yield {
                    "question": f"{template} {lens} {_MODIFIERS[2]} {output_format}",
                    "scope_mode": "portfolio",
                    "project_ids": [],
                    "web_enabled": False,
                    "intent_type": "aggregate_query",
                }


def _candidate_questions(database: Path) -> Iterator[dict[str, Any]]:
    projects = _project_ids(database)
    if not projects:
        raise ValueError("database clone contains no projects")
    yield from _repair_enriched_questions()
    yield from repair_recovery_questions(
        portfolio_templates=_PORTFOLIO_TEMPLATES,
        evidence_lenses=_EVIDENCE_LENSES,
        modifiers=_MODIFIERS,
    )
    project_rows: list[dict[str, Any]] = []
    for template_index, template in enumerate(_PROJECT_TEMPLATES):
        for project_index, project_id in enumerate(projects):
            modifier = _MODIFIERS[(template_index + project_index) % len(_MODIFIERS)]
            project_rows.append({
                "question": f"{template.format(project_id=project_id)} {modifier}",
                "scope_mode": "projects",
                "project_ids": [project_id],
                "web_enabled": False,
                "intent_type": "aggregate_query",
            })
    portfolio_rows: list[dict[str, Any]] = []
    for cycle in range(20):
        for template_index, template in enumerate(_PORTFOLIO_TEMPLATES):
            first = _MODIFIERS[(cycle + template_index) % len(_MODIFIERS)]
            second = _MODIFIERS[(cycle * 3 + template_index + 1) % len(_MODIFIERS)]
            portfolio_rows.append({
                "question": f"{template} {_EVIDENCE_LENSES[cycle]} {first} {second}",
                "scope_mode": "portfolio",
                "project_ids": [],
                "web_enabled": False,
                "intent_type": "aggregate_query",
            })
    maximum = max(len(project_rows), len(portfolio_rows))
    for index in range(maximum):
        if index < len(project_rows):
            yield project_rows[index]
        if index < len(portfolio_rows) and index % 2 == 0:
            yield portfolio_rows[index]
    for index, row in enumerate(portfolio_rows):
        if index % 2:
            yield row
    # Reserve deterministic zero-overlap variants for a fresh experiment when an
    # earlier sealed run is invalidated by infrastructure or yields an insufficient sample.
    for cycle in range(1, 5):
        for template_index, template in enumerate(_PROJECT_TEMPLATES):
            for project_index, project_id in enumerate(projects):
                lens = _EVIDENCE_LENSES[
                    (cycle * len(_PROJECT_TEMPLATES) + template_index + project_index)
                    % len(_EVIDENCE_LENSES)
                ]
                modifier = _MODIFIERS[
                    (cycle * 5 + template_index * 2 + project_index) % len(_MODIFIERS)
                ]
                yield {
                    "question": f"{template.format(project_id=project_id)} {lens} {modifier}",
                    "scope_mode": "projects",
                    "project_ids": [project_id],
                    "web_enabled": False,
                    "intent_type": "aggregate_query",
                }


def build_question_payloads(
    *, database: Path, historical: HistoricalExclusionManifest
) -> list[dict[str, Any]]:
    if not verify_v4(historical):
        raise ValueError("historical exclusion manifest hash is invalid")
    excluded = set(historical.request_hashes)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in _candidate_questions(database):
        request_hash = normalized_request_hash(payload["question"])
        if request_hash in excluded or request_hash in seen:
            continue
        seen.add(request_hash)
        selected.append({**payload, "question_hash": request_hash, "request_hash": request_hash})
        if len(selected) == 420:
            break
    if len(selected) != 420:
        raise ValueError(
            f"insufficient unique zero-overlap questions: generated={len(selected)}, required=420"
        )
    return selected


def seal_question_set(
    *,
    database: Path,
    artifact_roots: Iterable[Path],
    question_set_id: str,
    manifest_path: Path,
    encrypted_payload_path: Path,
    generated_at: datetime | None = None,
) -> tuple[QuestionManifest, EncryptedQuestionSet, HistoricalExclusionManifest]:
    """Seal exactly 420 turns; prior operator authorization replaces per-turn review."""
    manifest_path = Path(manifest_path).resolve()
    encrypted_payload_path = Path(encrypted_payload_path).resolve()
    if manifest_path.exists() or encrypted_payload_path.exists():
        if not (manifest_path.exists() and encrypted_payload_path.exists()):
            raise FileExistsError("sealed question artifacts are incomplete")
        manifest = read_model(manifest_path, QuestionManifest)
        encrypted = read_model(encrypted_payload_path, EncryptedQuestionSet)
        if not verify_v4(manifest) or not verify_v4(encrypted):
            raise ValueError("existing sealed question artifact hash is invalid")
        if manifest.question_set_id != question_set_id or encrypted.question_set_id != question_set_id:
            raise FileExistsError("existing sealed question set has a different ID")
        historical = collect_historical_exclusions(
            database=database, artifact_roots=artifact_roots, generated_at=generated_at
        )
        return manifest, encrypted, historical

    timestamp = generated_at or _utcnow()
    historical = collect_historical_exclusions(
        database=database, artifact_roots=artifact_roots, generated_at=timestamp
    )
    payloads = build_question_payloads(database=database, historical=historical)
    encrypted_entries: list[EncryptedQuestionEntry] = []
    indexes: list[QuestionIndexItem] = []
    for order, payload in enumerate(payloads, start=1):
        question_hash = payload["question_hash"]
        question_id = "v4q_" + question_hash.removeprefix("sha256:")[:16]
        encrypted_data = encrypt_payload({
            "question": payload["question"],
            "scope_mode": payload["scope_mode"],
            "project_ids": payload["project_ids"],
            "web_enabled": False,
            "intent_type": payload["intent_type"],
            "question_hash": question_hash,
            "request_hash": payload["request_hash"],
        })
        blob = EncryptedBlob.model_validate(encrypted_data)
        encrypted_entries.append(EncryptedQuestionEntry(
            order=order, question_id=question_id, payload=blob
        ))
        indexes.append(QuestionIndexItem(
            order=order,
            question_id=question_id,
            question_hash=question_hash,
            request_hash=payload["request_hash"],
            encrypted_payload_hash=blob.payload_hash,
        ))
    encrypted = stamp_v4(EncryptedQuestionSet(
        question_set_id=question_set_id, generated_at=timestamp, entries=encrypted_entries
    ))
    manifest = stamp_v4(QuestionManifest(
        question_set_id=question_set_id,
        generated_at=timestamp,
        database_fingerprint=database_fingerprint(database),
        historical_exclusion_hash=historical.content_hash,
        encrypted_payload_artifact_hash=encrypted.content_hash,
        questions=indexes,
    ))
    write_model(encrypted_payload_path, encrypted, immutable=True)
    write_model(manifest_path, manifest, immutable=True)
    return manifest, encrypted, historical


def seal_preregistration(
    *,
    experiment_id: str,
    question_manifest: QuestionManifest,
    historical: HistoricalExclusionManifest,
    implementation_paths: Iterable[Path],
    rubric_path: Path,
    reference_prompt_hashes: list[str],
    judge_prompt_hash: str,
    model_identity: dict[str, Any],
    output_path: Path,
    sealed_at: datetime | None = None,
) -> Preregistration:
    if not verify_v4(question_manifest) or not verify_v4(historical):
        raise ValueError("cannot preregister against an invalid sealed artifact")
    implementation_lines = [
        f"{Path(path).name}:{_file_sha256(Path(path))}"
        for path in sorted({Path(item).resolve() for item in implementation_paths}, key=str)
    ]
    preregistration = stamp_v4(Preregistration(
        experiment_id=experiment_id,
        sealed_at=sealed_at or _utcnow(),
        implementation_hash=sha256_text("\n".join(implementation_lines)),
        rubric_hash=_file_sha256(Path(rubric_path).resolve()),
        reference_prompt_hashes=reference_prompt_hashes,
        judge_prompt_hash=judge_prompt_hash,
        model_identity_hash=sha256_text(json.dumps(
            model_identity, sort_keys=True, separators=(",", ":"), default=str
        )),
        question_manifest_hash=question_manifest.content_hash,
        historical_exclusion_hash=historical.content_hash,
    ))
    write_model(Path(output_path), preregistration, immutable=True)
    return preregistration


__all__ = [
    "build_question_payloads", "collect_historical_exclusions", "database_fingerprint",
    "seal_preregistration", "seal_question_set",
]
