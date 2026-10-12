import pytest

from contractradius.config import DEFAULT_CLIENT_NAMES
from contractradius.exceptions import FrontendError
from contractradius.ts_parser import analyze_source, scan_frontend

CLIENTS = set(DEFAULT_CLIENT_NAMES)


def analyze(code: str, suffix: str = ".ts"):
    return analyze_source(code.encode(), suffix, "t" + suffix, CLIENTS)


def fields(site, min_conf=0.0):
    return {u.field for u in site.usages if u.confidence >= min_conf}


def test_destructured_axios_response_gives_high_confidence_field():
    r = analyze("""
async function f() {
  const { data: { user } } = await axios.get('/users/1');
  return user.email;
}""")
    [site] = r.sites
    assert (site.method, site.path) == ("GET", "/users/1")
    assert {"user", "user.email"} <= fields(site)
    assert all(u.confidence >= 0.9 for u in site.usages if u.field == "user.email")


def test_destructured_data_then_member_access():
    [site] = analyze("""
async function f() {
  const { data } = await axios.get('/u');
  return data.user.email;
}""").sites
    assert "user.email" in fields(site)


def test_plain_identifier_member_chain_is_traced():
    [site] = analyze("""
async function f() {
  const res = await apiClient.get('/u');
  return res.data.user.name;
}""").sites
    assert fields(site) == {"user.name"}


def test_status_and_headers_are_not_body_fields():
    [site] = analyze("""
async function f() {
  const res = await axios.get('/u');
  return [res.status, res.headers['x']];
}""").sites
    assert fields(site) == set()


def test_inline_await_chain():
    [site] = analyze("""
async function f() { return (await axios.get('/u')).data.email; }""").sites
    assert "email" in fields(site)


def test_then_arrow_param_is_traced():
    [site] = analyze("""
function f() { return axios.get('/u').then((res) => res.data.email); }""").sites
    assert "email" in fields(site)


def test_then_with_destructured_param():
    [site] = analyze("""
function f() { return axios.get('/u').then(({ data }) => data.name); }""").sites
    assert "name" in fields(site)


def test_fetch_call_is_detected_with_get_default():
    [site] = analyze("""
async function f() {
  const res = await fetch('/items');
  const body = await res.json();
  return body.total;
}""").sites
    assert (site.method, site.client) == ("GET", "fetch")
    assert "total" in fields(site)


def test_fetch_then_json_chain():
    [site] = analyze("""
function f() {
  return fetch('/items').then(r => r.json()).then(body => body.total);
}""").sites
    assert "total" in fields(site)


def test_fetch_method_and_body_keys():
    [site] = analyze("""
function f(x) {
  return fetch('/items', { method: 'POST', body: JSON.stringify({ name: x, qty: 1 }) });
}""").sites
    assert site.method == "POST"
    assert site.request_keys == {"name", "qty"}


def test_axios_post_request_keys_and_unknown_body():
    r = analyze("""
function a(x) { return axios.post('/a', { one: 1, two: x }); }
function b(payload) { return axios.post('/b', payload); }
function c() { return axios.post('/c'); }
function d(rest) { return axios.post('/d', { ...rest, k: 1 }); }""")
    got = {s.path: s.request_keys for s in r.sites}
    assert got == {"/a": {"one", "two"}, "/b": None, "/c": set(), "/d": None}


def test_array_callback_item_fields_are_traced():
    [site] = analyze("""
async function f() {
  const { data } = await axios.get('/items');
  return data.items.map((i) => i.price);
}""").sites
    assert "items[].price" in fields(site)


def test_top_level_array_callback_and_subscript():
    [site] = analyze("""
async function f() {
  const res = await axios.get('/xs');
  const first = res.data[0].name;
  return res.data.filter((x) => x.active);
}""").sites
    assert {"[].name", "[].active"} <= fields(site)


def test_alias_through_variable():
    [site] = analyze("""
async function f() {
  const { data } = await axios.get('/u');
  const user = data.user;
  return user.email;
}""").sites
    assert "user.email" in fields(site)


def test_destructuring_from_member_chain():
    [site] = analyze("""
async function f() {
  const res = await axios.get('/u');
  const { email, name } = res.data.user;
  return email + name;
}""").sites
    assert {"user.email", "user.name"} <= fields(site)


def test_passing_whole_body_is_low_confidence_wildcard():
    [site] = analyze("""
async function f() {
  const { data } = await axios.get('/u');
  render(data);
}""").sites
    assert [(u.field, u.confidence) for u in site.usages] == [("*", 0.3)]


def test_passing_object_whole_is_bare_usage():
    [site] = analyze("""
async function f() {
  const { data: { user } } = await axios.get('/u');
  render(user);
}""").sites
    by_conf = {(u.field, u.confidence) for u in site.usages}
    assert ("user", 0.4) in by_conf


def test_variable_name_collision_across_functions_does_not_cross_contaminate():
    [a, b] = analyze("""
async function a() { const { data } = await axios.get('/a'); return data.alpha; }
async function b() { const { data } = await axios.get('/b'); return data.beta; }""").sites
    assert fields(a) == {"alpha"}
    assert fields(b) == {"beta"}


def test_non_client_call_is_ignored():
    r = analyze("""
const m = new Map();
m.get('/users');
other.get('/users');
helper('/users');
somethingElse.post('/x', {});""")
    assert r.sites == [] and r.unanalyzed == []


def test_custom_client_names():
    r = analyze_source(b"async function f(){ const r = await myApi.get('/a'); }", ".ts", "f.ts",
                       {"myApi"})
    assert len(r.sites) == 1


def test_template_literal_and_base_url_paths():
    r = analyze("""
function f(id) {
  axios.get(`/users/${id}/orders`);
  axios.get(`${BASE}/items`);
  axios.get('http://localhost:8000/health?verbose=1');
  axios.get(BASE + '/legacy');
}""")
    assert [s.path for s in r.sites] == ["/users/{}/orders", "{}/items", "/health", "{}/legacy"]


def test_dynamic_url_is_reported_as_unanalyzed():
    r = analyze("function f(url) { return fetch(url); }")
    assert r.sites == []
    assert len(r.unanalyzed) == 1 and r.unanalyzed[0].line == 1


def test_generic_type_arguments_dont_break_detection():
    [site] = analyze("""
async function f() {
  const { data } = await axios.get<User>('/u');
  return data.email;
}""").sites
    assert "email" in fields(site)


def test_tsx_files_are_parsed():
    [site] = analyze("""
export function C() {
  useEffect(() => {
    axios.get('/u').then((r) => setUser(r.data.user));
  }, []);
  return <div />;
}""", ".tsx").sites
    assert "user" in fields(site)


def test_non_null_and_optional_chaining():
    [site] = analyze("""
async function f() {
  const res = await axios.get('/u');
  return res.data?.user!.email;
}""").sites
    assert "user.email" in fields(site)


def test_scan_frontend_walks_dirs_and_skips_node_modules(tmp_path):
    (tmp_path / "src" / "api").mkdir(parents=True)
    (tmp_path / "src" / "api" / "a.ts").write_text("axios.get('/a');")
    (tmp_path / "src" / "B.tsx").write_text("axios.get('/b');")
    (tmp_path / "src" / "types.d.ts").write_text("axios.get('/d');")
    (tmp_path / "src" / "notes.js").write_text("axios.get('/js');")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "i.ts").write_text("axios.get('/nm');")
    result = scan_frontend(tmp_path / "src", CLIENTS)
    assert sorted(s.path for s in result.sites) == ["/a", "/b"]
    assert {s.file for s in result.sites} == {"src/api/a.ts", "src/B.tsx"}


def test_missing_directory_raises(tmp_path):
    with pytest.raises(FrontendError):
        scan_frontend(tmp_path / "does_not_exist", CLIENTS)
