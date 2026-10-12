# Contributing

```bash
git clone https://github.com/Bindu2006/ContractRadius.git
cd ContractRadius
pip install -e ".[dev]"
pytest && ruff check src tests && mypy src
```

## Layout

| Module | Responsibility |
|---|---|
| `spec_diff.py` | OpenAPI -> flat field maps -> `ApiChange` list |
| `ts_parser.py` | tree-sitter: find HTTP calls, trace response-field reads |
| `matcher.py` | join changes and call sites; confidence rules |
| `report.py` | text / json / markdown / github rendering |
| `analyzer.py` | `analyze()` entry point |
| `cli.py` | Typer CLI |

## Guidelines

- Detection stays deterministic. Anything probabilistic belongs in confidence, not in guesses.
- When a pattern can't be analyzed, say so ("unanalyzed") instead of pretending.
- Every new frontend pattern needs a test in `tests/test_ts_parser.py`.
