import json
from pathlib import Path

import pytest

from contractradius import Config, SpecError, analyze
from contractradius.cli import app
from contractradius.report import render

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo"
OLD, NEW, FRONTEND = DEMO / "openapi_old.json", DEMO / "openapi_new.json", DEMO / "frontend" / "src"


def keys(report):
    return {(f.change.endpoint, f.change.field, f.change.kind.value) for f in report.findings}


def test_analyze_full_pipeline_matches_expected_real_breaks():
    report = analyze(OLD, NEW, FRONTEND)
    assert keys(report) == {
        ("DELETE /legacy/{id}", None, "endpoint_removed"),
        ("GET /users/{id}", "email", "field_removed"),
        ("GET /orders", "[].total", "field_type_changed"),
        ("POST /orders", "currency", "field_added_required"),
    }
    assert len(report.real_breaks) == 4
    # Changed but unused by the frontend: must NOT be reported.
    unused = {c.field for c in report.changes} & {"age", "address.zip", "items[].price"}
    assert unused == {"age", "address.zip", "items[].price"}
    assert not ({"age", "address.zip", "items[].price"} & {f.change.field for f in report.findings})


def test_analyze_with_identical_specs_has_no_breaks():
    report = analyze(OLD, OLD, FRONTEND)
    assert report.changes == [] and report.findings == []
    assert not report.should_fail("potentially_breaking")


def test_analyze_accepts_dicts():
    old, new = json.loads(OLD.read_text()), json.loads(NEW.read_text())
    assert len(analyze(old, new, FRONTEND).findings) == 4


def test_ignore_config_suppresses_findings():
    cfg = Config(ignore_paths=["/legacy/*"], ignore_fields=["email"])
    report = analyze(OLD, NEW, FRONTEND, config=cfg)
    assert {f.change.endpoint for f in report.findings} == {"GET /orders", "POST /orders"}


def test_custom_client_names_change_what_is_detected():
    # With only `axios` as a client, the apiClient calls in orders.ts are invisible.
    report = analyze(OLD, NEW, FRONTEND, config=Config(client_names=["axios"]))
    assert {f.change.endpoint for f in report.findings} == {"DELETE /legacy/{id}", "GET /users/{id}"}


def test_should_fail_levels():
    report = analyze(OLD, NEW, FRONTEND)
    assert report.should_fail("breaking") and report.should_fail("potentially_breaking")
    assert not report.should_fail("none")


def test_bad_spec_raises():
    with pytest.raises(SpecError):
        analyze(DEMO / "nope.json", NEW, FRONTEND)


def test_report_formats():
    report = analyze(OLD, NEW, FRONTEND)
    text = render(report, "text")
    assert "GET /users/{id}" in text and "src/api/users.ts:7" in text
    assert "Frontend-impacting: 4" in text

    data = json.loads(render(report, "json"))
    assert data["summary"]["real_breaks"] == 4
    assert data["findings"][0]["evidence"][0]["file"].startswith("src/")

    md = render(report, "markdown")
    assert md.startswith("# ContractRadius report") and "| Endpoint |" in md

    gh = render(report, "github")
    assert "4 breaking API change(s)" in gh

    clean = analyze(OLD, OLD, FRONTEND)
    assert "No frontend-impacting" in render(clean, "text")
    assert "No frontend-impacting" in render(clean, "github")


def test_unanalyzed_calls_show_up_in_text_report(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("export const f = (u: string) => fetch(u);")
    report = analyze(OLD, OLD, src)
    assert len(report.unanalyzed) == 1
    assert "Could not analyze 1 call" in render(report, "text")


# ----------------------------------------------------------------- CLI

from typer.testing import CliRunner  # noqa: E402

runner = CliRunner()


def run_cli(*extra):
    return runner.invoke(app, ["diff", str(OLD), str(NEW), "--frontend", str(FRONTEND), *extra])


def test_cli_fails_ci_on_real_breaks_by_default():
    result = run_cli()
    assert result.exit_code == 1
    assert "GET /users/{id}" in result.output


def test_cli_no_fail_ci_exits_zero():
    assert run_cli("--no-fail-ci").exit_code == 0


def test_cli_json_format_is_valid_json():
    result = run_cli("--format", "json", "--no-fail-ci")
    assert json.loads(result.output)["summary"]["frontend_impacting"] == 4


def test_cli_min_confidence_filters():
    result = run_cli("--format", "json", "--no-fail-ci", "--min-confidence", "0.93")
    assert json.loads(result.output)["summary"]["frontend_impacting"] == 1


def test_cli_config_file_fail_on_none(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("fail_on: none\n")
    assert run_cli("--config", str(cfg)).exit_code == 0


def test_cli_bad_spec_exits_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    result = runner.invoke(app, ["diff", str(bad), str(NEW), "--frontend", str(FRONTEND)])
    assert result.exit_code == 2


def test_cli_bad_config_exits_2(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("- list\n")
    assert run_cli("--config", str(cfg)).exit_code == 2


def test_cli_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0 and result.output.strip() == "0.1.1"
