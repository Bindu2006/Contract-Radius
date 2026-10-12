"""Render a Report as text, JSON, Markdown, or a GitHub PR comment. ASCII-only output."""

from __future__ import annotations

import json
from enum import StrEnum

from contractradius.models import Report

_HEADERS = ["ENDPOINT", "FIELD", "CHANGE", "CONFIDENCE", "LOCATION"]


class OutputFormat(StrEnum):
    text = "text"
    json = "json"
    markdown = "markdown"
    github = "github"


def _rows(report: Report) -> list[list[str]]:
    rows = []
    for finding in report.findings:
        for ev in finding.evidence:
            rows.append([
                finding.change.endpoint,
                finding.change.field or "-",
                finding.change.kind.value,
                f"{ev.confidence:.2f}",
                f"{ev.file}:{ev.line}",
            ])
    return rows


def _summary_lines(report: Report) -> list[str]:
    return [
        f"Total API changes: {report.total_changes}",
        f"Potentially breaking: {report.potentially_breaking}",
        f"Frontend-impacting: {report.frontend_impacting}",
    ]


def _unanalyzed_lines(report: Report, limit: int = 10) -> list[str]:
    if not report.unanalyzed:
        return []
    lines = [f"Could not analyze {len(report.unanalyzed)} call(s) (URL not a static string):"]
    lines += [f"  {u.file}:{u.line}  {u.reason}" for u in report.unanalyzed[:limit]]
    if len(report.unanalyzed) > limit:
        lines.append(f"  ... and {len(report.unanalyzed) - limit} more")
    return lines


def render_text(report: Report) -> str:
    out: list[str] = []
    rows = _rows(report)
    if rows:
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(_HEADERS)]
        fmt = "   ".join(f"{{:<{w}}}" for w in widths)
        out.append(fmt.format(*_HEADERS).rstrip())
        out.append("")
        out += [fmt.format(*row).rstrip() for row in rows]
        out.append("")
    else:
        out += ["No frontend-impacting API changes found.", ""]
    out += _summary_lines(report)
    extra = _unanalyzed_lines(report)
    if extra:
        out += ["", *extra]
    return "\n".join(out)


def _markdown_table(report: Report) -> list[str]:
    lines = ["| " + " | ".join(h.title() for h in _HEADERS) + " |",
             "|" + "|".join("---" for _ in _HEADERS) + "|"]
    for row in _rows(report):
        lines.append("| " + " | ".join(f"`{c}`" if i != 3 else c for i, c in enumerate(row)) + " |")
    return lines


def render_markdown(report: Report) -> str:
    lines = ["# ContractRadius report", ""]
    if report.findings:
        lines += _markdown_table(report) + [""]
    else:
        lines += ["No frontend-impacting API changes found.", ""]
    lines += [f"- {line}" for line in _summary_lines(report)]
    extra = _unanalyzed_lines(report)
    if extra:
        lines += ["", *extra]
    return "\n".join(lines)


def render_github(report: Report) -> str:
    """Body for a pull-request comment."""
    if report.real_breaks:
        verdict = f"**{len(report.real_breaks)} breaking API change(s) hit frontend code.**"
    elif report.findings:
        verdict = "**API changes may affect frontend code (no definite breaks).**"
    else:
        verdict = "**No frontend-impacting API changes found.**"
    lines = ["### ContractRadius", "", verdict, ""]
    if report.findings:
        lines += _markdown_table(report) + [""]
    lines += [f"<sub>{' | '.join(_summary_lines(report))}</sub>"]
    return "\n".join(lines)


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2)


def render(report: Report, fmt: OutputFormat | str = OutputFormat.text) -> str:
    fmt = OutputFormat(fmt)
    return {
        OutputFormat.text: render_text,
        OutputFormat.json: render_json,
        OutputFormat.markdown: render_markdown,
        OutputFormat.github: render_github,
    }[fmt](report)
