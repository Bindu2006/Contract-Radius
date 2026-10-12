"""Correlate API changes with frontend call sites.

For each non-trivial change we look for call sites hitting the same endpoint and
check whether the code reads (or, for request changes, fails to send) the field.
"""

from __future__ import annotations

from fnmatch import fnmatch

from contractradius.models import (
    ApiChange,
    CallSite,
    ChangeKind,
    Evidence,
    FieldUsage,
    Finding,
    Severity,
)

CONF_ENDPOINT_REMOVED = 0.95
CONF_REQUEST_MISSING = 0.85
CONF_REQUEST_UNKNOWN = 0.4
CONF_REQUEST_SENT = 0.5
CONF_ANCESTOR_CAP = 0.4  # code reads a parent object, not the changed field itself
SUFFIX_PATH_FACTOR = 0.9  # call path only matches after ignoring a base-URL prefix


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def _segment_ok(spec_seg: str, call_seg: str) -> bool:
    if spec_seg.startswith("{") and spec_seg.endswith("}"):
        return True  # spec parameter matches anything
    if "{}" in call_seg:
        return False  # dynamic call segment can't prove a literal spec segment
    return spec_seg == call_seg


def path_match_factor(spec_path: str, call_path: str) -> float:
    """1.0 exact, 0.9 if the call has an extra base prefix (e.g. /api/v1), else 0."""
    spec, call = _segments(spec_path), _segments(call_path)
    if not spec:
        return 1.0 if not call else 0.0
    if len(spec) == len(call) and all(_segment_ok(s, c) for s, c in zip(spec, call, strict=True)):
        return 1.0
    if len(call) > len(spec) and all(
        _segment_ok(s, c) for s, c in zip(spec, call[-len(spec):], strict=True)
    ):
        return SUFFIX_PATH_FACTOR
    return 0.0


def _is_descendant(child: str, parent: str) -> bool:
    return child.startswith(parent) and child[len(parent):][:1] in (".", "[")


def usage_confidence(change_field: str, usage: FieldUsage) -> float:
    """How strongly does this usage suggest the changed field is read? 0 = unrelated."""
    if usage.field == "*":
        return usage.confidence
    if usage.field == change_field or _is_descendant(usage.field, change_field):
        return usage.confidence
    if _is_descendant(change_field, usage.field) and not usage.precise:
        return min(usage.confidence, CONF_ANCESTOR_CAP)  # code touches a parent object
    return 0.0


def _is_ignored(change: ApiChange, ignore_paths: list[str], ignore_fields: list[str]) -> bool:
    if any(fnmatch(change.path, pattern) for pattern in ignore_paths):
        return True
    return change.field is not None and any(fnmatch(change.field, p) for p in ignore_fields)


def filter_ignored(
    changes: list[ApiChange], ignore_paths: list[str], ignore_fields: list[str]
) -> list[ApiChange]:
    return [c for c in changes if not _is_ignored(c, ignore_paths, ignore_fields)]


def _match_response(change: ApiChange, site: CallSite, factor: float) -> Evidence | None:
    assert change.field is not None
    best: tuple[float, FieldUsage] | None = None
    for usage in site.usages:
        conf = usage_confidence(change.field, usage)
        if conf > 0 and (best is None or conf > best[0]):
            best = (conf, usage)
    if best is None:
        return None
    conf, usage = best
    note = "whole response body used" if usage.field == "*" else f"reads '{usage.field}'"
    return Evidence(site.file, usage.line, round(conf * factor, 2), note)


def _match_request(change: ApiChange, site: CallSite, factor: float) -> Evidence | None:
    assert change.field is not None
    top_key = change.field.split(".")[0].split("[")[0]
    keys = site.request_keys
    if change.kind in (ChangeKind.FIELD_ADDED_REQUIRED, ChangeKind.FIELD_BECAME_REQUIRED):
        if keys is None:
            return Evidence(site.file, site.line, round(CONF_REQUEST_UNKNOWN * factor, 2),
                            f"request body not statically known; may omit '{change.field}'")
        if top_key not in keys:
            return Evidence(site.file, site.line, round(CONF_REQUEST_MISSING * factor, 2),
                            f"request does not send '{top_key}'")
        return None
    if change.kind is ChangeKind.FIELD_TYPE_CHANGED and keys is not None and top_key in keys:
        return Evidence(site.file, site.line, round(CONF_REQUEST_SENT * factor, 2),
                        f"request sends '{top_key}'; type changed")
    return None


def match_changes(
    changes: list[ApiChange], sites: list[CallSite], min_confidence: float = 0.0
) -> list[Finding]:
    findings: list[Finding] = []
    for change in changes:
        if change.severity is Severity.NON_BREAKING:
            continue

        evidence: list[Evidence] = []
        for site in sites:
            if site.method != change.method:
                continue
            factor = path_match_factor(change.path, site.path)
            if factor == 0.0:
                continue

            item: Evidence | None
            if change.kind is ChangeKind.ENDPOINT_REMOVED:
                item = Evidence(site.file, site.line, round(CONF_ENDPOINT_REMOVED * factor, 2),
                                "calls a removed endpoint")
            elif change.location == "response":
                item = _match_response(change, site, factor)
            elif change.location == "request":
                item = _match_request(change, site, factor)
            else:
                item = None
            if item is not None and item.confidence >= min_confidence:
                evidence.append(item)

        if evidence:
            evidence.sort(key=lambda e: (e.file, e.line))
            findings.append(Finding(change, evidence))

    findings.sort(key=lambda f: (-f.confidence, f.change.endpoint, f.change.field or ""))
    return findings
