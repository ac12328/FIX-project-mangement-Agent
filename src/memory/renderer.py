"""Structured deterministic renderer with hard memory budgets."""
from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from src.memory.exposure_repo import ExposureCandidate
from src.memory.ranking import estimate_tokens
from src.memory.retriever import MemoryRetrievalResult, RetrievedMemoryFact


class MemoryBudget(BaseModel):
    model_config = ConfigDict(frozen=True)
    total_tokens: int = Field(gt=0)
    section_tokens: dict[str, int] = Field(default_factory=dict)


class RenderedMemory(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    estimated_tokens: int
    rendered_fact_ids: list[str] = Field(default_factory=list)
    exposure_candidates: list[ExposureCandidate] = Field(default_factory=list)
    truncated: bool = False


def budget_for_profile(profile: str) -> MemoryBudget:
    budgets = {
        "bootstrap": MemoryBudget(total_tokens=300, section_tokens={"warnings": 80, "facts": 220}),
        "minimal": MemoryBudget(total_tokens=700, section_tokens={"warnings": 140, "facts": 560}),
        "full": MemoryBudget(total_tokens=1400, section_tokens={"warnings": 250, "facts": 1150}),
    }
    return budgets.get(profile, budgets["full"])


def _fact_line(fact: RetrievedMemoryFact) -> str:
    value = json.dumps(fact.value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    scope = f"{fact.scope_type}:{fact.scope_id or '*'}"
    return (
        f"- [id={fact.fact_id} version={fact.version} scope={scope} source={fact.source_type} "
        f"trust={fact.trust_tier} confidence={fact.confidence:.3f} evidence={fact.evidence_count}] "
        f"{fact.subject_type}:{fact.subject_id or '*'} {fact.predicate}={value}"
    )


def render_memory(result: MemoryRetrievalResult, budget: MemoryBudget) -> RenderedMemory:
    """Pack warnings then facts, checking the final rendered budget per line."""
    lines = ["<governed-memory>"]
    used_ids: list[str] = []
    candidates: list[ExposureCandidate] = []
    section_used = {"warnings": 0, "facts": 0}
    truncated = False

    def append(line: str, section: str) -> bool:
        candidate = "\n".join(lines + [line, "</governed-memory>"])
        line_tokens = estimate_tokens(line)
        section_limit = budget.section_tokens.get(section, budget.total_tokens)
        if section_used[section] + line_tokens > section_limit or estimate_tokens(candidate) > budget.total_tokens:
            return False
        lines.append(line)
        section_used[section] += line_tokens
        return True

    for warning in result.warnings:
        line = f"- WARNING conflict={warning.conflict_id} risk={warning.risk_level}: {warning.message}"
        if not append(line, "warnings"):
            truncated = True
            break
    for fact in result.facts:
        fact_line = _fact_line(fact)
        if append(fact_line, "facts"):
            used_ids.append(fact.fact_id)
            candidates.append(ExposureCandidate(
                fact_id=fact.fact_id, fact_version=fact.version,
                rendered_hash=hashlib.sha256(fact_line.encode("utf-8")).hexdigest(),
                rendered_text=fact_line, rank=len(candidates) + 1,
                token_count=estimate_tokens(fact_line),
            ))
        else:
            truncated = True
    lines.append("</governed-memory>")
    text = "\n".join(lines) if used_ids or result.warnings else ""
    return RenderedMemory(text=text, estimated_tokens=estimate_tokens(text) if text else 0,
                          rendered_fact_ids=used_ids, exposure_candidates=candidates,
                          truncated=truncated)
