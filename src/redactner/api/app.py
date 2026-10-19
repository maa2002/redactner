"""Local FastAPI service: upload → analyse → review → redact → download.

Privacy and safety by design:
* binds to 127.0.0.1 only (see ``redactner serve``); ``TrustedHostMiddleware`` blocks DNS rebinding;
* every API call needs a per-launch token header (injected into the page) and a local ``Origin``,
  so other websites open in the browser cannot drive the API (CSRF);
* uploads are read from the raw request body under a byte cap (never spooled to disk) and kept
  in an in-memory store with a TTL; responses are ``Cache-Control: no-store``;
* strict Content-Security-Policy; validation errors never echo the request back.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..docx_engine import DocxDocument, DocxError, DocxRefused
from ..labels import DEFAULT_PRESET, PRESETS, label_info
from ..pipeline import RedactionPipeline, build_detectors

STATIC_DIR = Path(__file__).parent / "static"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TOKEN_HEADER = "X-RedactNER-Token"
CSP = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
DETECTOR_NAMES = {"hybrid": "RedactNER (Model and Rules)", "model": "Model Only", "rules": "Rules Only"}


# -- request models ------------------------------------------------------------------------------
def to_tau(min_confidence: float | None) -> float | None:
    """The UI/API speak in *minimum confidence* (1 − P(O)); the decoder uses τ on P(O): tag if P(O) < τ."""
    return None if min_confidence is None else round(1.0 - min_confidence, 4)


class AnalyzeRequest(BaseModel):
    detector: str | None = None
    labels: list[str] | None = None
    threshold: float | None = Field(default=None, ge=0.01, le=0.99, description="minimum model confidence")
    deny_terms: list[str] = Field(default_factory=list, max_length=200)
    allow_terms: list[str] = Field(default_factory=list, max_length=200)


class ExtraSpan(BaseModel):
    segment_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    label: str


class RedactRequest(BaseModel):
    mode: str = Field(default="label", pattern="^(label|mask)$")
    exclude_ids: list[str] = Field(default_factory=list)
    extra_spans: list[ExtraSpan] = Field(default_factory=list)


class TextRequest(BaseModel):
    text: str = Field(max_length=200_000)
    detector: str | None = None
    labels: list[str] | None = None
    threshold: float | None = Field(default=None, ge=0.01, le=0.99)


# -- in-memory document store --------------------------------------------------------------------
@dataclass
class Entry:
    data: bytes
    filename: str
    doc: DocxDocument
    created: float = field(default_factory=time.time)
    analysis: dict | None = None
    redacted: bytes | None = None
    report: dict | None = None

    @property
    def size(self) -> int:
        return len(self.data) * 12 + len(self.redacted or b"")  # parsed XML trees ≈ 10× the zip


class DocumentStore:
    def __init__(self, ttl_seconds: int = 900, budget_bytes: int = 600 * 2**20) -> None:
        self.ttl = ttl_seconds
        self.budget = budget_bytes
        self._items: dict[str, Entry] = {}
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, v in self._items.items() if now - v.created > self.ttl]:
            del self._items[key]

    def put(self, entry: Entry) -> str:
        with self._lock:
            self._purge()
            while self._items and sum(e.size for e in self._items.values()) + entry.size > self.budget:
                del self._items[min(self._items, key=lambda k: self._items[k].created)]
            doc_id = uuid.uuid4().hex
            self._items[doc_id] = entry
            return doc_id

    def get(self, doc_id: str) -> Entry:
        with self._lock:
            self._purge()
            if doc_id not in self._items:
                raise HTTPException(404, "Document not found or expired.")
            return self._items[doc_id]

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            return self._items.pop(doc_id, None) is not None


# -- application factory -------------------------------------------------------------------------
def create_app(model_dir: str | Path | None = None, device: str | None = None, max_upload_mb: int = 20,
               port: int = 8000) -> FastAPI:
    token = os.environ.get("REDACTNER_TOKEN") or secrets.token_urlsafe(24)
    detectors = build_detectors(model_dir, device)
    pipeline = RedactionPipeline(detectors)
    store = DocumentStore()
    infer_lock = threading.Lock()  # one GPU model → serialise inference
    max_bytes = max_upload_mb * 2**20
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    model_info = {"name": None, "device": "cpu"}
    if "hybrid" in detectors:
        m = detectors["hybrid"].model
        model_info = {"name": m.name, "device": m.device.type, "threshold": m.threshold}

    app = FastAPI(title="RedactNER", version="0.1.0", redoc_url=None,
                  description="Local PII redaction for Word documents. All endpoints need the "
                              f"`{TOKEN_HEADER}` header printed by `redactner serve`.")
    app.state.token = token
    app.state.pipeline = pipeline
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    api_key = APIKeyHeader(name=TOKEN_HEADER, auto_error=False)

    def require_token(request: Request, key: str | None = Depends(api_key)) -> None:
        if not key or not secrets.compare_digest(key, token):
            raise HTTPException(401, "Missing or invalid API token.")
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            raise HTTPException(403, "Cross-origin requests are not allowed.")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith(("/docs", "/openapi.json")):
            response.headers["Content-Security-Policy"] = CSP
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        fields = sorted({".".join(str(x) for x in err.get("loc", [])[1:]) for err in exc.errors()})
        return JSONResponse({"detail": f"Invalid request fields: {', '.join(fields) or 'body'}."}, status_code=422)

    # -- page -------------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__REDACTNER_TOKEN__", token))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- API --------------------------------------------------------------------------------
    @app.get("/api/v1/health")
    def health() -> dict:
        return {"status": "ok", "model": model_info, "detectors": sorted(detectors)}

    @app.get("/api/v1/config", dependencies=[Depends(require_token)])
    def config() -> dict:
        default_threshold = round(1.0 - model_info.get("threshold", 0.5), 2)  # as minimum confidence
        return {
            "labels": label_info(),  # deny-list terms come back as CUSTOM and are always applied
            "presets": PRESETS, "default_preset": DEFAULT_PRESET,
            "modes": [{"id": "label", "name": "Labels ([PERSON_1])"}, {"id": "mask", "name": "Black-Out (█████)"}],
            "detectors": [{"id": d, "name": DETECTOR_NAMES[d]} for d in ("hybrid", "model", "rules") if d in detectors],
            "default_detector": pipeline.default_detector, "default_threshold": default_threshold,
            "max_upload_mb": max_upload_mb, "model": model_info,
        }

    async def read_docx_body(request: Request) -> bytes:
        size, chunks = 0, []
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(413, f"File is larger than {max_upload_mb} MB.")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise HTTPException(400, "Empty upload. Send the .docx file as the request body.")
        return data

    def load_doc(data: bytes) -> DocxDocument:
        try:
            return DocxDocument.from_bytes(data, max_bytes=max_bytes)
        except DocxRefused as exc:
            raise HTTPException(415, f"Document refused: {exc}.") from exc
        except DocxError as exc:
            raise HTTPException(422, f"Not a readable .docx file: {exc}.") from exc

    @app.post("/api/v1/documents", dependencies=[Depends(require_token)])
    async def upload(request: Request) -> dict:
        data = await read_docx_body(request)
        doc = await run_in_threadpool(load_doc, data)
        filename = unquote(request.headers.get("x-filename", "document.docx"))[:200]
        doc_id = store.put(Entry(data=data, filename=filename, doc=doc))
        return {"doc_id": doc_id, "filename": filename, "stats": doc.stats, "warnings": doc.warnings}

    def check_detector(name: str | None) -> str:
        name = name or pipeline.default_detector
        if name not in detectors:
            raise HTTPException(400, f"Detector '{name}' is not available.")
        return name

    @app.post("/api/v1/documents/{doc_id}/analyze", dependencies=[Depends(require_token)])
    async def analyze(doc_id: str, req: AnalyzeRequest) -> dict:
        entry = store.get(doc_id)
        detector = check_detector(req.detector)

        def work() -> dict:
            with infer_lock:
                return pipeline.analyze(entry.doc, detector=detector, labels=req.labels, threshold=to_tau(req.threshold),
                                        deny_terms=req.deny_terms, allow_terms=req.allow_terms)

        entry.analysis = await run_in_threadpool(work)
        entry.redacted = entry.report = None
        return entry.analysis

    @app.post("/api/v1/documents/{doc_id}/redact", dependencies=[Depends(require_token)])
    async def redact(doc_id: str, req: RedactRequest) -> dict:
        entry = store.get(doc_id)
        if entry.analysis is None:
            raise HTTPException(409, "Analyse the document first.")
        extra = [s.model_dump() for s in req.extra_spans]
        data, report = await run_in_threadpool(pipeline.redact, entry.doc, entry.analysis["entities"],
                                               req.mode, req.exclude_ids, extra)
        report["warnings"] = sorted(set(report.get("warnings", [])) | set(entry.doc.warnings))
        entry.redacted, entry.report = data, report
        return {"report": report, "download_url": f"/api/v1/documents/{doc_id}/download"}

    @app.get("/api/v1/documents/{doc_id}/download", dependencies=[Depends(require_token)])
    def download(doc_id: str) -> Response:
        entry = store.get(doc_id)
        if entry.redacted is None:
            raise HTTPException(409, "Nothing has been redacted yet.")
        return Response(entry.redacted, media_type=DOCX_MIME,
                        headers={"Content-Disposition": 'attachment; filename="redacted.docx"'})

    @app.get("/api/v1/documents/{doc_id}/report", dependencies=[Depends(require_token)])
    def report(doc_id: str) -> dict:
        entry = store.get(doc_id)
        if entry.report is None:
            raise HTTPException(409, "Nothing has been redacted yet.")
        return entry.report

    @app.delete("/api/v1/documents/{doc_id}", dependencies=[Depends(require_token)])
    def delete(doc_id: str) -> dict:
        return {"deleted": store.delete(doc_id)}

    @app.post("/api/v1/redact", dependencies=[Depends(require_token)])
    async def redact_one_shot(request: Request, mode: str = "label", preset: str = DEFAULT_PRESET,
                              detector: str | None = None, threshold: float | None = None) -> Response:
        if mode not in ("label", "mask") or preset not in PRESETS:
            raise HTTPException(400, "Invalid mode or preset.")
        data = await read_docx_body(request)
        det = check_detector(detector)

        def work() -> tuple[bytes, dict]:
            load_doc(data)  # validation with proper HTTP errors
            with infer_lock:
                return pipeline.redact_bytes(data, mode=mode, detector=det, labels=PRESETS[preset],
                                             threshold=to_tau(threshold))

        out, rep = await run_in_threadpool(work)
        return Response(out, media_type=DOCX_MIME,
                        headers={"Content-Disposition": 'attachment; filename="redacted.docx"',
                                 "X-Redactions": str(sum(rep.get("counts_by_label", {}).values()))})

    @app.post("/api/v1/analyze-text", dependencies=[Depends(require_token)])
    async def analyze_text(req: TextRequest) -> dict:
        det = check_detector(req.detector)

        def work():
            with infer_lock:
                return pipeline.detect_text(req.text, detector=det, labels=req.labels, threshold=to_tau(req.threshold))

        spans = await run_in_threadpool(work)
        return {"entities": [{**sp.to_dict(), "text": req.text[sp.start:sp.end]} for sp in spans]}

    return app
