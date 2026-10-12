import json
from copy import deepcopy

import pytest

from contractradius.exceptions import SpecError
from contractradius.models import ChangeKind, Severity
from contractradius.spec_diff import diff_specs, flatten_schema, load_spec


def make_spec(response_schema=None, request_schema=None, path="/users/{id}", method="get"):
    op = {"responses": {"200": {"description": "ok", "content": {
        "application/json": {"schema": response_schema or {"type": "object"}}}}}}
    if request_schema:
        op["requestBody"] = {"content": {"application/json": {"schema": request_schema}}}
    return {"openapi": "3.0.3", "paths": {path: {method: op}}}


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


S, INT = {"type": "string"}, {"type": "integer"}


def kinds(changes):
    return {(c.location, c.kind, c.field, c.severity) for c in changes}


def test_endpoint_removed_is_breaking():
    old = make_spec()
    new = {"openapi": "3.0.3", "paths": {}}
    [change] = diff_specs(old, new)
    assert change.kind is ChangeKind.ENDPOINT_REMOVED
    assert change.severity is Severity.BREAKING
    assert change.endpoint == "GET /users/{id}"


def test_endpoint_added_is_non_breaking():
    [change] = diff_specs({"openapi": "3.0.3", "paths": {}}, make_spec())
    assert change.kind is ChangeKind.ENDPOINT_ADDED
    assert change.severity is Severity.NON_BREAKING


def test_path_param_rename_is_not_a_removal():
    old = make_spec(path="/users/{id}")
    new = make_spec(path="/users/{user_id}")
    assert diff_specs(old, new) == []


def test_response_field_removed_is_breaking():
    old = make_spec(obj({"a": S, "b": S}))
    new = make_spec(obj({"a": S}))
    assert kinds(diff_specs(old, new)) == {
        ("response", ChangeKind.FIELD_REMOVED, "b", Severity.BREAKING)}


def test_response_field_type_changed_is_breaking():
    old = make_spec(obj({"a": INT}))
    new = make_spec(obj({"a": S}))
    [change] = diff_specs(old, new)
    assert (change.kind, change.old_type, change.new_type) == (
        ChangeKind.FIELD_TYPE_CHANGED, "integer", "string")


def test_new_optional_response_field_is_non_breaking():
    old = make_spec(obj({"a": S}))
    new = make_spec(obj({"a": S, "b": S}))
    [change] = diff_specs(old, new)
    assert change.kind is ChangeKind.FIELD_ADDED
    assert change.severity is Severity.NON_BREAKING


def test_response_field_became_optional_is_potentially_breaking():
    old = make_spec(obj({"a": S}, ["a"]))
    new = make_spec(obj({"a": S}))
    [change] = diff_specs(old, new)
    assert change.kind is ChangeKind.FIELD_BECAME_OPTIONAL
    assert change.severity is Severity.POTENTIALLY_BREAKING


def test_request_field_newly_required_is_breaking():
    old = make_spec(request_schema=obj({"a": S}), method="post")
    new = make_spec(request_schema=obj({"a": S}, ["a"]), method="post")
    [change] = diff_specs(old, new)
    assert (change.location, change.kind, change.severity) == (
        "request", ChangeKind.FIELD_BECAME_REQUIRED, Severity.BREAKING)


def test_request_field_added_required_vs_optional():
    old = make_spec(request_schema=obj({"a": S}), method="post")
    new = make_spec(request_schema=obj({"a": S, "req": S, "opt": S}, ["req"]), method="post")
    got = {(c.field, c.kind, c.severity) for c in diff_specs(old, new)}
    assert got == {
        ("req", ChangeKind.FIELD_ADDED_REQUIRED, Severity.BREAKING),
        ("opt", ChangeKind.FIELD_ADDED, Severity.NON_BREAKING),
    }


def test_request_field_removed_is_non_breaking():
    old = make_spec(request_schema=obj({"a": S, "b": S}), method="post")
    new = make_spec(request_schema=obj({"a": S}), method="post")
    [change] = diff_specs(old, new)
    assert change.severity is Severity.NON_BREAKING


def test_unchanged_spec_produces_no_changes():
    spec = make_spec(obj({"a": S}))
    assert diff_specs(spec, deepcopy(spec)) == []


def test_nested_field_removed_reports_full_dotted_path():
    old = make_spec(obj({"user": obj({"address": obj({"city": S, "zip": S})})}))
    new = make_spec(obj({"user": obj({"address": obj({"city": S})})}))
    [change] = diff_specs(old, new)
    assert change.field == "user.address.zip"


def test_removed_parent_is_reported_once_not_per_child():
    old = make_spec(obj({"address": obj({"city": S, "zip": S})}))
    new = make_spec(obj({}))
    [change] = diff_specs(old, new)
    assert change.field == "address"


def test_array_item_fields_use_bracket_paths():
    item = obj({"id": INT, "price": INT})
    old = make_spec(obj({"items": {"type": "array", "items": item}}))
    new = make_spec(obj({"items": {"type": "array", "items": obj({"id": INT})}}))
    [change] = diff_specs(old, new)
    assert change.field == "items[].price"


def test_top_level_array_response():
    old = make_spec({"type": "array", "items": obj({"id": INT, "x": S})})
    new = make_spec({"type": "array", "items": obj({"id": INT})})
    [change] = diff_specs(old, new)
    assert change.field == "[].x"


def test_refs_and_allof_are_resolved():
    def spec(extra):
        return {
            "openapi": "3.0.3",
            "paths": {"/u": {"get": {"responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}}}}},
            "components": {"schemas": {
                "Base": obj({"id": INT}),
                "User": {"allOf": [{"$ref": "#/components/schemas/Base"}, obj(extra)]},
            }},
        }
    [change] = diff_specs(spec({"name": S, "email": S}), spec({"name": S}))
    assert change.field == "email"


def test_circular_refs_do_not_hang():
    spec = {
        "paths": {"/n": {"get": {"responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}}}}},
        "components": {"schemas": {"Node": obj(
            {"value": INT, "next": {"$ref": "#/components/schemas/Node"}})}},
    }
    fields = flatten_schema(spec, {"$ref": "#/components/schemas/Node"})
    assert "value" in fields and "next" in fields


def test_oneof_fields_are_union_and_optional():
    schema = {"oneOf": [obj({"a": S}, ["a"]), obj({"b": S}, ["b"])]}
    fields = flatten_schema({"paths": {}}, schema)
    assert set(fields) == {"a", "b"}
    assert not fields["a"].required


def test_swagger2_schema_and_body_param():
    spec = {
        "swagger": "2.0",
        "paths": {"/x": {"post": {
            "parameters": [{"in": "body", "name": "b", "schema": obj({"a": S}, ["a"])}],
            "responses": {"200": {"description": "ok", "schema": obj({"r": S})}},
        }}},
    }
    new = deepcopy(spec)
    new["paths"]["/x"]["post"]["responses"]["200"]["schema"] = obj({})
    [change] = diff_specs(spec, new)
    assert change.field == "r"


def test_external_ref_raises_spec_error():
    spec = make_spec({"$ref": "other.json#/Foo"})
    with pytest.raises(SpecError):
        diff_specs(spec, spec)


def test_load_spec_errors(tmp_path):
    with pytest.raises(SpecError):
        load_spec(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(SpecError):
        load_spec(bad)
    nopaths = tmp_path / "nopaths.json"
    nopaths.write_text(json.dumps({"openapi": "3.0.0"}))
    with pytest.raises(SpecError):
        load_spec(nopaths)


def test_load_spec_reads_yaml(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text("openapi: 3.0.0\npaths:\n  /a:\n    get:\n      responses:\n        '200':\n"
                 "          description: ok\n")
    assert "/a" in load_spec(f)["paths"]
