from contractradius.matcher import filter_ignored, match_changes, path_match_factor
from contractradius.models import ApiChange, CallSite, ChangeKind, FieldUsage, Severity


def change(field="user.email", kind=ChangeKind.FIELD_REMOVED, location="response",
           severity=Severity.BREAKING, method="GET", path="/users/{id}"):
    return ApiChange(method, path, location, kind, severity, field)


def site(usages, method="GET", path="/users/{}", request_keys=None, file="a.ts", line=3):
    return CallSite(file, line, method, path, "axios", usages, request_keys)


def U(field, conf=0.9, line=7, precise=False):
    return FieldUsage(field, line, conf, precise)


def run(c, s, **kw):
    return match_changes([c], [s] if not isinstance(s, list) else s, **kw)


def test_exact_field_and_path_match_is_a_real_break():
    [f] = run(change(), site([U("user.email")]))
    assert f.confidence == 0.9
    assert (f.evidence[0].file, f.evidence[0].line) == ("a.ts", 7)


def test_unrelated_field_is_not_matched():
    assert run(change(), site([U("user.name")])) == []


def test_parent_object_access_matches_nested_field_change_at_low_confidence():
    [f] = run(change(), site([U("user", 0.9)]))
    assert f.confidence == 0.4


def test_precise_parent_usage_does_not_match_nested_change():
    assert run(change("items[].price"), site([U("items", 0.9, precise=True)])) == []


def test_child_usage_matches_removed_parent():
    [f] = run(change("user"), site([U("user.email")]))
    assert f.confidence == 0.9


def test_wildcard_field_matches_anything_on_the_endpoint():
    [f] = run(change(), site([U("*", 0.3)]))
    assert f.confidence == 0.3


def test_path_template_normalizes_against_literal_call_path():
    assert path_match_factor("/users/{id}", "/users/42") == 1.0
    assert path_match_factor("/users/{id}", "/users/{}") == 1.0
    assert path_match_factor("/users/me", "/users/{}") == 0.0
    assert path_match_factor("/users/{id}", "/orders/1") == 0.0
    assert path_match_factor("/users/{id}/orders", "/users/1") == 0.0


def test_base_url_prefix_matches_with_penalty():
    assert path_match_factor("/users/{id}", "/api/v1/users/5") == 0.9
    [f] = run(change(), site([U("user.email")], path="/api/v1/users/{}"))
    assert f.confidence == 0.81


def test_endpoint_removed_matches_any_site_on_that_endpoint_regardless_of_field():
    c = ApiChange("GET", "/users/{id}", "endpoint", ChangeKind.ENDPOINT_REMOVED, Severity.BREAKING)
    [f] = run(c, site([]))
    assert f.confidence == 0.95


def test_non_breaking_change_is_never_reported():
    c = change(kind=ChangeKind.FIELD_ADDED, severity=Severity.NON_BREAKING)
    assert run(c, site([U("user.email")])) == []


def test_different_method_does_not_match():
    assert run(change(), site([U("user.email")], method="POST")) == []


def test_multiple_sites_for_same_change_are_all_attached():
    sites = [site([U("user.email")], file="a.ts", line=1), site([U("user.email", 0.4)], file="b.ts")]
    [f] = run(change(), sites)
    assert [e.file for e in f.evidence] == ["a.ts", "b.ts"]
    assert f.confidence == 0.9


def test_min_confidence_filters_weak_evidence():
    assert run(change(), site([U("*", 0.3)]), min_confidence=0.5) == []


def test_request_required_field_missing_from_known_body():
    c = change("currency", ChangeKind.FIELD_ADDED_REQUIRED, "request", path="/orders", method="POST")
    s = site([], method="POST", path="/orders", request_keys={"product_id"})
    [f] = run(c, s)
    assert f.confidence == 0.85


def test_request_required_field_present_or_unknown():
    c = change("currency", ChangeKind.FIELD_ADDED_REQUIRED, "request", path="/orders", method="POST")
    present = site([], method="POST", path="/orders", request_keys={"currency"})
    assert run(c, present) == []
    unknown = site([], method="POST", path="/orders", request_keys=None)
    [f] = run(c, unknown)
    assert f.confidence == 0.4


def test_ignore_filters():
    changes = [change("debug_info"), change("user.debug_info"), change("name", path="/internal/x")]
    kept = filter_ignored(changes, ["/internal/*"], ["*.debug_info"])
    assert [c.field for c in kept] == ["debug_info"]
