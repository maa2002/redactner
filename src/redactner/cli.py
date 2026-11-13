"""Command-line interface: ``redactner desktop | redact | analyze``."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .labels import DEFAULT_PRESET, PRESETS
from .paths import default_model_dir

DEFAULT_MODEL_DIR = default_model_dir()

MODES = ("label", "mask")

# pretty_exceptions_show_locals=False: a crash must never print local variables that may hold document text
app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False,
                  help="RedactNER: local PII redaction for Word documents.")


def _fail(message: str) -> typer.Exit:
    """Print a one-line error and return the exit (code 2: wrong usage) for the caller to raise."""
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(2)


def _check_options(preset: str, mode: str | None = None, threshold: float | None = None) -> None:
    """Validate option values before the (slow) model load."""
    if mode is not None and mode not in MODES:
        raise _fail(f"--mode must be one of: {', '.join(MODES)} (got {mode!r}).")
    if preset not in PRESETS:
        raise _fail(f"--preset must be one of: {', '.join(PRESETS)} (got {preset!r}).")
    if threshold is not None and not 0 < threshold < 1:
        raise _fail(f"--threshold must be greater than 0 and less than 1 (got {threshold}).")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    except OSError:
        return False


def _pipeline(model_dir: Path, device: str | None):
    from .pipeline import ModelNotFoundError, RedactionPipeline, build_detector

    try:
        return RedactionPipeline(build_detector(model_dir, device))
    except ModelNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


@app.command()
def desktop(model_dir: Path = typer.Option(DEFAULT_MODEL_DIR, help="Fine-tuned checkpoint directory."),
            device: str = typer.Option(None, help="Compute device: cuda or cpu (detected automatically by default).")) -> None:
    """Open the review window (needs the desktop extra: uv sync --extra desktop)."""
    from .desktop import main

    main(["--model-dir", str(model_dir), *(["--device", device] if device else [])])


@app.command()
def redact(input: Path = typer.Argument(..., exists=True, dir_okay=False, help="Word document to redact."),
           output: Path = typer.Option(None, "-o", "--output", help="Output path (defaults to <input>_redacted.docx)."),
           mode: str = typer.Option("label", help="Replacement style: label or mask."),
           preset: str = typer.Option(DEFAULT_PRESET, help="Category preset: strict or direct."),
           threshold: float = typer.Option(None, help="Override the tuned decision threshold, between 0 and 1 "
                                                      "(tag when P(O) < threshold)."),
           model_dir: Path = typer.Option(DEFAULT_MODEL_DIR), device: str = None,
           report: Path = typer.Option(None, help="Write the JSON audit report to this path."),
           force: bool = typer.Option(False, "--force", help="Overwrite the output file if it already exists.")) -> None:
    """Redact a Word document and save the result."""
    _check_options(preset, mode, threshold)
    output = output or input.with_name(f"{input.stem}_redacted.docx")
    if _same_file(output, input):
        raise _fail("the output path is the input document; choose a different --output.")
    if report and (_same_file(report, input) or _same_file(report, output)):
        raise _fail("the report path must differ from the input and output paths.")
    if output.exists() and not force:
        raise _fail(f"{output} already exists; use --force to overwrite it.")
    pipeline = _pipeline(model_dir, device)
    data, rep = pipeline.redact_bytes(input.read_bytes(), mode=mode, labels=PRESETS[preset], threshold=threshold)
    output.write_bytes(data)
    if report:
        report.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    counts = ", ".join(f"{k} {v}" for k, v in sorted(rep.get("counts_by_label", {}).items())) or "none"
    typer.echo(f"Saved {output} | Redactions: {counts} | Safety-net fixes: {rep.get('residual_fixes', 0)}")
    for warning in rep.get("warnings", []):  # e.g. images were not analysed: shown even without --report
        typer.secho(f"Warning: {warning}", fg=typer.colors.YELLOW, err=True)


@app.command()
def analyze(input: Path = typer.Argument(..., exists=True, dir_okay=False),
            preset: str = typer.Option(DEFAULT_PRESET),
            model_dir: Path = typer.Option(DEFAULT_MODEL_DIR), device: str = None) -> None:
    """Print the detected entities of a Word document as JSON without redacting it."""
    from .docx_engine import DocxDocument

    _check_options(preset)
    pipeline = _pipeline(model_dir, device)
    doc = DocxDocument.from_bytes(input.read_bytes())
    result = pipeline.analyze(doc, labels=PRESETS[preset])
    typer.echo(json.dumps({"entities": result["entities"], "summary": result["summary"]}, indent=2,
                          ensure_ascii=False))


if __name__ == "__main__":
    app()
