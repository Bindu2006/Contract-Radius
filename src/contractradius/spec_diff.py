"""Compare two OpenAPI documents and list contract changes.

Approach: flatten each operation's request/response schema into a dict of
`dotted.path -> FieldInfo(type, required)` (arrays use `[]`: `items[].id`), then diff
the dicts. Flat paths make diffing, ignoring, and matching against frontend field
accesses all use the same simple string keys.

Supports OpenAPI 3.x and Swagger 2.0, local `$ref`s, allOf/oneOf/anyOf, JSON or YAML.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from contractradius.exceptions import SpecError
from contractradius.models import ApiChange, ChangeKind, Severity

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")


@dataclass(frozen=True)
class FieldInfo:
    type: str
    required: bool


@dataclass
class Operation:
    method: str
    path: str
    response: dict[str, FieldInfo] = field(default_factory=dict)
    request: dict[str, FieldInfo] = field(default_factory=dict)


# ---------------------------------------------------------------- loading


def load_spec(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Load a spec from a dict, or a .json/.yaml/.yml file path."""
    if isinstance(source, dict):
        spec = source
    else:
        path = Path(source)
        if not path.is_file():
            raise SpecError(f"Spec file not found: {path}")
        try:
            text = path.read_text(encoding="utf-8")
            if path.suffix.lower() in (".yaml", ".yml"):
                spec = yaml.safe_load(text)
            else:
                spec = json.loads(text)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise SpecError(f"Could not parse spec {path}: {exc}") from exc

    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise SpecError("Not an OpenAPI document: missing top-level 'paths' object")
    return spec


# ---------------------------------------------------------------- schema flattening


def _deref(spec: dict[str, Any], node: Any, stack: tuple[str, ...]) -> tuple[Any, tuple[str, ...]]:
    """Follow local $refs. Returns (None, stack) when a reference cycle is detected."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise SpecError(f"Unsupported $ref {ref!r}: only local '#/...' references are supported")
        if ref in stack:
            return None, stack
        stack = (*stack, ref)
        target: Any = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise SpecError(f"Unresolvable $ref: {ref}")
            target = target[part]
        node = target
    return node, stack


def _merge_all_of(spec: dict[str, Any], node: dict[str, Any], stack: tuple[str, ...]) -> dict[str, Any]:
    if "allOf" not in node:
        return node
    merged: dict[str, Any] = {k: v for k, v in node.items() if k != "allOf"}
    props: dict[str, Any] = {}
    required: list[str] = []
    for sub in node["allOf"]:
        resolved, sub_stack = _deref(spec, sub, stack)
        if not isinstance(resolved, dict):
            continue
        resolved = _merge_all_of(spec, resolved, sub_stack)
        props.update(resolved.get("properties", {}))
        required += resolved.get("required", [])
        for key in ("type", "items"):
            if key in resolved and key not in merged:
                merged[key] = resolved[key]
    props.update(merged.get("properties", {}))
    merged["properties"] = props
    merged["required"] = list(merged.get("required", [])) + required
    return merged


def _type_of(node: dict[str, Any]) -> str:
    t = node.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "any")
    if t:
        return str(t)
    if "properties" in node:
        return "object"
    if "items" in node:
        return "array"
    return "any"


def _join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _flatten(
    spec: dict[str, Any],
    schema: Any,
    prefix: str,
    required: bool,
    out: dict[str, FieldInfo],
    stack: tuple[str, ...] = (),
) -> None:
    node, stack = _deref(spec, schema, stack)
    if node is None:  # circular $ref: record the field itself but don't recurse forever
        if prefix:
            out.setdefault(prefix, FieldInfo("object", required))
        return
    if not isinstance(node, dict):
        return
    node = _merge_all_of(spec, node, stack)

    if prefix:
        out.setdefault(prefix, FieldInfo(_type_of(node), required))

    required_names = set(node.get("required", []))
    for name, sub in (node.get("properties") or {}).items():
        _flatten(spec, sub, _join(prefix, name), name in required_names, out, stack)

    items = node.get("items")
    if isinstance(items, dict):
        _flatten(spec, items, f"{prefix}[]", True, out, stack)

    # oneOf / anyOf: union of the variants' fields, none of them guaranteed.
    for variant in node.get("oneOf", []) + node.get("anyOf", []):
        extra: dict[str, FieldInfo] = {}
        _flatten(spec, variant, prefix, False, extra, stack)
        for key, info in extra.items():
            if key != prefix:
                out.setdefault(key, FieldInfo(info.type, False))


def flatten_schema(spec: dict[str, Any], schema: Any) -> dict[str, FieldInfo]:
    out: dict[str, FieldInfo] = {}
    _flatten(spec, schema, "", True, out)
    return out


# ---------------------------------------------------------------- operations


def _json_schema(content: Any) -> Any:
    if not isinstance(content, dict) or not content:
        return None
    media = next((m for m in content if m == "application/json"), None) or next(
        (m for m in content if "json" in m), None
    )
    return content[media].get("schema") if media else None


def _response_schema(spec: dict[str, Any], operation: dict[str, Any]) -> Any:
    responses = operation.get("responses") or {}
    codes = sorted(c for c in responses if str(c).startswith("2"))
    if not codes:
        return None
    response, _ = _deref(spec, responses[codes[0]], ())
    if not isinstance(response, dict):
        return None
    if "content" in response:  # OpenAPI 3
        return _json_schema(response["content"])
    return response.get("schema")  # Swagger 2


def _request_schema(spec: dict[str, Any], operation: dict[str, Any]) -> Any:
    body, _ = _deref(spec, operation.get("requestBody") or {}, ())
    if isinstance(body, dict) and "content" in body:  # OpenAPI 3
        return _json_schema(body["content"])
    for param in operation.get("parameters") or []:  # Swagger 2
        param, _ = _deref(spec, param, ())
        if isinstance(param, dict) and param.get("in") == "body":
            return param.get("schema")
    return None


def _canonical(path: str) -> str:
    """`/users/{id}` and `/users/{user_id}` are the same endpoint."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def extract_operations(spec: dict[str, Any]) -> dict[tuple[str, str], Operation]:
    ops: dict[tuple[str, str], Operation] = {}
    for path, item in spec["paths"].items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            ops[(method.upper(), _canonical(path))] = Operation(
                method=method.upper(),
                path=path,
                response=flatten_schema(spec, _response_schema(spec, operation)),
                request=flatten_schema(spec, _request_schema(spec, operation)),
            )
    return ops


# ---------------------------------------------------------------- diffing


def _parent(key: str) -> str | None:
    if key.endswith("[]"):
        return key[:-2] or None
    idx = key.rfind(".")
    return key[:idx] if idx >= 0 else None


def _ancestors(key: str) -> Iterator[str]:
    parent = _parent(key)
    while parent:
        yield parent
        parent = _parent(parent)


def _top_most(keys: set[str]) -> list[str]:
    """Drop keys whose ancestor is also in the set (report `address`, not `address.city`)."""
    return sorted(k for k in keys if not any(a in keys for a in _ancestors(k)))


def _effectively_required(key: str, fields: dict[str, FieldInfo]) -> bool:
    chain = [key, *_ancestors(key)]
    return all(fields[k].required for k in chain if k in fields and not k.endswith("[]"))


def _diff_fields(
    old: Operation, new: Operation, location: str, old_fields: dict[str, FieldInfo],
    new_fields: dict[str, FieldInfo],
) -> list[ApiChange]:
    changes: list[ApiChange] = []
    is_response = location == "response"

    def make(kind: ChangeKind, severity: Severity, key: str, **kw: str | None) -> ApiChange:
        return ApiChange(
            method=old.method, path=old.path, location=location, kind=kind,
            severity=severity, field=key, **kw,
        )

    for key in _top_most(set(old_fields) - set(new_fields)):
        severity = Severity.BREAKING if is_response else Severity.NON_BREAKING
        changes.append(make(ChangeKind.FIELD_REMOVED, severity, key, old_type=old_fields[key].type))

    for key in _top_most(set(new_fields) - set(old_fields)):
        if not is_response and _effectively_required(key, new_fields):
            changes.append(make(ChangeKind.FIELD_ADDED_REQUIRED, Severity.BREAKING, key,
                                new_type=new_fields[key].type))
        else:
            changes.append(make(ChangeKind.FIELD_ADDED, Severity.NON_BREAKING, key,
                                new_type=new_fields[key].type))

    for key in sorted(set(old_fields) & set(new_fields)):
        o, n = old_fields[key], new_fields[key]
        if o.type != n.type and "any" not in (o.type, n.type):
            changes.append(make(ChangeKind.FIELD_TYPE_CHANGED, Severity.BREAKING, key,
                                old_type=o.type, new_type=n.type))
        if o.required != n.required and not key.endswith("[]"):
            if n.required:  # optional -> required
                sev = Severity.NON_BREAKING if is_response else Severity.BREAKING
                changes.append(make(ChangeKind.FIELD_BECAME_REQUIRED, sev, key))
            else:  # required -> optional
                sev = Severity.POTENTIALLY_BREAKING if is_response else Severity.NON_BREAKING
                changes.append(make(ChangeKind.FIELD_BECAME_OPTIONAL, sev, key))
    return changes


def diff_specs(old_spec: dict[str, Any], new_spec: dict[str, Any]) -> list[ApiChange]:
    """Return every difference between two loaded specs, breaking or not."""
    old_ops, new_ops = extract_operations(old_spec), extract_operations(new_spec)
    changes: list[ApiChange] = []

    for key in sorted(old_ops.keys() - new_ops.keys(), key=lambda k: (k[1], k[0])):
        op = old_ops[key]
        changes.append(ApiChange(op.method, op.path, "endpoint", ChangeKind.ENDPOINT_REMOVED,
                                 Severity.BREAKING))
    for key in sorted(new_ops.keys() - old_ops.keys(), key=lambda k: (k[1], k[0])):
        op = new_ops[key]
        changes.append(ApiChange(op.method, op.path, "endpoint", ChangeKind.ENDPOINT_ADDED,
                                 Severity.NON_BREAKING))
    for key in sorted(old_ops.keys() & new_ops.keys(), key=lambda k: (k[1], k[0])):
        old, new = old_ops[key], new_ops[key]
        changes += _diff_fields(old, new, "response", old.response, new.response)
        changes += _diff_fields(old, new, "request", old.request, new.request)
    return changes
