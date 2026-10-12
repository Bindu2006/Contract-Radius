"""Command line interface: `contractradius diff OLD NEW --frontend ./src`."""

from pathlib import Path
from typing import Annotated, Optional

import typer

from contractradius import __version__
from contractradius.analyzer import analyze
from contractradius.config import load_config
from contractradius.exceptions import ContractRadiusError
from contractradius.report import OutputFormat, render

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find API changes that have evidence of affecting your frontend code.",
)


@app.command()
def diff(
    old_spec: Annotated[Path, typer.Argument(exists=True, dir_okay=False,
                                             help="Old (before) OpenAPI spec, JSON or YAML.")],
    new_spec: Annotated[Path, typer.Argument(exists=True, dir_okay=False,
                                             help="New (after) OpenAPI spec, JSON or YAML.")],
    frontend: Annotated[Path, typer.Option("--frontend", "-f", exists=True, file_okay=False,
                                           help="Frontend source directory (.ts/.tsx).")],
    output_format: Annotated[OutputFormat, typer.Option("--format",
                                                        help="Output format.")] = OutputFormat.text,
    config: Annotated[Optional[Path], typer.Option(  # noqa: UP045 - typer-friendly
        "--config", exists=True, dir_okay=False,
        help="Path to a .contractradius.yaml (default: ./.contractradius.yaml).")] = None,
    min_confidence: Annotated[Optional[float], typer.Option(  # noqa: UP045
        min=0.0, max=1.0, help="Hide findings below this confidence.")] = None,
    fail_ci: Annotated[bool, typer.Option(
        "--fail-ci/--no-fail-ci",
        help="Exit 1 when findings match the configured fail_on level.")] = True,
) -> None:
    """Compare OLD_SPEC and NEW_SPEC and report changes your frontend code appears to use."""
    try:
        cfg = load_config(config)
        if min_confidence is not None:
            cfg = cfg.model_copy(update={"min_confidence": min_confidence})
        report = analyze(old_spec, new_spec, frontend, config=cfg)
    except ContractRadiusError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    typer.echo(render(report, output_format))
    if fail_ci and report.should_fail(cfg.fail_on):
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)
