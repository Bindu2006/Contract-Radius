# ContractRadius

**Find the API changes that are most likely to affect your frontend, not just the changes that look dangerous in an API diff.**

A normal OpenAPI diff tells you `User.age was removed`. But what if your frontend never reads `age`?

ContractRadius compares **old OpenAPI spec + new OpenAPI spec + your frontend source code** and reports only the changes that have **evidence of affecting real frontend code**, with file and line numbers and a confidence score.

```
$ contractradius diff openapi_old.json openapi_new.json --frontend ./frontend/src

ENDPOINT              FIELD      CHANGE                 CONFIDENCE   LOCATION

DELETE /legacy/{id}   -          endpoint_removed       0.95         src/api/legacy.ts:2
GET /orders           [].total   field_type_changed     0.90         src/api/orders.ts:6
GET /users/{id}       email      field_removed          0.90         src/api/users.ts:7
POST /orders          currency   field_added_required   0.85         src/api/orders.ts:11

Total API changes: 9
Potentially breaking: 8
Frontend-impacting: 4
```

Removed `age`, `address.zip`, and `items[].price` are **not** reported, because nothing in the frontend reads them.

## Install

```bash
pip install -e .            # from a clone of this repo
pip install -e ".[dev]"     # plus pytest, ruff, mypy
```

Requires Python 3.11+.

> If the `contractradius` command is "not recognized" on Windows, your Python `Scripts` folder
> isn't on PATH. Use `python -m contractradius ...` instead; it behaves identically.

## Usage

### 1. Get two specs

```bash
curl http://localhost:8000/openapi.json > openapi_old.json
# make your backend changes
curl http://localhost:8000/openapi.json > openapi_new.json
```

### 2. Run the diff

```bash
contractradius diff openapi_old.json openapi_new.json --frontend ./src
```

### 3. Output formats

```bash
--format text        # default table
--format json        # machine-readable
--format markdown    # for docs/reports
--format github      # pull-request comment body
```

### 4. CI

```yaml
- run: pip install contractradius
- run: contractradius diff openapi_old.json openapi_new.json --frontend ./src
```

Exit codes: `0` ok, `1` findings at the configured `fail_on` level, `2` usage/input error.
Use `--no-fail-ci` to print the report without failing the build.

### 5. Configuration (optional)

`.contractradius.yaml` in your repo root:

```yaml
client_names: [axios, myCustomClient]   # default: axios, apiClient, api, client, http, httpClient
ignore_paths: ["/internal/*"]           # fnmatch on the spec path
ignore_fields: ["*.debug_info"]         # fnmatch on the dotted field
min_confidence: 0.5                     # default 0.3
fail_on: breaking                       # breaking | potentially_breaking | none
```

### As a library

```python
from contractradius import analyze

report = analyze("openapi_old.json", "openapi_new.json", frontend_src="./src")
for finding in report.real_breaks:
    print(finding.summary)
```

## Try the demo

```bash
contractradius diff examples/demo/openapi_old.json examples/demo/openapi_new.json \
    --frontend examples/demo/frontend/src
```

## How it works

```
 Old spec + New spec --> Spec diff engine --> [API changes]
                                                    |
 Frontend .ts/.tsx --> Usage analyzer ------> [call sites + fields read]
                                                    |
                                          Impact correlation --> Report
```

1. **Spec diff** (`spec_diff.py`): flattens each operation's request/response schema to paths like `items[].id`, resolves `$ref`/`allOf`/`oneOf`, and diffs them: removed/added/retyped fields, optional<->required, removed endpoints.
2. **Usage analyzer** (`ts_parser.py`): parses TypeScript/TSX with tree-sitter, finds `fetch(...)` and `axios.get(...)`-style calls, and traces what is read from the result.
3. **Matcher** (`matcher.py`): joins them on method + path, and assigns confidence.

### Confidence

| Evidence | Confidence |
|---|---|
| Calls a removed endpoint | 0.95 |
| Code reads the changed field (or a child of it) | 0.90 |
| Request is missing a newly required field | 0.85 |
| Request body not statically known | 0.40 |
| Code uses a parent object whole (`setUser(user)`) | 0.40 |
| Whole response body used, fields unknown | 0.30 |

Matching through a base-URL prefix (`/api/v1/users` vs spec `/users`) multiplies by 0.9.

## Limitations (v0.1)

ContractRadius is static analysis and says so with confidence scores instead of certainty.

- TypeScript/TSX only; `fetch` and axios-style clients only.
- Only follows data within one function. If `getUser()` returns `axios.get(...)` and a different file reads `.email`, that read is not linked.
- URLs must be static strings or template literals (`/users/${id}`). Others appear under "Could not analyze".
- Query/path parameter changes are not diffed yet; only request/response bodies.
- Only local `$ref`s are resolved.
- No AI explanations yet. The design keeps detection deterministic; a future optional step would only *explain* findings.

## Development

```bash
pytest
ruff check src tests
mypy src
```

See [docs/contributing.md](docs/contributing.md).

## License

MIT, see [LICENSE](LICENSE).
