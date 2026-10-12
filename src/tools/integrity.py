"""Deterministic startup integrity checks for the unified agent tool plane."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from src.tools.integrity_contracts import audit_contract_definition
from src.tools.contracts import ToolContract


@dataclass(frozen=True, order=True)
class ToolIntegrityIssue:
    """One deterministic, actionable tool-plane integrity failure."""

    code: str
    subject: str
    detail: str
    action: str

    def format(self) -> str:
        return f"[{self.code}] {self.subject}: {self.detail} Action: {self.action}"


@dataclass(frozen=True)
class ToolIntegrityReport:
    """Summary returned by the non-raising integrity audit."""

    contract_count: int
    indexed_contract_count: int
    handler_count: int
    visible_contract_count: int
    exposure_count: int
    issues: tuple[ToolIntegrityIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    def format_error(self) -> str:
        if self.ok:
            return (
                "Tool integrity validation passed "
                f"({self.contract_count} contracts, {self.handler_count} handlers)."
            )
        lines = [
            f"Tool integrity validation failed with {len(self.issues)} issue(s):"
        ]
        lines.extend(f"- {issue.format()}" for issue in self.issues)
        return "\n".join(lines)


class ToolIntegrityError(RuntimeError):
    """Raised when the runtime tool plane is internally inconsistent."""

    def __init__(self, report: ToolIntegrityReport):
        self.report = report
        super().__init__(report.format_error())


def _issue(
    issues: list[ToolIntegrityIssue],
    code: str,
    subject: str,
    detail: str,
    action: str,
) -> None:
    issues.append(ToolIntegrityIssue(code, subject, detail, action))


def _default_runtime_inputs() -> tuple[
    Sequence[ToolContract],
    Mapping[str, ToolContract],
    Mapping[str, Any],
    Mapping[str, Iterable[str]],
    Sequence[tuple[str, Mapping[str, Any]]],
]:
    """Load runtime registries lazily so importing this module has no side effects."""
    from src.agent.skills.registry import (
        CURATOR_ONLY_TOOLS,
        SKILLS,
        SURFACE_ONLY_TOOLS,
        get_host_tool_names,
        get_specialist_tool_names,
    )
    from src.agent.specialists.contracts import SpecialistType
    from src.tools.executor import TOOL_HANDLERS, TOOL_HANDLER_SOURCES
    from src.tools.registry import ALL_TOOLS, TOOL_BY_NAME

    exposures: dict[str, Iterable[str]] = {
        "host": get_host_tool_names(),
        "curator_only": CURATOR_ONLY_TOOLS,
        "surface_only": SURFACE_ONLY_TOOLS,
    }
    for skill in SKILLS:
        exposures[f"skill:{skill.skill_id}"] = skill.tools
    for specialist_type in SpecialistType:
        exposures[f"specialist:{specialist_type.value}"] = (
            get_specialist_tool_names(specialist_type) - {"delegate"}
        )

    return (
        ALL_TOOLS,
        TOOL_BY_NAME,
        TOOL_HANDLERS,
        exposures,
        TOOL_HANDLER_SOURCES,
    )


def audit_tool_integrity(
    *,
    contracts: Sequence[ToolContract] | None = None,
    contract_index: Mapping[str, ToolContract] | None = None,
    handlers: Mapping[str, Any] | None = None,
    exposures: Mapping[str, Iterable[str]] | None = None,
    handler_sources: Sequence[tuple[str, Mapping[str, Any]]] | None = None,
) -> ToolIntegrityReport:
    """Audit registry, exposure, dispatch, schema, and metadata consistency.

    All inputs are injectable for focused contract tests. With no arguments the
    function audits the production registries without database or network work.
    """
    if any(
        value is None
        for value in (contracts, contract_index, handlers, exposures, handler_sources)
    ):
        defaults = _default_runtime_inputs()
        contracts = defaults[0] if contracts is None else contracts
        contract_index = defaults[1] if contract_index is None else contract_index
        handlers = defaults[2] if handlers is None else handlers
        exposures = defaults[3] if exposures is None else exposures
        handler_sources = defaults[4] if handler_sources is None else handler_sources

    contract_list = tuple(contracts)
    contract_index = contract_index or {}
    handlers = handlers or {}
    exposures = exposures or {}
    handler_sources = handler_sources or ()
    issues: list[ToolIntegrityIssue] = []

    valid_contracts: list[tuple[str, ToolContract]] = []
    for position, contract in enumerate(contract_list):
        if not isinstance(contract, ToolContract):
            _issue(
                issues,
                "invalid_contract",
                f"contract:{position}",
                f"expected ToolContract, got {type(contract).__name__}",
                "Register a ToolContract instance in ALL_TOOLS.",
            )
            continue
        if not isinstance(contract.name, str) or not contract.name.strip():
            _issue(
                issues,
                "invalid_tool_name",
                f"contract:{position}",
                "tool name must be a non-empty string",
                "Set a stable, non-empty tool contract name.",
            )
            continue
        valid_contracts.append((contract.name, contract))

    contract_names = [name for name, _contract in valid_contracts]
    name_counts = Counter(contract_names)
    for name in sorted(name for name, count in name_counts.items() if count > 1):
        _issue(
            issues,
            "duplicate_contract",
            f"tool:{name}",
            f"contract name appears {name_counts[name]} times in ALL_TOOLS",
            "Keep exactly one ToolContract for this name.",
        )

    expected_index: dict[str, ToolContract] = {}
    for name, contract in valid_contracts:
        expected_index[name] = contract
    contract_name_set = set(expected_index)

    index_keys = {key for key in contract_index if isinstance(key, str)}
    for key in contract_index:
        if not isinstance(key, str) or not key:
            _issue(
                issues,
                "invalid_contract_index_key",
                "TOOL_BY_NAME",
                f"index contains invalid key {key!r}",
                "Use non-empty string tool names as index keys.",
            )
    for name in sorted(contract_name_set - index_keys):
        _issue(
            issues,
            "contract_index_missing",
            f"tool:{name}",
            "ALL_TOOLS contract is missing from TOOL_BY_NAME",
            "Rebuild TOOL_BY_NAME from the complete contract list.",
        )
    for name in sorted(index_keys - contract_name_set):
        _issue(
            issues,
            "contract_index_orphan",
            f"tool:{name}",
            "TOOL_BY_NAME contains no matching ALL_TOOLS contract",
            "Remove the stale index entry or register its contract.",
        )
    for name in sorted(contract_name_set & index_keys):
        if contract_index[name] is not expected_index[name]:
            _issue(
                issues,
                "contract_index_mismatch",
                f"tool:{name}",
                "TOOL_BY_NAME does not reference the registered contract object",
                "Rebuild TOOL_BY_NAME after assembling ALL_TOOLS.",
            )

    handler_names: set[str] = set()
    for name, handler in handlers.items():
        if not isinstance(name, str) or not name:
            _issue(
                issues,
                "invalid_handler_name",
                "TOOL_HANDLERS",
                f"handler registry contains invalid key {name!r}",
                "Use the exact non-empty ToolContract name.",
            )
            continue
        handler_names.add(name)
        if not callable(handler):
            _issue(
                issues,
                "non_callable_handler",
                f"tool:{name}",
                f"registered handler is {type(handler).__name__}, not callable",
                "Register a callable handler.",
            )

    for name in sorted(handler_names - contract_name_set):
        _issue(
            issues,
            "orphan_handler",
            f"tool:{name}",
            "handler has no ToolContract",
            "Add the matching contract or remove the handler registration.",
        )

    visible_names = {
        name for name, contract in valid_contracts if contract.llm_visible
    }
    for name in sorted(visible_names - handler_names):
        _issue(
            issues,
            "visible_tool_missing_handler",
            f"tool:{name}",
            "LLM-visible contract is not executable",
            "Register a callable handler or set llm_visible to False.",
        )

    exposed_names: set[str] = set()
    for exposure_name in sorted(exposures, key=str):
        raw_names = exposures[exposure_name]
        names: set[str] = set()
        for name in raw_names:
            if not isinstance(name, str) or not name:
                _issue(
                    issues,
                    "invalid_exposure_name",
                    f"exposure:{exposure_name}",
                    f"contains invalid tool name {name!r}",
                    "Use registered non-empty tool names in exposure bundles.",
                )
                continue
            names.add(name)
        exposed_names.update(names)
        for name in sorted(names - contract_name_set):
            _issue(
                issues,
                "exposure_missing_contract",
                f"exposure:{exposure_name}",
                f"references unknown tool {name}",
                "Register the contract or remove the tool from this exposure.",
            )
        for name in sorted(names & contract_name_set):
            contract = expected_index[name]
            if not contract.llm_visible:
                _issue(
                    issues,
                    "exposure_invisible_contract",
                    f"exposure:{exposure_name}",
                    f"references LLM-invisible tool {name}",
                    "Remove it from the model exposure or make the contract visible.",
                )
            if name not in handler_names:
                _issue(
                    issues,
                    "exposure_missing_handler",
                    f"exposure:{exposure_name}",
                    f"references non-executable tool {name}",
                    "Register a callable handler or remove the exposure.",
                )
            elif not callable(handlers[name]):
                _issue(
                    issues,
                    "exposure_non_callable_handler",
                    f"exposure:{exposure_name}",
                    f"tool {name} does not resolve to a callable handler",
                    "Register a callable handler before exposing the tool.",
                )

    for name in sorted(visible_names - exposed_names):
        _issue(
            issues,
            "visible_tool_unexposed",
            f"tool:{name}",
            "LLM-visible contract is not classified in any runtime exposure",
            "Add it to host, skill, curator-only, or surface-only exposure.",
        )

    source_names_by_tool: dict[str, list[str]] = defaultdict(list)
    for source_name, source_handlers in handler_sources:
        for name, handler in source_handlers.items():
            if isinstance(name, str) and name:
                source_names_by_tool[name].append(str(source_name))
            if not callable(handler):
                _issue(
                    issues,
                    "source_non_callable_handler",
                    f"handler_source:{source_name}",
                    f"tool {name!r} is not callable",
                    "Register a callable handler in this source map.",
                )
    for name in sorted(source_names_by_tool):
        sources = sorted(source_names_by_tool[name])
        if len(sources) > 1:
            _issue(
                issues,
                "duplicate_handler_source",
                f"tool:{name}",
                "handler is registered by multiple sources: " + ", ".join(sources),
                "Keep one owning handler source to prevent silent shadowing.",
            )
        if name not in handler_names:
            _issue(
                issues,
                "handler_source_missing_from_dispatch",
                f"tool:{name}",
                "source handler is missing from TOOL_HANDLERS",
                "Rebuild the flat dispatcher from every declared handler source.",
            )

    add_issue = partial(_issue, issues)
    for name, contract in valid_contracts:
        audit_contract_definition(contract, name, add_issue)

    sorted_issues = tuple(sorted(set(issues)))
    return ToolIntegrityReport(
        contract_count=len(contract_list),
        indexed_contract_count=len(contract_index),
        handler_count=len(handlers),
        visible_contract_count=len(visible_names),
        exposure_count=len(exposures),
        issues=sorted_issues,
    )


def validate_tool_integrity(**kwargs: Any) -> ToolIntegrityReport:
    """Raise an actionable startup error when the tool plane is inconsistent."""
    report = audit_tool_integrity(**kwargs)
    if not report.ok:
        raise ToolIntegrityError(report)
    return report
