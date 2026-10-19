"""Command-line interface: ``redactner serve | redact | analyze``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from .labels import DEFAULT_PRESET, PRESETS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_DIR = Path(os.environ.get("REDACTNER_MODEL_DIR", PROJECT_ROOT / "models" / "redactner-best"))

app = typer.Typer(add_completion=False, help="RedactNER: local PII redaction for Word documents.")


def _pipeline(model_dir: Path, device: str | None):
    from .pipeline import RedactionPipeline, build_detectors

    if not model_dir.exists():
        typer.secho(f"Model not found at {model_dir}; using rule-based detection only.", fg=typer.colors.YELLOW, err=True)
    return RedactionPipeline(build_detectors(model_dir, device))


@app.command()
def serve(model_dir: Path = typer.Option(DEFAULT_MODEL_DIR, help="Fine-tuned checkpoint directory."),
          port: int = 8000, device: str = typer.Option(None, help="Compute device: cuda or cpu (detected automatically by default).")) -> None:
    """Start the local web application on http://127.0.0.1:PORT."""
    import uvicorn

    from .api import create_app

    web = create_app(model_dir=model_dir, device=device, port=port)
    typer.secho(f"RedactNER is running at http://127.0.0.1:{port} (API token: {web.state.token})", fg=typer.colors.GREEN)
    uvicorn.run(web, host="127.0.0.1", port=port, log_level="warning", workers=1)


@app.command()
def redact(input: Path = typer.Argument(..., exists=True, dir_okay=False, help="Word document to redact."),
           output: Path = typer.Option(None, "-o", "--output", help="Output path (defaults to <input>_redacted.docx)."),
           mode: str = typer.Option("label", help="Replacement style: label or mask."),
           preset: str = typer.Option(DEFAULT_PRESET, help="Category preset: strict or direct."),
           detector: str = typer.Option(None, help="Detection method: hybrid, model or rules."),
           threshold: float = typer.Option(None, help="Override the model decision threshold."),
           model_dir: Path = typer.Option(DEFAULT_MODEL_DIR), device: str = None,
           report: Path = typer.Option(None, help="Write the JSON audit report to this path.")) -> None:
    """Redact a Word document and save the result."""
    pipeline = _pipeline(model_dir, device)
    data, rep = pipeline.redact_bytes(input.read_bytes(), mode=mode, detector=detector, labels=PRESETS[preset],
                                      threshold=threshold)
    output = output or input.with_name(f"{input.stem}_redacted.docx")
    output.write_bytes(data)
    if report:
        report.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    counts = ", ".join(f"{k} {v}" for k, v in sorted(rep.get("counts_by_label", {}).items())) or "none"
    typer.echo(f"Saved {output} | Redactions: {counts} | Safety-net fixes: {rep.get('residual_fixes', 0)}")


@app.command()
def analyze(input: Path = typer.Argument(..., exists=True, dir_okay=False),
            detector: str = typer.Option(None), preset: str = typer.Option(DEFAULT_PRESET),
            model_dir: Path = typer.Option(DEFAULT_MODEL_DIR), device: str = None) -> None:
    """Print the detected entities of a Word document as JSON without redacting it."""
    from .docx_engine import DocxDocument

    pipeline = _pipeline(model_dir, device)
    doc = DocxDocument.from_bytes(input.read_bytes())
    result = pipeline.analyze(doc, detector=detector, labels=PRESETS[preset])
    typer.echo(json.dumps({"entities": result["entities"], "summary": result["summary"]}, indent=2,
                          ensure_ascii=False))


if __name__ == "__main__":
    app()
