"""Safe, format-preserving reading and redaction of Word ``.docx`` packages.

:meth:`DocxDocument.from_bytes` opens a package defensively (size and entry
caps, no DTDs, no macros), accepts tracked changes and exposes the visible text
as one :class:`Segment` per paragraph, in reading order.

:meth:`DocxDocument.redact` rewrites span replacements in place -- across run
boundaries, so characters outside the spans keep their run formatting --
unlinks sensitive hyperlinks, scrubs field codes and metadata, and finally runs
a residual verifier that sweeps *every* XML part (headers, comments, docProps,
customXml, relationship targets...) for any remaining copy of a redacted value.

:func:`extract_text_raw` and :func:`find_residuals` are independent leak checks
over all XML parts of a package.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from io import BytesIO
from typing import Any
from urllib.parse import unquote

from lxml import etree

__all__ = [
    "DocxDocument",
    "DocxError",
    "DocxRefused",
    "Segment",
    "extract_text_raw",
    "find_residuals",
]

Element = etree._Element
Edit = tuple[int, int, str]  # (start, end, replacement) in paragraph-local offsets
Mark = dict[str, int | bool]  # {"s", "e", "b", "i", "u"}: formatting range in segment text
Marks = list[Mark]  # alias: Segment.list shadows the builtin when hints are resolved
TablePosition = dict[str, int]  # {"t", "r", "c", "rows", "cols"}

MAX_BYTES = 20 * 2**20
MAX_UNCOMPRESSED = 200 * 2**20
MAX_ENTRIES = 3000

OBJECT_CHAR = "\N{OBJECT REPLACEMENT CHARACTER}"  # drawings, symbols, embedded objects
REDACTED = "[REDACTED]"  # replacement used by the residual verifier
REDACTED_URL = "https://redacted.invalid/"

# --------------------------------------------------------------------------- #
# Namespaces and tag names
# --------------------------------------------------------------------------- #
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
DC_NS = "http://purl.org/dc/elements/1.1/"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
EP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
XML_NS = "http://www.w3.org/XML/1998/namespace"
STRICT_NS_PREFIX = "http://purl.oclc.org/ooxml/"


def _w(local: str) -> str:
    return f"{{{W_NS}}}{local}"


W_P, W_R, W_T = _w("p"), _w("r"), _w("t")
W_TBL, W_TR, W_TC, W_TRPR, W_RPR = _w("tbl"), _w("tr"), _w("tc"), _w("trPr"), _w("rPr")
W_PPR, W_PSTYLE, W_JC, W_VAL = _w("pPr"), _w("pStyle"), _w("jc"), _w("val")
W_NUMPR, W_NUMID, W_B, W_I, W_U = _w("numPr"), _w("numId"), _w("b"), _w("i"), _w("u")
W_TXBX = _w("txbxContent")
W_HYPERLINK, W_FLDSIMPLE, W_INSTRTEXT = _w("hyperlink"), _w("fldSimple"), _w("instrText")
W_INS, W_DEL, W_MOVE_FROM, W_MOVE_TO = _w("ins"), _w("del"), _w("moveFrom"), _w("moveTo")
W_ALTCHUNK, W_FONT = _w("altChunk"), _w("font")
W_COMMENT, W_FOOTNOTE, W_ENDNOTE = _w("comment"), _w("footnote"), _w("endnote")
W_AUTHOR, W_INITIALS, W_TOOLTIP = _w("author"), _w("initials"), _w("tooltip")
W_INSTR, W_TYPE = _w("instr"), _w("type")
R_ID = f"{{{R_NS}}}id"
MC_ALTERNATE, MC_CHOICE, MC_FALLBACK = (
    f"{{{MC_NS}}}{local}" for local in ("AlternateContent", "Choice", "Fallback")
)
W15_AUTHOR, W15_PRESENCE = f"{{{W15_NS}}}author", f"{{{W15_NS}}}presenceInfo"
REL = f"{{{PKG_REL_NS}}}Relationship"
CT_DEFAULT, CT_OVERRIDE = f"{{{CT_NS}}}Default", f"{{{CT_NS}}}Override"
XML_SPACE = f"{{{XML_NS}}}space"

# Paragraph walker: run content that maps to characters, and containers to descend
# into. Everything else -- w:pPr, w:rPr, w:sdtPr, w:instrText, w:delText, w:fldChar,
# w:softHyphen (zero-width), nested w:txbxContent (own paragraphs) -- is skipped.
_CHAR_PIECES: dict[str, str] = {
    _w("tab"): "\t",
    _w("ptab"): "\t",
    _w("br"): "\n",
    _w("cr"): "\n",
    _w("noBreakHyphen"): "-",
    _w("sym"): OBJECT_CHAR,
    _w("drawing"): OBJECT_CHAR,
    _w("pict"): OBJECT_CHAR,
    _w("object"): OBJECT_CHAR,
}
_CONTAINERS = frozenset(
    _w(local)
    for local in (
        "r", "hyperlink", "smartTag", "customXml", "sdt", "sdtContent",
        "fldSimple", "dir", "bdo", "ins", "moveTo",
    )
)

# Tracked-change markup.
_PROPERTY_CHANGES = frozenset(
    _w(local)
    for local in (
        "rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "trPrChange",
        "tcPrChange", "tblPrExChange", "tblGridChange", "numberingChange",
    )
)
_RANGE_MARKERS = frozenset(
    _w(f"{kind}Range{edge}")
    for kind in ("moveFrom", "moveTo", "customXmlIns", "customXmlDel",
                 "customXmlMoveFrom", "customXmlMoveTo")
    for edge in ("Start", "End")
)
_CELL_MARKERS = frozenset(_w(local) for local in ("cellIns", "cellDel", "cellMerge"))

# Text-bearing parts: content-type suffix -> segment location, plus a name fallback.
_TEXT_PART_TYPES: dict[str, str] = {
    "wordprocessingml.document.main+xml": "body",
    "wordprocessingml.template.main+xml": "body",
    "wordprocessingml.header+xml": "header",
    "wordprocessingml.footer+xml": "footer",
    "wordprocessingml.footnotes+xml": "footnote",
    "wordprocessingml.endnotes+xml": "endnote",
    "wordprocessingml.comments+xml": "comment",
}
_TEXT_PART_NAMES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"word/document\.xml"), "body"),
    (re.compile(r"word/header\d*\.xml"), "header"),
    (re.compile(r"word/footer\d*\.xml"), "footer"),
    (re.compile(r"word/footnotes\.xml"), "footnote"),
    (re.compile(r"word/endnotes\.xml"), "endnote"),
    (re.compile(r"word/comments\.xml"), "comment"),
]
_READING_ORDER = {"header": 0, "body": 1, "footnote": 2, "endnote": 3, "footer": 4, "comment": 5}

# Layout hints: w:jc values -> UI alignment (left/start -> None), and ST_OnOff "off".
_ALIGNMENTS = {
    "center": "center", "right": "right", "end": "right", "both": "both",
    "distribute": "both", "lowKashida": "both", "mediumKashida": "both",
    "highKashida": "both", "thaiDistribute": "both",
}
_OFF_VALUES = frozenset({"0", "false", "off", "none"})

# Attributes that are identifiers or font names and must never be rewritten, and
# attribute values that look like schema tokens (enums such as "page", numbers,
# hex colours, measurements) -- rewriting those would corrupt the document.
_PROTECTED_ATTRIBUTES = frozenset(
    {"paraId", "textId", "durableId", "ascii", "hAnsi", "cs", "eastAsia", "typeface", "panose"}
)
_SCHEMA_TOKEN = re.compile(
    r"[a-z][A-Za-z0-9]*|[-+]?(?=[0-9A-Fa-f.]*\d)[0-9A-Fa-f.]+(?:%|pt|in|cm|mm|pc|pi|em|px)?"
)

_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
_SEPARATOR_CLASS = r"[\s\-\N{HYPHEN}-\N{HORIZONTAL BAR}.()]"  # spaces, dashes, dots, parens
_SEPARATORS = re.compile(_SEPARATOR_CLASS)
_URL_PUNCTUATION = re.compile(r"[-_.+/=&?#:%~]+")


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #
class DocxError(Exception):
    """The file could not be processed (corrupt zip, malformed XML...)."""


class DocxRefused(DocxError):
    """The file was refused by policy (encrypted, macros, Strict OOXML, limits...)."""


@dataclass(frozen=True, slots=True)
class Segment:
    """The visible text of one paragraph (``w:p``), tracked changes accepted.

    The optional fields are rendering hints for the UI, read from *direct*
    formatting only (style-inherited formatting is not resolved).
    """

    id: str
    part: str
    location: str
    text: str
    style: str | None = None  # w:pStyle id, e.g. "Title", "Heading1"
    align: str | None = None  # "center" | "right" | "both"; None = left/start
    # Declared before ``list``, which shadows the builtin inside this class body.
    marks: Marks = field(default_factory=list, hash=False)  # bold/italic/underline ranges
    list: bool = False  # paragraph has w:numPr (numId 0 = numbering removed)
    table: TablePosition | None = field(default=None, hash=False)  # innermost table cell
    hidden: bool = False  # duplicate legacy copy of a text box (mc:Fallback)
    author: str | None = None  # original w:comment/@w:author for comment segments

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict of all fields."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# Package reading
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Package:
    """Raw zip entries (in original order) plus the content-type map."""

    infos: list[zipfile.ZipInfo]
    raw: dict[str, bytes]
    defaults: dict[str, str] = field(default_factory=dict)  # extension -> content type
    overrides: dict[str, str] = field(default_factory=dict)  # lower-case part name -> type
    names: dict[str, str] = field(default_factory=dict)  # lower-case part name -> entry name

    def find(self, name: str) -> str | None:
        """Entry name for a part name, case-insensitively (OPC names are)."""
        return self.names.get(posixpath.normpath(name.lstrip("/")).lower())

    def content_type(self, name: str) -> str:
        extension = posixpath.splitext(name)[1].lstrip(".").lower()
        return self.overrides.get(name.lower()) or self.defaults.get(extension, "")

    @property
    def xml_parts(self) -> list[str]:
        """Every XML entry (images such as SVG are excluded)."""
        parts = []
        for name in self.raw:
            if name.endswith("/"):
                continue
            ctype = self.content_type(name)
            if ctype.startswith("image/"):
                continue
            if name.lower().endswith((".xml", ".rels")) or ctype.endswith("xml"):
                parts.append(name)
        return parts


def _read_package(
    data: bytes,
    *,
    max_bytes: int = MAX_BYTES,
    max_uncompressed: int = MAX_UNCOMPRESSED,
    max_entries: int = MAX_ENTRIES,
) -> _Package:
    """Open the zip safely: size/entry caps, a running decompression cap, no encryption."""
    if len(data) > max_bytes:
        raise DocxRefused(f"file is larger than the {max_bytes:,}-byte limit")
    if data[:4] == _OLE_MAGIC:
        raise DocxRefused("encrypted or legacy .doc")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise DocxRefused("not a .docx file (not a zip archive)") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise DocxRefused(f"too many zip entries ({len(infos)} > {max_entries})")
        raw: dict[str, bytes] = {}
        total = 0
        for info in infos:
            if info.flag_bits & 0x1:
                raise DocxRefused("encrypted zip entries are not supported")
            if info.filename in raw:
                raise DocxRefused(f"duplicate zip entry {info.filename!r}")
            chunks = []
            try:
                with archive.open(info) as stream:
                    while chunk := stream.read(1 << 16):
                        total += len(chunk)
                        if total > max_uncompressed:
                            raise DocxRefused("uncompressed size limit exceeded")
                        chunks.append(chunk)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
                raise DocxError(f"corrupt zip entry {info.filename!r}: {exc}") from exc
            raw[info.filename] = b"".join(chunks)

    package = _Package(infos, raw, names={n.lower(): n for n in raw})
    types_name = package.find("[Content_Types].xml")
    if types_name is None:
        raise DocxRefused("not an Office Open XML package ([Content_Types].xml missing)")
    types = _parse_xml(types_name, raw[types_name])
    package.defaults = {
        el.get("Extension", "").lower(): el.get("ContentType", "") for el in types.iter(CT_DEFAULT)
    }
    package.overrides = {
        el.get("PartName", "").lstrip("/").lower(): el.get("ContentType", "")
        for el in types.iter(CT_OVERRIDE)
    }
    return package


def _parse_xml(name: str, raw: bytes) -> Element:
    """Parse one part with a hardened parser; DOCTYPEs are refused outright."""
    if _DOCTYPE.search(raw):
        raise DocxRefused(f"DOCTYPE declarations are not allowed ({name})")
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise DocxError(f"malformed XML in {name}: {exc}") from exc
    if root.getroottree().docinfo.doctype:
        raise DocxRefused(f"DOCTYPE declarations are not allowed ({name})")
    return root


def _parse_parts(package: _Package) -> dict[str, Element]:
    """Fresh, mutable trees for every XML part."""
    return {name: _parse_xml(name, package.raw[name]) for name in package.xml_parts}


def _check_policy(package: _Package, trees: dict[str, Element]) -> None:
    """Refuse macro-enabled, Strict OOXML and altChunk documents."""
    lowered = [name.lower() for name in package.raw]
    content_types = [*package.overrides.values(), *package.defaults.values()]
    if (
        "word/vbaproject.bin" in lowered
        or any(name.startswith("word/activex/") for name in lowered)
        or any("macroenabled" in ct.lower() or "vbaproject" in ct.lower() for ct in content_types)
    ):
        raise DocxRefused("macro-enabled documents (VBA/ActiveX) are not supported")
    for name, root in trees.items():
        if (etree.QName(root).namespace or "").startswith(STRICT_NS_PREFIX):
            raise DocxRefused("Strict Open XML is not supported; re-save as a standard .docx")
        if next(root.iter(W_ALTCHUNK), None) is not None:
            raise DocxRefused(f"embedded alternative-format content (w:altChunk) in {name}")


def _find_text_parts(package: _Package, trees: dict[str, Element]) -> list[tuple[str, str]]:
    """(part, kind) for every text-bearing part, in reading order."""
    found: list[tuple[str, str]] = []
    for name in trees:
        ctype = package.content_type(name)
        kind = next(
            (k for suffix, k in _TEXT_PART_TYPES.items() if ctype.endswith("officedocument." + suffix)),
            None,
        )
        if kind is None and name.lower() not in package.overrides:
            kind = next((k for rx, k in _TEXT_PART_NAMES if rx.fullmatch(name.lower())), None)
        if kind is not None:
            found.append((name, kind))
    return sorted(found, key=lambda item: (_READING_ORDER[item[1]], _natural_key(item[0])))


def _natural_key(name: str) -> list[int | str]:
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", name.lower())]


# --------------------------------------------------------------------------- #
# Tree helpers
# --------------------------------------------------------------------------- #
def _remove(element: Element) -> None:
    """Detach ``element`` but keep its tail text."""
    parent = element.getparent()
    if parent is None:
        return
    if element.tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + element.tail
        else:
            parent.text = (parent.text or "") + element.tail
    parent.remove(element)


def _unwrap(element: Element) -> None:
    """Replace ``element`` by its children, in place."""
    parent = element.getparent()
    if parent is None:
        return
    index = parent.index(element)
    for offset, child in enumerate(list(element)):
        parent.insert(index + offset, child)
    _remove(element)


def _ancestor(element: Element, tag: str, stop: Element | None = None) -> Element | None:
    for ancestor in element.iterancestors():
        if ancestor is stop:
            return None
        if ancestor.tag == tag:
            return ancestor
    return None


def _attached(element: Element, root: Element) -> bool:
    return any(ancestor is root for ancestor in element.iterancestors())


def _set_text(element: Element, text: str) -> None:
    element.text = text
    element.set(XML_SPACE, "preserve")


def _rels_name(part: str) -> str:
    folder, base = posixpath.split(part)
    return posixpath.join(folder, "_rels", base + ".rels")


# --------------------------------------------------------------------------- #
# Tracked changes
# --------------------------------------------------------------------------- #
def _accept_tracked_changes(root: Element) -> tuple[int, int]:
    """Accept every revision in place. Returns (revisions accepted, paragraph marks kept).

    Deletions and move sources are dropped, insertions and move targets unwrapped,
    property-change history removed. Deleted paragraph marks only lose their marker
    (paragraphs are not merged).
    """
    accepted = marks_kept = 0
    for row in list(root.iter(W_TR)):
        props = row.find(W_TRPR)
        if props is not None and props.find(W_DEL) is not None:
            _remove(row)
            accepted += 1
    removable = (W_DEL, W_MOVE_FROM, *_PROPERTY_CHANGES, *_RANGE_MARKERS, *_CELL_MARKERS)
    for element in list(root.iter(*removable)):
        if not _attached(element, root):
            continue
        if element.tag not in _RANGE_MARKERS:
            accepted += 1
            parent = element.getparent()
            if element.tag in (W_DEL, W_MOVE_FROM) and parent is not None and parent.tag == W_RPR:
                marks_kept += 1
        _remove(element)
    for element in list(root.iter(W_INS, W_MOVE_TO)):
        if not _attached(element, root):
            continue
        accepted += 1
        parent = element.getparent()
        if parent is not None and parent.tag in (W_RPR, W_TRPR):
            _remove(element)  # inserted paragraph mark / row: just drop the marker
        else:
            _unwrap(element)
    return accepted, marks_kept


# --------------------------------------------------------------------------- #
# Paragraph model: pieces and the cross-run rewrite
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Piece:
    """A stretch of paragraph text produced by one element (only ``w:t`` is editable)."""

    element: Element
    start: int
    end: int
    editable: bool


@dataclass(slots=True)
class _Paragraph:
    element: Element
    pieces: list[_Piece]
    text: str


def _read_paragraph(paragraph: Element) -> _Paragraph:
    """Map a ``w:p`` to its visible text and the pieces that produce it."""
    pieces: list[_Piece] = []
    chunks: list[str] = []
    position = 0

    def visit(parent: Element) -> None:
        nonlocal position
        for child in parent:
            tag = child.tag
            if tag == W_T:
                text = child.text or ""
                pieces.append(_Piece(child, position, position + len(text), True))
                chunks.append(text)
                position += len(text)
            elif tag in _CHAR_PIECES:
                pieces.append(_Piece(child, position, position + 1, False))
                chunks.append(_CHAR_PIECES[tag])
                position += 1
            elif tag in _CONTAINERS:
                visit(child)
            elif tag == MC_ALTERNATE:  # render one branch only: Choice, else Fallback
                branch = child.find(MC_CHOICE)
                if branch is None:
                    branch = child.find(MC_FALLBACK)
                if branch is not None:
                    visit(branch)

    visit(paragraph)
    return _Paragraph(paragraph, pieces, "".join(chunks))


def _rewrite(pieces: list[_Piece], edits: list[Edit]) -> None:
    """Apply sorted, non-overlapping edits to one paragraph, across runs.

    Each replacement goes into the first editable ``w:t`` overlapping its span; the
    span's characters are cut from every overlapping ``w:t``; tabs, breaks, symbols
    and other non-text pieces inside the span are removed. Characters outside the
    spans stay in their original ``w:t``, so run formatting is preserved and no
    ``w:r`` is ever deleted.
    """
    keep: dict[int, list[bool]] = {}
    inserts: dict[int, dict[int, str]] = {}
    doomed: list[Element] = []
    orphans: list[tuple[Element, str]] = []
    for start, end, replacement in edits:
        hits = [
            i for i, piece in enumerate(pieces)
            if piece.start < end and piece.end > start and piece.end > piece.start
        ]
        if not hits:
            continue
        for i in hits:
            piece = pieces[i]
            if not piece.editable:
                doomed.append(piece.element)
                continue
            mask = keep.setdefault(i, [True] * (piece.end - piece.start))
            for k in range(max(start, piece.start) - piece.start, min(end, piece.end) - piece.start):
                mask[k] = False
        target = next((i for i in hits if pieces[i].editable), None)
        if target is None:
            orphans.append((pieces[hits[0]].element, replacement))
        else:
            local = max(start, pieces[target].start) - pieces[target].start
            slot = inserts.setdefault(target, {})
            slot[local] = slot.get(local, "") + replacement
    for i, mask in keep.items():
        element, extra = pieces[i].element, inserts.get(i, {})
        old = element.text or ""
        _set_text(element, "".join(extra.get(k, "") + (ch if mask[k] else "") for k, ch in enumerate(old)))
    for anchor, text in orphans:
        _insert_text_before(anchor, text)
    for element in doomed:
        _remove(element)


def _insert_text_before(anchor: Element, text: str) -> None:
    """Add a new ``w:t`` in the run containing ``anchor`` (span had no text piece)."""
    run = _ancestor(anchor, W_R)
    if run is None:
        return
    child = anchor
    while child.getparent() is not run:
        child = child.getparent()
    new = etree.SubElement(run, W_T)
    _set_text(new, text)
    run.insert(run.index(child), new)


def _location(paragraph: Element, kind: str) -> str:
    if kind != "body":
        return kind
    for ancestor in paragraph.iterancestors():
        if ancestor.tag == W_TXBX:
            return "textbox"
        if ancestor.tag == W_TC:
            return "table"
    return "body"


def _normalise_edits(edits: Iterable[Edit], length: int) -> tuple[list[Edit], int]:
    """Sort, clip to the segment and merge overlaps. Returns (edits, merged count)."""
    cleaned = sorted(
        (max(0, int(s)), min(length, int(e)), str(r)) for s, e, r in edits
    )
    merged: list[Edit] = []
    overlaps = 0
    for start, end, replacement in cleaned:
        if start >= end:
            continue
        if merged and start < merged[-1][1]:
            prev_start, prev_end, prev_replacement = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_replacement)
            overlaps += 1
        else:
            merged.append((start, end, replacement))
    return merged, overlaps


# --------------------------------------------------------------------------- #
# Layout hints for the UI (direct formatting only; styles are not resolved)
# --------------------------------------------------------------------------- #
def _paragraph_format(paragraph: Element) -> tuple[str | None, str | None, bool]:
    """(style id, alignment, is list item) from the paragraph's own w:pPr."""
    props = paragraph.find(W_PPR)
    if props is None:
        return None, None, False
    style, jc, numbering = props.find(W_PSTYLE), props.find(W_JC), props.find(W_NUMPR)
    num_id = numbering.find(W_NUMID) if numbering is not None else None
    return (
        style.get(W_VAL) if style is not None else None,
        _ALIGNMENTS.get(jc.get(W_VAL, "")) if jc is not None else None,
        numbering is not None and (num_id is None or num_id.get(W_VAL) != "0"),
    )


def _switched_on(props: Element | None, tag: str) -> bool:
    """Whether a run toggle (w:b, w:i, w:u) is present and not switched off."""
    element = props.find(tag) if props is not None else None
    return element is not None and (element.get(W_VAL) or "on").lower() not in _OFF_VALUES


def _marks(paragraph: _Paragraph) -> Marks:
    """Bold/italic/underline ranges in segment offsets; adjacent equal ranges merged."""
    marks: Marks = []
    for piece in paragraph.pieces:
        if piece.end <= piece.start:
            continue
        run = _ancestor(piece.element, W_R, stop=paragraph.element)
        props = run.find(W_RPR) if run is not None else None
        flags = {flag: _switched_on(props, tag) for flag, tag in (("b", W_B), ("i", W_I), ("u", W_U))}
        if not any(flags.values()):
            continue
        last = marks[-1] if marks else None
        if last is not None and last["e"] == piece.start and all(last[k] == v for k, v in flags.items()):
            last["e"] = piece.end
        else:
            marks.append({"s": piece.start, "e": piece.end, **flags})
    return marks


def _is_fallback_copy(paragraph: Element) -> bool:
    """Inside an mc:Fallback whose mc:AlternateContent also offers an mc:Choice."""
    for fallback in paragraph.iterancestors(MC_FALLBACK):
        parent = fallback.getparent()
        if parent is not None and parent.find(MC_CHOICE) is not None:
            return True
    return False


def _comment_author(paragraph: Element) -> str | None:
    comment = _ancestor(paragraph, W_COMMENT)
    return comment.get(W_AUTHOR) if comment is not None else None


@dataclass(slots=True)
class _TableIndex:
    """Every table, row and cell of the text parts, numbered in reading order."""

    tables: dict[Element, tuple[int, int, int]] = field(default_factory=dict)  # (t, rows, cols)
    rows: dict[Element, int] = field(default_factory=dict)
    cells: dict[Element, int] = field(default_factory=dict)

    @classmethod
    def build(cls, roots: Iterable[Element]) -> _TableIndex:
        index = cls()
        for root in roots:
            for table in root.iter(W_TBL):  # nested tables get their own number
                rows = [row for row in table.iter(W_TR) if _ancestor(row, W_TBL) is table]
                widths = [0]
                for r, row in enumerate(rows):
                    index.rows[row] = r
                    cells = [cell for cell in row.iter(W_TC) if _ancestor(cell, W_TR) is row]
                    index.cells.update((cell, c) for c, cell in enumerate(cells))
                    widths.append(len(cells))
                index.tables[table] = (len(index.tables), len(rows), max(widths))
        return index

    def position(self, paragraph: Element) -> TablePosition | None:
        """Innermost cell of a paragraph; None outside tables or inside a text box."""
        for ancestor in paragraph.iterancestors(W_TC, W_TXBX):
            if ancestor.tag == W_TXBX:
                return None
            row = _ancestor(ancestor, W_TR)
            table = _ancestor(row, W_TBL) if row is not None else None
            if table not in self.tables:
                return None
            t, rows, cols = self.tables[table]
            return {"t": t, "r": self.rows[row], "c": self.cells[ancestor], "rows": rows, "cols": cols}
        return None


def _layout(paragraph: _Paragraph, kind: str, tables: _TableIndex) -> dict[str, Any]:
    """Keyword arguments for the optional :class:`Segment` rendering fields."""
    style, align, is_list = _paragraph_format(paragraph.element)
    return {
        "style": style,
        "align": align,
        "marks": _marks(paragraph),
        "list": is_list,
        "table": tables.position(paragraph.element),
        "hidden": _is_fallback_copy(paragraph.element),
        "author": _comment_author(paragraph.element) if kind == "comment" else None,
    }


# --------------------------------------------------------------------------- #
# Value matching (shared by the residual verifier and find_residuals)
# --------------------------------------------------------------------------- #
def _normalise_value(value: str) -> str | None:
    value = " ".join(value.replace(OBJECT_CHAR, " ").split())
    if len(value) < 3 or not any(ch.isalnum() for ch in value):
        return None
    return value


def _bounded(core: str, value: str, mostly_letters: bool) -> str:
    """Add digit / word boundaries so 'Ann' does not hit 'Annual' and 123 not 91234."""

    def guard(ch: str, look: str) -> str:
        if ch.isdigit():
            return f"(?{look}\\d)"
        if mostly_letters and (ch.isalnum() or ch == "_"):
            return f"(?{look}\\w)"
        return ""

    return guard(value[0], "<!") + core + guard(value[-1], "!")


@dataclass(frozen=True, slots=True)
class _ValueRule:
    """Matcher for one value, with literal keys for a cheap pre-check.

    ``key`` (longest alphanumeric token, lower-cased) occurs in every literal match;
    ``compact`` (separator-free form, values with >= 6 digits only) occurs in the
    separator-stripped text of every separator-insensitive match.
    """

    value: str
    key: str
    compact: str | None
    pattern: re.Pattern[str]

    @classmethod
    def build(cls, value: str) -> _ValueRule:
        nonspace = value.replace(" ", "")
        mostly_letters = sum(ch.isalpha() for ch in nonspace) * 2 > len(nonspace)
        literal = r"\s+".join(re.escape(token) for token in value.split(" "))
        alternatives = [_bounded(literal, value, mostly_letters)]
        compact = _strip_separators(value)
        flexible = sum(ch.isdigit() for ch in compact) >= 6
        if flexible:
            pattern = f"{_SEPARATOR_CLASS}*".join(re.escape(ch) for ch in compact)
            alternatives.append(_bounded(pattern, compact, mostly_letters))
        key = max(re.findall(r"\w+", value.lower()), key=len, default=value.lower())
        return cls(
            value,
            key,
            compact.lower() if flexible else None,
            re.compile("|".join(alternatives), re.IGNORECASE),
        )

    @property
    def short_number(self) -> bool:
        """No letters and < 6 digits (postcode, year...): visible text only."""
        return not any(ch.isalpha() for ch in self.value) and self.compact is None

    def may_occur(self, lower: str, stripped: str) -> bool:
        """Necessary condition for a match in a text (given lower-cased and stripped forms)."""
        return self.key in lower or (self.compact is not None and self.compact in stripped)


def _strip_separators(text: str) -> str:
    return _SEPARATORS.sub("", text)


def _value_key(value: str) -> str | None:
    """Identity of a value under the matching rules: case-insensitive, and
    separator-free (digits only, for phone-like numbers) when it has >= 6 digits."""
    normal = _normalise_value(value)
    if normal is None:
        return None
    compact = _strip_separators(normal).lower()
    return compact if sum(ch.isdigit() for ch in compact) >= 6 else normal.lower()


class _ValueMatcher:
    """Case-insensitive finder for a set of redacted values (leftmost-longest wins).

    ``protected`` (optional) matches kept values: hits lying entirely inside an
    occurrence of a kept value are ignored.
    """

    def __init__(
        self,
        values: Iterable[str] = (),
        *,
        rules: list[_ValueRule] | None = None,
        protected: _ValueMatcher | None = None,
    ) -> None:
        if rules is None:
            unique = sorted({v for v in map(_normalise_value, values) if v})
            rules = [_ValueRule.build(v) for v in unique]
        self.rules = rules
        self.protected = protected

    @classmethod
    def for_redaction(cls, values: Iterable[str], keep_values: Iterable[str]) -> _ValueMatcher:
        """Safety-net matcher: kept values leave the set and their occurrences are spared."""
        keep = [str(value) for value in keep_values]
        kept = {key for key in map(_value_key, keep) if key}
        return cls(
            (value for value in values if _value_key(value) not in kept),
            protected=cls(keep) if kept else None,
        )

    def __bool__(self) -> bool:
        return bool(self.rules)

    def for_markup(self) -> _ValueMatcher:
        """Rules safe outside visible text: short numbers would hit sizes, counts, dates."""
        rules = [rule for rule in self.rules if not rule.short_number]
        return _ValueMatcher(rules=rules, protected=self.protected)

    def restrict(self, text: str) -> _ValueMatcher:
        """A matcher with only the rules that can occur somewhere in ``text``."""
        if "%" in text:
            text = f"{text}\n{unquote(text)}"
        lower = text.lower()
        stripped = _strip_separators(lower)
        rules = [rule for rule in self.rules if rule.may_occur(lower, stripped)]
        return _ValueMatcher(rules=rules, protected=self.protected)

    def finditer(self, text: str) -> list[tuple[int, int, str]]:
        """Non-overlapping ``(start, end, value)`` matches, leftmost-longest first."""
        if not self.rules or not text:
            return []
        lower = text.lower()
        stripped: str | None = None
        hits: list[tuple[int, int, str]] = []
        for rule in self.rules:
            if rule.key not in lower:
                if rule.compact is None:
                    continue
                if stripped is None:
                    stripped = _strip_separators(lower)
                if rule.compact not in stripped:
                    continue
            hits.extend(
                (m.start(), m.end(), rule.value)
                for m in rule.pattern.finditer(text)
                if m.end() > m.start()
            )
        if hits and self.protected is not None:
            shields = self.protected.finditer(text)
            hits = [hit for hit in hits if not any(s <= hit[0] and hit[1] <= e for s, e, _ in shields)]
        hits.sort(key=lambda hit: (hit[0], hit[0] - hit[1]))
        chosen: list[tuple[int, int, str]] = []
        for hit in hits:
            if not chosen or hit[0] >= chosen[-1][1]:
                chosen.append(hit)
        return chosen

    def search(self, text: str) -> bool:
        return bool(self.finditer(text))

    def sub(self, replacement: str, text: str) -> tuple[str, int]:
        hits = self.finditer(text)
        if not hits:
            return text, 0
        pieces, last = [], 0
        for start, end, _ in hits:
            pieces += [text[last:start], replacement]
            last = end
        pieces.append(text[last:])
        return "".join(pieces), len(hits)


def _scan_string(value: str, matcher: _ValueMatcher, fix: bool) -> tuple[str, list[str]]:
    """Find values in a raw string (also URL-unquoted); optionally replace them."""
    candidates = (value, unquote(value)) if "%" in value else (value,)
    for candidate in candidates:
        found = [v for _, _, v in matcher.finditer(candidate)]
        if found:
            return (matcher.sub(REDACTED, candidate)[0] if fix else value), found
    return value, []


def _checkable_attribute(part: str, element: Element, key: str, value: str) -> bool:
    """Skip identifiers, schema tokens and package plumbing (rewriting would corrupt)."""
    local = key.rpartition("}")[2]
    if len(value) < 3 or local.startswith("rsid") or local in _PROTECTED_ATTRIBUTES:
        return False
    if part.lower() == "[content_types].xml" or _SCHEMA_TOKEN.fullmatch(value):
        return False
    if element.tag == REL:
        return local == "Target" and element.get("TargetMode") == "External"
    return element.tag != W_FONT  # font table entries are font names


def _all_strings(root: Element, paragraphs: list[_Paragraph]) -> str:
    """Everything `_scan_tree` looks at -- joined paragraph texts, text and tail
    nodes, attribute values -- newline-joined, for the per-part pre-check."""
    strings = [paragraph.text for paragraph in paragraphs]
    for node in root.iter():
        strings += [node.text or "", node.tail or ""]
        if isinstance(node.tag, str):
            strings.extend(node.attrib.values())
    return "\n".join(strings)


def _scan_tree(part: str, root: Element, matcher: _ValueMatcher, *, fix: bool) -> list[dict[str, str]]:
    """Report (and with ``fix`` rewrite) every occurrence of a value in one part.

    Paragraphs are checked on their joined text, so values split across runs are
    found; all other text nodes and attribute values (markup) are checked one by
    one, without short numbers and skipping protected attributes.
    """
    findings: list[dict[str, str]] = []
    paragraphs = [_read_paragraph(element) for element in root.iter(W_P)]
    matcher = matcher.restrict(_all_strings(root, paragraphs))  # most parts drop out here
    if not matcher:
        return findings
    covered: set[Element] = set()
    for paragraph in paragraphs:
        covered.update(piece.element for piece in paragraph.pieces if piece.editable)
        hits = matcher.finditer(paragraph.text)
        findings.extend({"value": v, "part": part, "where": "text"} for _, _, v in hits)
        if fix and hits:
            _rewrite(paragraph.pieces, [(s, e, REDACTED) for s, e, _ in hits])
    markup = matcher.for_markup()
    if not markup:
        return findings
    for node in root.iter():
        is_element = isinstance(node.tag, str)
        if node.text and node not in covered and (is_element or node.tag is etree.Comment):
            new, found = _scan_string(node.text, markup, fix)
            findings.extend({"value": v, "part": part, "where": "text"} for v in found)
            if fix and found:
                if node.tag == W_T:
                    _set_text(node, new)
                else:
                    node.text = new
        if node.tail:
            new, found = _scan_string(node.tail, markup, fix)
            findings.extend({"value": v, "part": part, "where": "text"} for v in found)
            if fix and found:
                node.tail = new
        if not is_element:
            continue
        for key, value in node.attrib.items():
            if not _checkable_attribute(part, node, key, value):
                continue
            new, found = _scan_string(value, markup, fix)
            findings.extend({"value": v, "part": part, "where": "attribute"} for v in found)
            if fix and found:
                node.set(key, new)
    return findings


# --------------------------------------------------------------------------- #
# Working copy: parsed trees + segment index
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Working:
    """A mutable parse of the package with tracked changes accepted."""

    package: _Package
    trees: dict[str, Element]
    text_parts: list[tuple[str, str]]
    segments: list[Segment]
    paragraphs: dict[str, _Paragraph]
    accepted: int
    marks_kept: int
    dirty: set[str]

    @classmethod
    def build(cls, package: _Package, trees: dict[str, Element]) -> _Working:
        text_parts = _find_text_parts(package, trees)
        if not any(kind == "body" for _, kind in text_parts):
            raise DocxRefused("not a Word document (no main document part)")
        accepted = marks_kept = 0
        dirty: set[str] = set()
        for part, _ in text_parts:
            changes, marks = _accept_tracked_changes(trees[part])
            if changes:
                dirty.add(part)
                accepted += changes
                marks_kept += marks
        tables = _TableIndex.build(trees[part] for part, _ in text_parts)
        segments: list[Segment] = []
        paragraphs: dict[str, _Paragraph] = {}
        for part, kind in text_parts:
            for element in trees[part].iter(W_P):
                paragraph = _read_paragraph(element)
                if not paragraph.text.replace(OBJECT_CHAR, "").strip():
                    continue  # nothing analysable (empty, or only images/objects)
                segment = Segment(
                    f"s{len(segments)}",
                    part,
                    _location(element, kind),
                    paragraph.text,
                    **_layout(paragraph, kind, tables),
                )
                segments.append(segment)
                paragraphs[segment.id] = paragraph
        return cls(package, trees, text_parts, segments, paragraphs, accepted, marks_kept, dirty)

    def count(self, tag: str, kinds: Iterable[str] | None = None) -> int:
        wanted = set(kinds) if kinds is not None else None
        return sum(
            1
            for part, kind in self.text_parts
            if wanted is None or kind in wanted
            for _ in self.trees[part].iter(tag)
        )

    def notes(self, tag: str, kind: str) -> int:
        """Real footnotes/endnotes (separator notes excluded)."""
        return sum(
            1
            for part, k in self.text_parts
            if k == kind
            for note in self.trees[part].iter(tag)
            if note.get(W_TYPE) in (None, "normal")
        )

    def image_parts(self) -> list[str]:
        return [
            name
            for name in self.package.raw
            if self.package.content_type(name).startswith("image/")
            and not name.lower().startswith("docprops/")
        ]

    def stats(self) -> dict[str, int]:
        kinds = Counter(kind for _, kind in self.text_parts)
        return {
            "paragraphs": self.count(W_P),
            "tables": self.count(W_TBL),
            "headers": kinds["header"],
            "footers": kinds["footer"],
            "footnotes": self.notes(W_FOOTNOTE, "footnote"),
            "endnotes": self.notes(W_ENDNOTE, "endnote"),
            "comments": self.count(W_COMMENT, ["comment"]),
            "images": len(self.image_parts()),
            "hyperlinks": self.count(W_HYPERLINK),
            "tracked_changes_accepted": self.accepted,
        }

    def warnings(self) -> list[str]:
        names = [name.lower() for name in self.package.raw]
        messages = []
        if images := len(self.image_parts()):
            messages.append(f"{images} embedded image(s) were not analysed because text inside images is not supported.")
        if any(name.startswith(("word/charts/", "word/diagrams/")) for name in names):
            messages.append("Text in SmartArt graphics and charts was not analysed.")
        if objects := sum(name.startswith("word/embeddings/") for name in names):
            messages.append(f"{objects} embedded object(s) were not analysed or redacted.")
        if "word/glossary/document.xml" in names:
            messages.append("Building block (glossary) text was checked for known values only and was not analysed.")
        if self.marks_kept:
            messages.append(
                f"{self.marks_kept} deleted paragraph mark(s) were accepted without merging the affected paragraphs."
            )
        return messages


# --------------------------------------------------------------------------- #
# Redaction passes
# --------------------------------------------------------------------------- #
def _hyperlinks_touched(paragraph: _Paragraph, edits: list[Edit]) -> set[Element]:
    """Hyperlinks whose display text overlaps a redacted span."""
    links: set[Element] = set()
    for start, end, _ in edits:
        for piece in paragraph.pieces:
            if piece.start < end and piece.end > start:
                link = _ancestor(piece.element, W_HYPERLINK, stop=paragraph.element)
                if link is not None:
                    links.add(link)
    return links


def _is_sensitive_url(target: str, matcher: _ValueMatcher) -> bool:
    url = unquote(target).strip()
    if url.lower().startswith("mailto:"):
        return True
    return matcher.search(url) or matcher.search(_URL_PUNCTUATION.sub(" ", url))


def _scrub_hyperlinks(work: _Working, matcher: _ValueMatcher, flagged: set[Element]) -> int:
    """Unlink mailto/PII hyperlinks and neutralise their relationship targets."""
    unlinked = 0
    for part, _ in work.text_parts:
        root = work.trees[part]
        rels_name = work.package.find(_rels_name(part))
        rels_root = work.trees.get(rels_name) if rels_name else None
        rels = {rel.get("Id"): rel for rel in rels_root.iter(REL)} if rels_root is not None else {}
        for link in list(root.iter(W_HYPERLINK)):
            tooltip = link.get(W_TOOLTIP)
            if tooltip and matcher.search(tooltip):
                del link.attrib[W_TOOLTIP]
                work.dirty.add(part)
            rel = rels.get(link.get(R_ID))
            if rel is None:
                continue
            if link in flagged or _is_sensitive_url(rel.get("Target", ""), matcher):
                rel.set("Target", REDACTED_URL)
                work.dirty.update({part, rels_name})
                _unwrap(link)
                unlinked += 1
    for name, root in work.trees.items():  # e.g. hyperlinks on drawings and shapes
        if not name.lower().endswith(".rels"):
            continue
        for rel in root.iter(REL):
            target = rel.get("Target", "")
            if (
                rel.get("Type", "").endswith("/hyperlink")
                and target != REDACTED_URL
                and _is_sensitive_url(target, matcher)
            ):
                rel.set("Target", REDACTED_URL)
                work.dirty.add(name)
                unlinked += 1
    return unlinked


def _scrub_instruction(text: str, matcher: _ValueMatcher) -> str:
    candidates = (text, unquote(text)) if "%" in text else (text,)
    for candidate in candidates:
        new, values = matcher.sub("REDACTED", candidate)
        new, emails = _EMAIL.subn("REDACTED", new)
        if values or emails:
            return new
    return text


def _scrub_fields(work: _Working, matcher: _ValueMatcher) -> int:
    """Scrub redacted values and e-mail addresses from field codes."""
    matcher = matcher.for_markup()  # field switches and bookmark ids hold short numbers
    scrubbed = 0
    for part, _ in work.text_parts:
        root = work.trees[part]
        for element in root.iter(W_INSTRTEXT):
            old = element.text or ""
            new = _scrub_instruction(old, matcher)
            if new != old:
                _set_text(element, new)
                scrubbed += 1
                work.dirty.add(part)
        for element in root.iter(W_FLDSIMPLE):
            old = element.get(W_INSTR) or ""
            new = _scrub_instruction(old, matcher)
            if new != old:
                element.set(W_INSTR, new)
                scrubbed += 1
                work.dirty.add(part)
    return scrubbed


def _package_target(work: _Working, type_suffix: str) -> str | None:
    """Entry name of the package-level relationship target with the given type."""
    rels_name = work.package.find("_rels/.rels")
    root = work.trees.get(rels_name) if rels_name else None
    if root is None:
        return None
    for rel in root.iter(REL):
        if rel.get("Type", "").endswith(type_suffix):
            return work.package.find(rel.get("Target", ""))
    return None


def _clear_properties(work: _Working) -> list[str]:
    """Empty author-like fields in docProps/core.xml and docProps/app.xml."""
    cleared = []
    targets = (
        (_package_target(work, "/core-properties") or work.package.find("docProps/core.xml"),
         (f"{{{DC_NS}}}creator", f"{{{CP_NS}}}lastModifiedBy")),
        (_package_target(work, "/extended-properties") or work.package.find("docProps/app.xml"),
         (f"{{{EP_NS}}}Company", f"{{{EP_NS}}}Manager")),
    )
    for part, tags in targets:
        root = work.trees.get(part) if part else None
        if root is None:
            continue
        for element in root.iter(*tags):
            if element.text and element.text.strip():
                element.text = ""
                cleared.append(f"{part}: {etree.QName(element).localname}")
                work.dirty.add(part)
    return cleared


def _anonymise_authors(work: _Working) -> tuple[int, list[str]]:
    """Map comment/revision/people authors to a consistent 'Reviewer N' and drop
    presence info. Returns (distinct authors anonymised, parts changed)."""
    aliases: dict[str, str] = {}
    changed_parts: list[str] = []
    for part, root in work.trees.items():
        changed = False
        for element in root.iter(etree.Element):
            for attribute in (W_AUTHOR, W15_AUTHOR):
                if author := element.get(attribute):
                    element.set(attribute, aliases.setdefault(author, f"Reviewer {len(aliases) + 1}"))
                    changed = True
            if element.get(W_INITIALS):
                element.set(W_INITIALS, "R")
                changed = True
        for presence in list(root.iter(W15_PRESENCE)):
            _remove(presence)
            changed = True
        if changed:
            work.dirty.add(part)
            changed_parts.append(part)
    return len(aliases), changed_parts


def _remove_thumbnail(work: _Working) -> set[str]:
    """Drop docProps/thumbnail.*, its package relationship and its Override."""
    removed: set[str] = set()
    rels_name = work.package.find("_rels/.rels")
    rels_root = work.trees.get(rels_name) if rels_name else None
    if rels_root is not None:
        for rel in list(rels_root.iter(REL)):
            if rel.get("Type", "").endswith("/thumbnail"):
                if target := work.package.find(rel.get("Target", "")):
                    removed.add(target)
                _remove(rel)
                work.dirty.add(rels_name)
    removed.update(n for n in work.package.raw if n.lower().startswith("docprops/thumbnail."))
    types_name = work.package.find("[Content_Types].xml")
    if removed and types_name:
        lowered = {name.lower() for name in removed}
        for override in list(work.trees[types_name].iter(CT_OVERRIDE)):
            if override.get("PartName", "").lstrip("/").lower() in lowered:
                _remove(override)
                work.dirty.add(types_name)
    return removed


def _scrub_metadata(work: _Working) -> tuple[list[str], int, set[str]]:
    """Clear author properties, anonymise authors and drop the thumbnail.

    Returns (actions for the report, distinct authors anonymised, parts to drop).
    """
    actions = _clear_properties(work)
    authors, parts = _anonymise_authors(work)
    actions += [f"{part}: authors anonymised" for part in parts]
    removed = _remove_thumbnail(work)
    actions += [f"{name}: removed" for name in sorted(removed)]
    return actions, authors, removed


def _verify_residuals(work: _Working, matcher: _ValueMatcher) -> int:
    """Safety net: replace any remaining value in any XML part with [REDACTED]."""
    if not matcher:
        return 0
    fixes = 0
    for part, root in work.trees.items():
        if found := len(_scan_tree(part, root, matcher, fix=True)):
            fixes += found
            work.dirty.add(part)
    return fixes


def _clone_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    clone.compress_type = info.compress_type
    clone.external_attr = info.external_attr
    clone.create_system = info.create_system
    clone.comment = info.comment
    return clone


def _write_package(work: _Working, removed: set[str]) -> bytes:
    """Re-zip in the original entry order; untouched entries are copied verbatim."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for info in work.package.infos:
            name = info.filename
            if name in removed:
                continue
            if name in work.dirty:
                data = etree.tostring(
                    work.trees[name], xml_declaration=True, encoding="UTF-8", standalone=True
                )
            else:
                data = work.package.raw[name]
            archive.writestr(_clone_info(info), data)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
class DocxDocument:
    """A safely opened ``.docx``: segments for analysis plus an in-place redactor.

    Build instances with :meth:`from_bytes`. The original bytes are kept, and every
    :meth:`redact` call works on a fresh parse, so redaction can be repeated.
    """

    def __init__(
        self,
        package: _Package,
        segments: list[Segment],
        stats: dict[str, int],
        warnings: list[str],
    ) -> None:
        self._package = package
        self.segments = segments
        self.stats = stats
        self.warnings = warnings

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        max_bytes: int = MAX_BYTES,
        max_uncompressed: int = MAX_UNCOMPRESSED,
        max_entries: int = MAX_ENTRIES,
    ) -> DocxDocument:
        """Open and validate a ``.docx``; raises :class:`DocxRefused` / :class:`DocxError`."""
        package = _read_package(
            data, max_bytes=max_bytes, max_uncompressed=max_uncompressed, max_entries=max_entries
        )
        trees = _parse_parts(package)
        _check_policy(package, trees)
        work = _Working.build(package, trees)
        return cls(package, work.segments, work.stats(), work.warnings())

    def document_text(self) -> tuple[str, list[int]]:
        """All segment texts joined with '\\n', plus the start offset of each segment."""
        starts, offset = [], 0
        for segment in self.segments:
            starts.append(offset)
            offset += len(segment.text) + 1
        return "\n".join(segment.text for segment in self.segments), starts

    def redact(
        self,
        replacements: dict[str, list[tuple[int, int, str]]],
        *,
        redacted_values: Iterable[str] = (),
        keep_values: Iterable[str] = (),
        scrub_metadata: bool = True,
    ) -> tuple[bytes, dict]:
        """Apply ``segment_id -> [(start, end, replacement), ...]`` (segment-local offsets).

        Returns ``(new_docx_bytes, report)``. The text of every replaced span, plus
        ``redacted_values``, feeds hyperlink/field scrubbing and the residual verifier.
        ``keep_values`` (occurrences the user chose to keep) are removed from that set,
        compared case-insensitively and separator-free for numbers, and the safety net
        leaves their occurrences alone. Explicit replacements are always applied.
        """
        work = _Working.build(self._package, _parse_parts(self._package))
        segments = {segment.id: segment for segment in self.segments}
        warnings = list(self.warnings)
        values = [str(v) for v in redacted_values]
        flagged: set[Element] = set()
        by_location: Counter[str] = Counter()

        for segment_id, raw_edits in replacements.items():
            segment = segments.get(segment_id)
            if segment is None:
                raise ValueError(f"unknown segment id {segment_id!r}")
            paragraph = work.paragraphs[segment_id]
            if paragraph.text != segment.text:  # parsing is deterministic; guard anyway
                raise DocxError(f"segment {segment_id} changed between parses")
            edits, merged = _normalise_edits(raw_edits, len(segment.text))
            if merged:
                warnings.append(f"Segment {segment_id}: {merged} overlapping replacement(s) were merged.")
            if not edits:
                continue
            values.extend(segment.text[start:end] for start, end, _ in edits)
            flagged |= _hyperlinks_touched(paragraph, edits)
            _rewrite(paragraph.pieces, edits)
            work.dirty.add(segment.part)
            by_location[segment.location] += len(edits)

        matcher = _ValueMatcher.for_redaction(values, keep_values)
        hyperlinks = _scrub_hyperlinks(work, matcher, flagged)
        fields = _scrub_fields(work, matcher)
        cleared, authors, removed = _scrub_metadata(work) if scrub_metadata else ([], 0, set())
        residual_fixes = _verify_residuals(work, matcher)  # always last, before zipping

        report = {
            "replacements_applied": sum(by_location.values()),
            "by_location": dict(by_location),
            "hyperlinks_unlinked": hyperlinks,
            "fields_scrubbed": fields,
            "metadata_cleared": cleared,
            "comment_authors_anonymised": authors,
            "thumbnail_removed": bool(removed),
            "tracked_changes_accepted": work.accepted,
            "residual_fixes": residual_fixes,
            "warnings": warnings,
        }
        return _write_package(work, removed), report


def _dump_text(node: Element, out: list[str], in_paragraph: bool = False) -> None:
    """Append all text of ``node``: runs glued within a paragraph, newline elsewhere."""
    is_paragraph = node.tag == W_P
    inside = in_paragraph or is_paragraph
    if node.text and (isinstance(node.tag, str) or node.tag is etree.Comment):
        out.append(node.text)
    for child in node:
        _dump_text(child, out, inside)
        if child.tail:
            out.append(child.tail)
    if is_paragraph or not inside:
        out.append("\n")


def extract_text_raw(data: bytes) -> str:
    """Dump every text node and attribute value of every XML part (XML-unescaped,
    URL-unquoted), including deleted text, field codes and metadata."""
    package = _read_package(data)
    chunks: list[str] = []
    for name in package.xml_parts:
        root = _parse_xml(name, package.raw[name])
        _dump_text(root, chunks)
        for element in root.iter(etree.Element):
            chunks.extend(f"{value}\n" for value in element.attrib.values())
        chunks.append("\n")
    return unquote("".join(chunks))


def find_residuals(data: bytes, values: Iterable[str]) -> list[dict]:
    """Every occurrence of any value in any XML part of ``data``.

    Returns ``[{"value", "part", "where": "text" | "attribute"}, ...]``. Matching is
    case-insensitive, letter-heavy values match on word boundaries, values with at
    least 6 digits also match ignoring spaces/dashes/dots/parentheses, and values
    shorter than 3 characters are ignored. Paragraphs are matched on joined run text.
    Same scope as the residual verifier: short numbers (no letters, < 6 digits) only
    in paragraph text; identifiers, schema tokens and package plumbing are skipped.
    """
    matcher = _ValueMatcher(values)
    if not matcher:
        return []
    package = _read_package(data)
    findings: list[dict] = []
    for name in package.xml_parts:
        findings.extend(_scan_tree(name, _parse_xml(name, package.raw[name]), matcher, fix=False))
    return findings
