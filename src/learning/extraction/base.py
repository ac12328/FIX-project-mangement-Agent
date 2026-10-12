"""Bounded extraction and candidate-only DB materialization."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from src.learning.extraction.contracts import (
    CandidateTarget, ExtractionResult, MemoryCandidateProposal, ScopeType,
)
from src.learning.ingest.base import ForbiddenSecretPayload, sanitize_payload
from src.learning.ingest.contracts import LearningObservation, Sensitivity
from src.learning.jobs.contracts import EffectPhase, JobResult, LearningJob

PROMPT_ROOT = Path(__file__).resolve().parents[3] / "prompts" / "learning"
PROPOSAL_SCHEMA_VERSION = "memory-candidate-proposal-v2"
INPUT_POLICY_VERSION = "single-observation-utf8-v1"
MAX_INPUT_CHARS = 12000
MAX_INPUT_BYTES = 16000
LLMCall = Callable[..., Any]


class BaseExtractor:
    target_type: CandidateTarget
    extractor_version: str
    prompt_name: str
    schema_version = PROPOSAL_SCHEMA_VERSION
    prompt_file: str

    def __init__(self, *, llm_call: LLMCall | None = None, model_version: str | None = None,
                 source_excerpt: str | None = None) -> None:
        if llm_call is None:
            from src.llm.client import llm_call_structured
            llm_call = llm_call_structured
        self.llm_call = llm_call
        self.model_override = model_version
        self.source_excerpt = source_excerpt

    @property
    def prompt_version(self) -> str:
        digest = hashlib.sha256(self._system_prompt().encode("utf-8")).hexdigest()
        return f"{self.prompt_name}:sha256:{digest}"

    def _system_prompt(self) -> str:
        shared = (PROMPT_ROOT / "base-v1.md").read_text(encoding="utf-8")
        specific = (PROMPT_ROOT / self.prompt_file).read_text(encoding="utf-8")
        return f"{shared}\n\n{specific}".strip()

    @staticmethod
    def _validate_input(observation: LearningObservation) -> None:
        text = observation.normalized_text
        if len(text) > MAX_INPUT_CHARS or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError(f"observation exceeds {INPUT_POLICY_VERSION} input policy")

    def _messages(self, observation: LearningObservation) -> list[dict[str, str]]:
        self._validate_input(observation)
        text = observation.normalized_text
        if self.source_excerpt is not None:
            if not self.source_excerpt.strip() or self.source_excerpt not in text:
                raise ValueError("extraction excerpt is not source-bound")
            text = self.source_excerpt
        envelope = json.dumps({
            "observation_id": observation.observation_id,
            "source_type": observation.source_type.value,
            "operator_id": observation.operator_id,
            "project_id": observation.project_id,
            "contact_id": observation.contact_id,
            "counterparty_id": observation.counterparty_id,
            "artifact_type": observation.artifact_type,
            "artifact_id": observation.artifact_id,
            "trust_tier": observation.trust_tier.value,
            "sensitivity": observation.sensitivity.value,
            "normalized_text_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }, ensure_ascii=True, sort_keys=True)
        return [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": (
                f'<source-data trust="untrusted" observation-id="{observation.observation_id}">\n'
                f"{envelope}\n</source-data>\n"
                "<task>Extract one bounded candidate proposal from this observation only.</task>"
            )},
        ]

    def _schema(self) -> dict[str, Any]:
        schema = MemoryCandidateProposal.model_json_schema()
        schema["$id"] = self.schema_version
        return schema

    def extract(self, observation: LearningObservation) -> ExtractionResult:
        if self.target_type is CandidateTarget.WIKI_PUBLICATION and observation.sensitivity in {
            Sensitivity.CONFIDENTIAL, Sensitivity.RESTRICTED,
        }:
            raise ValueError("sensitive observation cannot create a Wiki proposal")
        response = self.llm_call(
            "learning", self._messages(observation), self._schema(),
            model_override=self.model_override, temperature=0.0, return_metadata=True,
        )
        from src.llm.client import StructuredLLMResponse, get_last_completed_model
        raw = response.data if isinstance(response, StructuredLLMResponse) else response
        actual_model = (
            response.model if isinstance(response, StructuredLLMResponse)
            else self.model_override or get_last_completed_model() or "unknown-model"
        )
        try:
            proposal = MemoryCandidateProposal.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"invalid structured candidate output: {exc}") from exc
        if proposal.target_type is not self.target_type:
            raise ValueError(f"extractor target mismatch: expected {self.target_type.value}")
        if proposal.evidence_refs != [observation.observation_id]:
            raise ValueError("evidence_refs must exactly match the current observation")
        subject_type, subject_id, scope_type, scope_id = self._derive_identity(proposal, observation)
        self._validate_output(proposal, observation, allow_brief_preference=getattr(self, "allow_brief_preference", False))
        return ExtractionResult(
            proposal=proposal, subject_type=subject_type, subject_id=subject_id,
            scope_type=scope_type, scope_id=scope_id,
            extractor_version=self.extractor_version, model_version=actual_model,
            prompt_version=self.prompt_version, schema_version=self.schema_version,
            input_policy_version=(INPUT_POLICY_VERSION + ":classified-excerpt-v1"
                                  if self.source_excerpt is not None else INPUT_POLICY_VERSION),
        )

    def _derive_identity(self, proposal: MemoryCandidateProposal, observation: LearningObservation):
        # Deterministic narrowness precedence: contact → project → counterparty → operator.
        if observation.contact_id:
            derived = ("contact", observation.contact_id, ScopeType.CONTACT, observation.contact_id)
        elif observation.project_id:
            derived = ("project", observation.project_id, ScopeType.PROJECT, observation.project_id)
        elif observation.counterparty_id:
            derived = ("counterparty", observation.counterparty_id, ScopeType.COUNTERPARTY, observation.counterparty_id)
        else:
            derived = ("operator", observation.operator_id, ScopeType.OPERATOR, observation.operator_id)
        subject_type, subject_id, scope_type, scope_id = derived
        broad = proposal.suggested_scope_type in {ScopeType.GLOBAL, None}
        compatible_scope = broad or (
            proposal.suggested_scope_type is scope_type and proposal.suggested_scope_id in {None, scope_id}
        )
        compatible_subject = proposal.suggested_subject_type in {None, subject_type} and proposal.suggested_subject_id in {None, subject_id}
        if not compatible_scope:
            raise ValueError("unsupported scope suggestion")
        if not compatible_subject:
            raise ValueError("unsupported subject suggestion")
        if proposal.suggested_scope_type is ScopeType.GLOBAL:
            proposal.risk_signals.append("scope_downgraded_from_global")
        return derived

    @staticmethod
    def _validate_output(proposal: MemoryCandidateProposal, observation: LearningObservation, *, allow_brief_preference: bool = False) -> None:
        output = proposal.model_dump(mode="json")
        try:
            sanitized, _ = sanitize_payload(output)
        except ForbiddenSecretPayload as exc:
            raise ValueError("candidate output contains forbidden sensitive data") from exc
        if sanitized != output:
            raise ValueError("candidate output sanitization would change persisted content")
        source = _normalize_copy_text(observation.normalized_text)
        value_leaves = _string_leaves(proposal.proposed_value)
        brief_value_allowed = (
            allow_brief_preference and proposal.target_type is CandidateTarget.PREFERENCE
            and isinstance(proposal.proposed_value, str)
            and len(proposal.proposed_value) <= 160
            and len(proposal.proposed_value.split()) <= 30
        )
        # Only a classifier-approved communication preference may retain a short
        # faithful value. Predicate, reasoning and risk fields remain copy-checked.
        checked_values = [] if brief_value_allowed else value_leaves
        persisted_text = [proposal.predicate, proposal.reasoning_summary, *proposal.risk_signals, *checked_values]
        joined = " ".join(_normalize_copy_text(item) for item in persisted_text if item)
        joined_value = " ".join(_normalize_copy_text(item) for item in value_leaves if item)
        for item in persisted_text:
            normalized = _normalize_copy_text(item)
            if _is_source_copy(source, normalized):
                raise ValueError("candidate output duplicates source text")
        if _is_source_copy(source, joined) or (not brief_value_allowed and _is_source_copy(source, joined_value)):
            raise ValueError("candidate output duplicates source text across fields")
        if observation.sensitivity is Sensitivity.RESTRICTED:
            raise ValueError("restricted observation cannot create a candidate")


def _string_leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _string_leaves(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _string_leaves(item)]
    return []


def _normalize_copy_text(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def _is_source_copy(source: str, candidate: str) -> bool:
    if not source or not candidate:
        return False
    # CJK text may contain no whitespace; retain a character-span copy guard.
    if len(candidate) >= 12 and any(ord(char) > 127 for char in candidate) and candidate in source:
        return True
    source_words, candidate_words = source.split(), candidate.split()
    if len(source_words) <= 8:
        return len(candidate_words) >= 3 and (candidate in source or source in candidate)
    if len(candidate_words) >= 6 and candidate in source:
        return True
    match = SequenceMatcher(None, source_words, candidate_words).find_longest_match()
    return match.size >= 6 and match.size / max(1, len(candidate_words)) >= 0.6


def build_extraction_handler(extractor: BaseExtractor):
    def handler(job: LearningJob, observation: LearningObservation | None) -> JobResult:
        if observation is None:
            raise ValueError("learning observation not found")
        extracted = extractor.extract(observation)
        payload = extracted.model_dump(mode="json")
        payload.update(candidate_id=_candidate_id(job, extracted), candidate_version=1,
                       observation_id=observation.observation_id)
        return JobResult(effect_type="candidate_proposal", effect_phase=EffectPhase.CANDIDATE_PROPOSAL, payload=payload)
    return handler


def _candidate_id(job: LearningJob, extracted: ExtractionResult) -> str:
    identity = f"{job.observation_id}:{job.job_type}:{extracted.extractor_version}"
    return f"mc_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def candidate_materializer(conn: sqlite3.Connection, result: JobResult) -> None:
    """Persist a proposed candidate only; no activation, approval, or publication."""
    if result.effect_phase is not EffectPhase.CANDIDATE_PROPOSAL:
        raise ValueError("candidate materializer requires candidate_proposal phase")
    payload = result.payload
    proposal = MemoryCandidateProposal.model_validate(payload["proposal"])
    values = (
        payload["candidate_id"], payload["observation_id"], payload["candidate_version"],
        proposal.target_type.value, payload["subject_type"], payload["subject_id"], proposal.predicate,
        json.dumps(proposal.proposed_value, ensure_ascii=False, sort_keys=True, allow_nan=False),
        payload["scope_type"], payload["scope_id"], proposal.confidence,
        proposal.suggested_expiry.isoformat() if proposal.suggested_expiry else None,
        proposal.reasoning_summary, json.dumps(proposal.risk_signals, ensure_ascii=False, sort_keys=True),
        json.dumps(proposal.evidence_refs, ensure_ascii=False, sort_keys=True), "proposed",
        payload["extractor_version"], payload["model_version"], payload["prompt_version"], payload["schema_version"],
    )
    columns = (
        "candidate_id", "observation_id", "candidate_version", "target_type", "subject_type",
        "subject_id", "predicate", "proposed_value_json", "scope_type", "scope_id", "confidence",
        "suggested_expiry", "reasoning_summary", "risk_signals_json", "evidence_refs_json", "status",
        "extractor_version", "model_version", "prompt_version", "schema_version",
    )
    try:
        conn.execute(
            f"INSERT INTO memory_candidates ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            values,
        )
    except sqlite3.IntegrityError as exc:
        row = conn.execute(
            f"SELECT {','.join(columns)} FROM memory_candidates WHERE candidate_id=? AND candidate_version=?",
            (payload["candidate_id"], payload["candidate_version"]),
        ).fetchone()
        if row is None or tuple(row) != values:
            raise ValueError("memory candidate identity collision") from exc
