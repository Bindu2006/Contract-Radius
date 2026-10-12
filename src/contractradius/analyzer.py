"""The public `analyze()` entry point tying the pipeline together."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from contractradius.config import Config
from contractradius.matcher import filter_ignored, match_changes
from contractradius.models import Report
from contractradius.spec_diff import diff_specs, load_spec
from contractradius.ts_parser import scan_frontend

SpecSource = str | Path | dict[str, Any]


def analyze(
    old_spec: SpecSource,
    new_spec: SpecSource,
    frontend_src: str | Path,
    config: Config | None = None,
) -> Report:
    """Diff two OpenAPI specs and report which changes the frontend code appears to use."""
    config = config or Config()

    changes = filter_ignored(
        diff_specs(load_spec(old_spec), load_spec(new_spec)),
        config.ignore_paths,
        config.ignore_fields,
    )
    scan = scan_frontend(frontend_src, config.client_names)
    findings = match_changes(changes, scan.sites, config.min_confidence)
    return Report(changes=changes, findings=findings, unanalyzed=scan.unanalyzed)
