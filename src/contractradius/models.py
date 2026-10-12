"""Plain data types shared across the pipeline.

Pipeline: spec_diff -> [ApiChange] ; ts_parser -> [CallSite] ; matcher -> [Finding] ; Report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    BREAKING = "breaking"
    POTENTIALLY_BREAKING = "potentially_breaking"
    NON_BREAKING = "non_breaking"


class ChangeKind(StrEnum):
    ENDPOINT_REMOVED = "endpoint_removed"
    ENDPOINT_ADDED = "endpoint_added"
    FIELD_REMOVED = "field_removed"
    FIELD_ADDED = "field_added"
    FIELD_ADDED_REQUIRED = "field_added_required"
    FIELD_TYPE_CHANGED = "field_type_changed"
    FIELD_BECAME_REQUIRED = "field_became_required"
    FIELD_BECAME_OPTIONAL = "field_became_optional"


@dataclass(frozen=True)
class ApiChange:
    """One difference between the old and new OpenAPI spec."""

    method: str
    path: str
    location: str  # "endpoint" | "request" | "response"
    kind: ChangeKind
    severity: Severity
    field: str | None = None  # dotted path, arrays as "items[].id"
    old_type: str | None = None
    new_type: str | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def summary(self) -> str:
        if self.kind is ChangeKind.ENDPOINT_REMOVED:
            return f"{self.endpoint} was removed"
        if self.kind is ChangeKind.ENDPOINT_ADDED:
            return f"{self.endpoint} was added"
        where = f"{self.location} field '{self.field}'"
        if self.kind is ChangeKind.FIELD_TYPE_CHANGED:
            return f"{where} changed type {self.old_type} -> {self.new_type} on {self.endpoint}"
        what = self.kind.value.replace("field_", "").replace("_", " ")
        return f"{where} {what} on {self.endpoint}"


@dataclass(frozen=True)
class FieldUsage:
    """A place where frontend code reads a response field."""

    field: str  # dotted path, or "*" when the whole body is used and fields are unknown
    line: int
    confidence: float
    # True when the code provably reads only this path itself (e.g. `items.length`, or
    # `items.map(i => i.name)` whose callback was traced), so it says nothing about
    # fields nested under it.
    precise: bool = False


@dataclass
class CallSite:
    """One HTTP call found in frontend source."""

    file: str
    line: int
    method: str
    path: str  # normalized: dynamic segments are "{}"
    client: str  # "fetch" or the client object name (axios, apiClient, ...)
    usages: list[FieldUsage] = field(default_factory=list)
    request_keys: set[str] | None = None  # top-level keys sent; None if not statically known


@dataclass(frozen=True)
class UnanalyzedCall:
    file: str
    line: int
    reason: str


@dataclass(frozen=True)
class Evidence:
    file: str
    line: int
    confidence: float
    note: str = ""


@dataclass
class Finding:
    """An API change plus the frontend evidence that it matters."""

    change: ApiChange
    evidence: list[Evidence]

    @property
    def confidence(self) -> float:
        return max((e.confidence for e in self.evidence), default=0.0)

    @property
    def summary(self) -> str:
        best = max(self.evidence, key=lambda e: e.confidence)
        return (
            f"{self.change.summary} (confidence {self.confidence:.2f}); "
            f"referenced at {best.file}:{best.line}"
        )


@dataclass
class Report:
    changes: list[ApiChange]
    findings: list[Finding]
    unanalyzed: list[UnanalyzedCall] = field(default_factory=list)

    @property
    def real_breaks(self) -> list[Finding]:
        """Definitely-breaking contract changes that have frontend evidence."""
        return [f for f in self.findings if f.change.severity is Severity.BREAKING]

    @property
    def total_changes(self) -> int:
        return len(self.changes)

    @property
    def potentially_breaking(self) -> int:
        return sum(1 for c in self.changes if c.severity is not Severity.NON_BREAKING)

    @property
    def frontend_impacting(self) -> int:
        return len(self.findings)

    def should_fail(self, fail_on: str) -> bool:
        if fail_on == "none":
            return False
        if fail_on == "potentially_breaking":
            return bool(self.findings)
        return bool(self.real_breaks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "total_changes": self.total_changes,
                "potentially_breaking": self.potentially_breaking,
                "frontend_impacting": self.frontend_impacting,
                "real_breaks": len(self.real_breaks),
            },
            "findings": [
                {
                    "endpoint": f.change.endpoint,
                    "location": f.change.location,
                    "field": f.change.field,
                    "change": f.change.kind.value,
                    "severity": f.change.severity.value,
                    "confidence": f.confidence,
                    "evidence": [
                        {"file": e.file, "line": e.line, "confidence": e.confidence, "note": e.note}
                        for e in f.evidence
                    ],
                }
                for f in self.findings
            ],
            "unanalyzed": [
                {"file": u.file, "line": u.line, "reason": u.reason} for u in self.unanalyzed
            ],
        }
