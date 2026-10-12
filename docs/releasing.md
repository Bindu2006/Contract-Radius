# Releasing to PyPI

## One-time setup
1. Create accounts on https://pypi.org and https://test.pypi.org and enable 2FA.
2. Check the name is free: open https://pypi.org/project/contractradius/ (404 = free).
3. Create an API token (Account settings -> API tokens). For the first upload the scope must be
   "Entire account"; afterwards create a token scoped to this project.

## Every release
1. Bump `version` in `pyproject.toml` and `__version__` in `src/contractradius/__init__.py`
   (they must match). **PyPI never accepts the same version twice, even after deleting it.**
2. Move the CHANGELOG entry from "Unreleased" to the new version.
3. Check everything locally:
   ```
   pip install -e ".[dev]"
   pytest && ruff check src tests && mypy src
   ```
4. Build fresh and validate:
   ```
   rmdir /s /q dist        (PowerShell: Remove-Item -Recurse -Force dist)
   python -m build
   python -m twine check dist/*
   ```
5. Upload to TestPyPI first:
   ```
   python -m twine upload --repository testpypi dist/*
   ```
   Username `__token__`, password = your token (starts with `pypi-`).
6. Test the *published* package in a clean environment:
   ```
   python -m venv t && t\Scripts\activate
   pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple contractradius
   python -m contractradius version
   ```
7. Upload to the real PyPI:
   ```
   python -m twine upload dist/*
   ```
8. Tag it: `git tag v0.1.0 && git push --tags`.

## Automatic publishing (optional)
`.github/workflows/publish.yml` publishes when you create a GitHub Release. Set it up once on PyPI:
Project -> Publishing -> add a trusted publisher (owner `Bindu2006`, repo `ContractRadius`,
workflow `publish.yml`, environment `pypi`). For a brand-new project use "pending publisher".