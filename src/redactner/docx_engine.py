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

import bisect
import posixpath
import re
import unicodedata
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from io import BytesIO
from itertools import islice
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
MAX_SEGMENTS = 200_000  # paragraphs with text; far beyond any real document
MAX_PARAGRAPHS = 1_000_000  # paragraph elements of any kind, empty ones included
MAX_ELEMENTS = 5_000_000  # XML elements in all parts together
MAX_BLOCK_STRINGS = 10_000  # strings of one drawing text block that are joined for the residual scan

OBJECT_CHAR = "\N{OBJECT REPLACEMENT CHARACTER}"  # drawings, symbols, embedded objects
REDACTED = "[REDACTED]"  # replacement used by the residual verifier
REDACTED_URL = "https://redacted.invalid/"

# --------------------------------------------------------------------------- #
# Namespaces and tag names
# --------------------------------------------------------------------------- #
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
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
W_BOOKMARK_START, W_NAME, W_ANCHOR = _w("bookmarkStart"), _w("name"), _w("anchor")
W_FLDCHAR, W_FLDCHARTYPE = _w("fldChar"), _w("fldCharType")
M_T, M_PREFIX = f"{{{M_NS}}}t", f"{{{M_NS}}}"
A_P, A_T = (f"{{{A_NS}}}{local}" for local in ("p", "t"))
W_DELTEXT = _w("delText")
SVG_TEXT = "{http://www.w3.org/2000/svg}text"
VML_TEXTBOX = "{urn:schemas-microsoft-com:vml}textbox"
R_ID = f"{{{R_NS}}}id"
MC_ALTERNATE, MC_CHOICE, MC_FALLBACK = (
    f"{{{MC_NS}}}{local}" for local in ("AlternateContent", "Choice", "Fallback")
)
W15_AUTHOR, W15_PRESENCE = f"{{{W15_NS}}}author", f"{{{W15_NS}}}presenceInfo"
REL = f"{{{PKG_REL_NS}}}Relationship"
CT_DEFAULT, CT_OVERRIDE = f"{{{CT_NS}}}Default", f"{{{CT_NS}}}Override"
XML_SPACE = f"{{{XML_NS}}}space"

# Paragraph walker (also Office Math ``m:t`` and ruby text): run content that maps to characters, and containers to descend
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
        "fldSimple", "dir", "bdo", "ins", "moveTo", "ruby", "rt", "rubyBase",
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
# Free-text attributes (names, captions, alt text) never hold schema tokens, so the
# token skip does not apply to them -- nor to custom XML and document properties.
_WP_DOCPR = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"
_FREE_TEXT_ATTRIBUTES = frozenset({
    (_w("docVar"), "val"), (_w("alias"), "val"), (_w("tag"), "val"),
    (_WP_DOCPR, "descr"), (_WP_DOCPR, "title"), ("{urn:schemas-microsoft-com:vml}textpath", "string"),
})
_DATA_PART_PREFIXES = ("customxml/", "docprops/")
_SCHEMA_TOKEN = re.compile(
    r"[a-z][A-Za-z0-9]*|[-+]?(?=[0-9A-Fa-f.]*\d)[0-9A-Fa-f.]+(?:%|pt|in|cm|mm|pc|pi|em|px)?"
)

_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
# The look-behind makes each run of address characters be tried once (not quadratic).
_EMAIL = re.compile(r"(?<![\w.%+-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
_XML_ILLEGAL = re.compile("[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_HYPERLINK_FIELD = re.compile(r"\s*HYPERLINK\b", re.IGNORECASE)
_FIELD_TOKEN = re.compile(r'"[^"]*"?|\\[A-Za-z]\S*|\S+')
_SWITCHES_WITH_ARGUMENT = frozenset({"\\l", "\\o", "\\t"})  # bookmark, tooltip, frame
_SEPARATOR_CLASS = r"[\s\-\N{HYPHEN}-\N{HORIZONTAL BAR}\N{MINUS SIGN}.()/,]"  # spaces, dashes, dots, parens, / ,
# Characters that are written differently but read the same: a copy of a redacted value may use any form.
_HYPHENS = "-\N{HYPHEN}\N{NON-BREAKING HYPHEN}\N{FIGURE DASH}\N{EN DASH}\N{EM DASH}\N{HORIZONTAL BAR}\N{MINUS SIGN}"
_APOSTROPHES = "'\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}\N{MODIFIER LETTER APOSTROPHE}"
_FULLWIDTH_ZERO = ord("\N{FULLWIDTH DIGIT ZERO}")
_LOOKALIKES = str.maketrans({
    **{ch: "-" for ch in _HYPHENS}, **{ch: "'" for ch in _APOSTROPHES},
    **{chr(_FULLWIDTH_ZERO + digit): str(digit) for digit in range(10)},
})
_ZERO_WIDTH_CLASS = "[\u200b-\u200f\u00ad\u2060\ufeff]"  # invisible characters inside a value
_ZERO_WIDTH = re.compile(_ZERO_WIDTH_CLASS)
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
    unreadable: list[str] = field(default_factory=list)  # SVG / VML parts that are not valid XML

    def find(self, name: str) -> str | None:
        """Entry name for a part name, case-insensitively (OPC names are)."""
        return self.names.get(posixpath.normpath(name.lstrip("/")).lower())

    def content_type(self, name: str) -> str:
        extension = posixpath.splitext(name)[1].lstrip(".").lower()
        return self.overrides.get(name.lower()) or self.defaults.get(extension, "")

    @property
    def xml_parts(self) -> list[str]:
        """Every XML entry (raster images excluded; SVG and VML drawings are read as XML)."""
        parts = []
        for name in self.raw:
            if name.endswith("/"):
                continue
            ctype = self.content_type(name)
            if ctype.startswith("image/") and not _is_optional_xml(name, ctype):
                continue
            if name.lower().endswith((".xml", ".rels", ".svg", ".vml")) or ctype.endswith("xml"):
                parts.append(name)
        return parts

    def opaque_parts(self) -> list[str]:
        """Entries that are neither XML nor images, media, fonts or embeddings."""
        xml = set(self.xml_parts)
        found = []
        for name in self.raw:
            ctype = self.content_type(name).lower()
            lowered = name.lower()
            if name.endswith("/") or name in xml or name.startswith("[") or lowered.startswith("word/embeddings/"):
                continue
            if ctype.startswith(_BINARY_TYPE_PREFIXES) or "font" in ctype or "oleobject" in ctype:
                continue
            if lowered.endswith(_BINARY_SUFFIXES):
                continue
            found.append(name)
        return found


_BINARY_TYPE_PREFIXES = ("image/", "audio/", "video/", "font/")
_BINARY_SUFFIXES = (".odttf", ".fntdata", ".ttf", ".otf", ".woff", ".woff2", ".eot")


def _is_optional_xml(name: str, ctype: str = "") -> bool:
    """SVG / VML drawings: scanned as XML when they parse, otherwise left alone (with a warning).

    A part declared as some other XML type (e.g. a header named ``header1.vml``) is not a drawing.
    """
    if ctype == "image/svg+xml":
        return True
    return name.lower().endswith((".svg", ".vml")) and not ctype.lower().endswith("xml")


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


def _check_element_count(package: _Package) -> None:
    """Refuse packages built to be slow, before parsing them.

    A small file can hold millions of empty elements, and each one is visited several times while a
    document is opened, redacted and checked. Every element needs a ``<`` that does not start an end
    tag, a comment or a processing instruction, so counting bytes can only over-estimate.
    """
    elements = 0
    for name in package.xml_parts:
        raw = package.raw[name]
        elements += raw.count(b"<") - raw.count(b"</") - raw.count(b"<!") - raw.count(b"<?")
        if elements > MAX_ELEMENTS:
            raise DocxRefused(f"too many XML elements (more than {MAX_ELEMENTS:,})")


def _check_paragraph_count(trees: dict[str, Element]) -> None:
    """Paragraphs cost far more than other elements, empty or not, so they have their own limit."""
    paragraphs = 0
    for root in trees.values():
        paragraphs += sum(1 for _ in islice(root.iter(W_P, A_P), MAX_PARAGRAPHS + 1 - paragraphs))
        if paragraphs > MAX_PARAGRAPHS:
            raise DocxRefused(f"too many paragraphs (more than {MAX_PARAGRAPHS:,})")


def _parse_parts(package: _Package) -> dict[str, Element]:
    """Fresh, mutable trees for every XML part."""
    _check_element_count(package)
    trees: dict[str, Element] = {}
    for name in package.xml_parts:
        try:
            trees[name] = _parse_xml(name, package.raw[name])
        except DocxError:
            if not _is_optional_xml(name, package.content_type(name)):
                raise
            if name not in package.unreadable:  # e.g. an SVG with a DOCTYPE: do not refuse the file
                package.unreadable.append(name)
    _check_paragraph_count(trees)
    return trees


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
    """Replace ``element`` by its children, in place (no index lookups: linear in siblings)."""
    if element.getparent() is None:
        return
    for child in list(element):
        element.addprevious(child)
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


def _flatten_text_nodes(root: Element) -> bool:
    """Fold comments / processing instructions out of ``w:t`` and ``m:t`` (text + child tails).

    Word shows ``Jo<!--x-->hn`` as "John"; without this the model and the rewriter would
    see only "Jo". Returns whether anything changed.
    """
    changed = False
    for node in list(root.iter(W_T, M_T)):
        if len(node) == 0:
            continue
        text = (node.text or "") + "".join(child.tail or "" for child in node)
        for child in list(node):
            node.remove(child)
        _set_text(node, text)
        changed = True
    return changed


def _read_paragraph(paragraph: Element) -> _Paragraph:
    """Map a ``w:p`` to its visible text and the pieces that produce it."""
    pieces: list[_Piece] = []
    chunks: list[str] = []
    position = 0

    def text_piece(child: Element) -> None:
        nonlocal position
        text = child.text or ""
        pieces.append(_Piece(child, position, position + len(text), True))
        chunks.append(text)
        position += len(text)

    def visit(parent: Element, text_only: bool = False) -> None:
        nonlocal position
        for child in parent:
            tag = child.tag
            if tag == W_T or (tag == M_T and not text_only):
                text_piece(child)
            elif tag in _CHAR_PIECES:
                if not text_only:
                    pieces.append(_Piece(child, position, position + 1, False))
                    chunks.append(_CHAR_PIECES[tag])
                    position += 1
            elif tag in _CONTAINERS:
                visit(child, text_only)
            elif tag == MC_ALTERNATE:  # render Choice, else Fallback; other branches: text only
                branches = [b for b in child if b.tag in (MC_CHOICE, MC_FALLBACK)]
                main = child.find(MC_CHOICE)
                if main is None:
                    main = child.find(MC_FALLBACK)
                for branch in branches:
                    visit(branch, text_only or branch is not main)
            elif not text_only and isinstance(tag, str) and tag.startswith(M_PREFIX):
                if not tag.endswith("Pr"):  # Office Math structure; *Pr = properties
                    visit(child)

    visit(paragraph)
    return _Paragraph(paragraph, pieces, "".join(chunks))


def _overlapping(pieces: list[_Piece], ends: list[int], start: int, end: int) -> Iterable[int]:
    """Indexes of the non-empty pieces overlapping ``[start, end)``.

    Pieces are contiguous, so ``ends`` (their end offsets) is sorted and can be bisected.
    """
    i = bisect.bisect_right(ends, start)
    while i < len(pieces) and pieces[i].start < end:
        if pieces[i].end > pieces[i].start:
            yield i
        i += 1


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
    ends = [piece.end for piece in pieces]
    for start, end, replacement in edits:
        hits = list(_overlapping(pieces, ends, start, end))
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
        (max(0, int(s)), min(length, int(e)), _XML_ILLEGAL.sub("", str(r))) for s, e, r in edits
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
def _fold(text: str) -> str:
    """Lower-cased, zero-width characters removed, NFC, look-alike hyphens, apostrophes and
    full-width digits in their plain form: the form used for cheap pre-checks."""
    return unicodedata.normalize("NFC", _ZERO_WIDTH.sub("", text.lower())).translate(_LOOKALIKES)


def _char_pattern(ch: str) -> str:
    """Pattern for one character of a value that also accepts the other ways it is written:
    any hyphen or dash for a hyphen, a curly apostrophe for a straight one, a full-width digit for a digit."""
    if ch in _HYPHENS:
        return f"[{re.escape(_HYPHENS)}]"
    if ch in _APOSTROPHES:
        return f"[{re.escape(_APOSTROPHES)}]"
    if ch.isdecimal():
        digit = unicodedata.digit(ch)
        return "[" + "".join(sorted({ch, str(digit), chr(_FULLWIDTH_ZERO + digit)})) + "]"
    return re.escape(ch)


def _normalise_value(value: str) -> str | None:
    value = " ".join(_ZERO_WIDTH.sub("", value.replace(OBJECT_CHAR, " ")).split())
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
        glue = f"{_ZERO_WIDTH_CLASS}*"
        words = rf"(?:[\s_]|{_ZERO_WIDTH_CLASS})+" if mostly_letters else rf"(?:\s|{_ZERO_WIDTH_CLASS})+"
        compact = _strip_separators(value)
        flexible = sum(ch.isdigit() for ch in compact) >= 6
        alternatives: list[str] = []
        for form in ("NFC", "NFD"):  # the text may be composed or decomposed
            spelled = unicodedata.normalize(form, value)
            literal = words.join(
                glue.join(_char_pattern(ch) for ch in token) for token in spelled.split(" ")
            )
            alternatives.append(_bounded(literal, spelled, mostly_letters))
            if flexible:
                spelled = unicodedata.normalize(form, compact)
                pattern = f"(?:{_SEPARATOR_CLASS}|{_ZERO_WIDTH_CLASS})*".join(_char_pattern(ch) for ch in spelled)
                alternatives.append(_bounded(pattern, spelled, mostly_letters))
        key = max(re.findall(r"\w+", _fold(value)), key=len, default=_fold(value))
        return cls(
            value,
            key,
            _strip_separators(_fold(value)) if flexible else None,
            re.compile("|".join(dict.fromkeys(alternatives)), re.IGNORECASE),
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
    folded = _fold(normal)
    compact = _strip_separators(folded)
    return compact if sum(ch.isdigit() for ch in compact) >= 6 else folded


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
        lower = _fold(text)
        stripped = _strip_separators(lower)
        rules = [rule for rule in self.rules if rule.may_occur(lower, stripped)]
        return _ValueMatcher(rules=rules, protected=self.protected)

    def finditer(self, text: str) -> list[tuple[int, int, str]]:
        """Non-overlapping ``(start, end, value)`` matches, leftmost-longest first."""
        if not self.rules or not text:
            return []
        lower = _fold(text)
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
    if part.lower() == "[content_types].xml":
        return False
    free_text = (element.tag, local) in _FREE_TEXT_ATTRIBUTES or part.lower().startswith(_DATA_PART_PREFIXES)
    if not free_text and _SCHEMA_TOKEN.fullmatch(value):
        return False
    if element.tag == REL:
        return local == "Target" and element.get("TargetMode") == "External"
    return element.tag != W_FONT  # font table entries are font names


def _flat_paragraphs(root: Element) -> list[_Paragraph]:
    """Paragraphs of text kept outside ``w:t``: DrawingML ``a:t`` runs (charts, SmartArt)
    and ``w:delText`` runs (deletions in parts where changes are not accepted)."""
    found: list[_Paragraph] = []
    for element in root.iter(A_P, W_P):
        tag = A_T if element.tag == A_P else W_DELTEXT
        pieces: list[_Piece] = []
        position = 0
        for node in element.iter(tag):
            if tag == W_DELTEXT and next(node.iterancestors(W_P), None) is not element:
                continue  # belongs to a nested paragraph (text box)
            text = node.text or ""
            pieces.append(_Piece(node, position, position + len(text), True))
            position += len(text)
        if len(pieces) > 1:  # a single run is covered by the per-node scan
            found.append(_Paragraph(element, pieces, "".join(node.element.text or "" for node in pieces)))
    return found


def _mixed_text_blocks(root: Element) -> list[Element]:
    """Drawing text that is spread over nested elements outside WordprocessingML: an SVG ``<text>``
    with ``<tspan>`` children, or the HTML-like content of a VML ``<v:textbox>``."""
    blocks = []
    for element in root.iter(SVG_TEXT, VML_TEXTBOX):
        if len(element) == 0:
            continue  # a single string: the per-node scan covers it
        if element.tag == VML_TEXTBOX and next(element.iter(W_TXBX), None) is not None:
            continue  # holds Word paragraphs, which are read as paragraphs
        if next(element.iterancestors(SVG_TEXT, VML_TEXTBOX), None) is None:
            blocks.append(element)
    return blocks


def _block_strings(block: Element) -> list[tuple[Element, bool]]:
    """Every string inside a block in reading order, as ``(node, is_tail)``: the node's own text, or
    the text that follows the node. Empty when the block is too large to be worth joining."""
    slots: list[tuple[Element, bool]] = []
    for event, node in etree.iterwalk(block, events=("start", "end", "comment", "pi")):
        if event == "start":
            slots.append((node, False))
        elif node is not block:  # the end of a child, or a comment: the text that follows it
            slots.append((node, True))
        if len(slots) > MAX_BLOCK_STRINGS:
            return []
    return slots


def _block_texts(block: Element) -> list[str]:
    """The two readings of a block: its strings run together, and its strings as separate words."""
    strings = [(node.tail if tail else node.text) or "" for node, tail in _block_strings(block)]
    return ["".join(strings), " ".join(strings)]


def _scan_block(part: str, block: Element, matcher: _ValueMatcher, fix: bool) -> list[dict[str, str]]:
    """Find (and with ``fix`` replace) values that run across the strings of one drawing text block.

    A name can be split over elements letter by letter ("Ja" + "ne Doe") or word by word, each word
    positioned on its own ("Jane" + "Doe"), so the strings are read both run together and joined by
    spaces. Values inside a single string are left to the per-node scan.
    """
    findings: list[dict[str, str]] = []
    seen: set[tuple[int, int, int]] = set()
    for separator in ("", " "):
        slots = _block_strings(block)
        strings = [(node.tail if tail else node.text) or "" for node, tail in slots]
        starts, position = [], 0
        for string in strings:
            starts.append(position)
            position += len(string) + len(separator)
        changes: dict[int, list[tuple[int, int, str]]] = {}
        for start, end, value in matcher.finditer(separator.join(strings)):
            pieces = []  # (string index, local start, local end) of the characters the hit covers
            k = max(bisect.bisect_right(starts, start) - 1, 0)
            while k < len(starts) and starts[k] < end:
                a, b = max(start, starts[k]) - starts[k], min(end, starts[k] + len(strings[k])) - starts[k]
                if b > a:
                    pieces.append((k, a, b))
                k += 1
            if len(pieces) < 2 or set(pieces) <= seen:
                continue  # inside one string, or already found in the other reading
            seen.update(pieces)
            findings.append({"value": value, "part": part, "where": "text"})
            for n, (k, a, b) in enumerate(pieces):
                changes.setdefault(k, []).append((a, b, REDACTED if n == 0 else ""))
        if not fix or not changes:
            continue
        for k, edits in changes.items():
            node, tail = slots[k]
            out, last = [], 0
            for a, b, replacement in sorted(edits):
                out += [strings[k][last:a], replacement]
                last = b
            new = "".join(out) + strings[k][last:]
            if tail:
                node.tail = new
            else:
                node.text = new
        seen.clear()  # positions changed; the next reading starts from the rewritten strings
    return findings


def _all_strings(root: Element, paragraphs: list[_Paragraph], extra: Iterable[str] = ()) -> str:
    """Everything `_scan_tree` looks at -- joined paragraph texts, text and tail
    nodes, attribute values -- newline-joined, for the per-part pre-check."""
    strings = [paragraph.text for paragraph in paragraphs]
    strings.extend(extra)
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
    _flatten_text_nodes(root)  # in memory; written only if a fix makes the part dirty
    paragraphs = [_read_paragraph(element) for element in root.iter(W_P)]
    split_fields = [f for f in _field_structure(root)[0] if len(f.instr) > 1]
    flat = _flat_paragraphs(root)
    joined = [f.text for f in split_fields] + [paragraph.text for paragraph in flat]
    blocks = _mixed_text_blocks(root)
    block_texts = [text for block in blocks for text in _block_texts(block)]
    matcher = matcher.restrict(_all_strings(root, paragraphs, joined + block_texts))  # most parts drop out here
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
    for paragraph in flat:  # runs of one a:p / deleted w:p, joined
        covered.update(piece.element for piece in paragraph.pieces)
        hits = markup.finditer(paragraph.text)
        findings.extend({"value": v, "part": part, "where": "text"} for _, _, v in hits)
        if fix and hits:
            _rewrite(paragraph.pieces, [(s, e, REDACTED) for s, e, _ in hits])
    for instruction, text in zip(split_fields, joined):  # one field code split over elements
        covered.update(instruction.instr)
        new, found = _scan_string(text, markup, fix)
        findings.extend({"value": v, "part": part, "where": "text"} for v in found)
        if fix and found:
            _write_instruction(instruction.instr, new)
    for block in blocks:  # an SVG <text> or VML text box: values running across its elements
        findings.extend(_scan_block(part, block, markup, fix))
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
            if _flatten_text_nodes(trees[part]):
                dirty.add(part)
        tables = _TableIndex.build(trees[part] for part, _ in text_parts)
        segments: list[Segment] = []
        paragraphs: dict[str, _Paragraph] = {}
        for part, kind in text_parts:
            for element in trees[part].iter(W_P):
                paragraph = _read_paragraph(element)
                if not paragraph.text.replace(OBJECT_CHAR, "").strip():
                    continue  # nothing analysable (empty, or only images/objects)
                if len(segments) >= MAX_SEGMENTS:
                    raise DocxRefused(f"too many paragraphs (more than {MAX_SEGMENTS:,})")
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
        if self.package.unreadable:
            messages.append(
                f"{len(self.package.unreadable)} SVG/VML drawing(s) could not be read as XML and were copied unchanged."
            )
        if opaque := len(self.package.opaque_parts()):
            messages.append(f"{opaque} part(s) of other types were copied unchanged and not checked for personal data.")
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
    ends = [piece.end for piece in paragraph.pieces]
    for start, end, _ in edits:
        for i in _overlapping(paragraph.pieces, ends, start, end):
            link = _ancestor(paragraph.pieces[i].element, W_HYPERLINK, stop=paragraph.element)
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


def _rename_bookmarks(work: _Working, matcher: _ValueMatcher) -> int:
    """Give bookmarks whose name holds a redacted value a neutral name, and follow every reference.

    A bookmark name is an identifier that internal links and cross-reference fields point at. Replacing
    it like ordinary text would leave a name Word does not allow, could give two bookmarks the same
    name, and would break the references. Each such bookmark becomes ``Redacted1``, ``Redacted2`` and
    so on, and links (``w:anchor``) and field codes (``REF``, ``PAGEREF``, ``HYPERLINK \\l``) are
    changed to match. Returns the number of bookmarks renamed.
    """
    markup = matcher.for_markup()
    if not markup:
        return 0
    bookmarks = [(part, element) for part, root in work.trees.items() for element in root.iter(W_BOOKMARK_START)]
    taken = {(element.get(W_NAME) or "").lower() for _, element in bookmarks}
    renamed: dict[str, str] = {}  # old name, lower-cased (Word compares names without case) -> new name
    for part, element in bookmarks:
        name = element.get(W_NAME) or ""
        if name.lower() not in renamed:
            if not name or not markup.search(name):
                continue
            number = len(renamed) + 1
            while f"redacted{number}" in taken:
                number += 1
            taken.add(f"redacted{number}")
            renamed[name.lower()] = f"Redacted{number}"
        element.set(W_NAME, renamed[name.lower()])
        work.dirty.add(part)
    if not renamed:
        return 0
    names = "|".join(re.escape(old) for old in sorted(renamed, key=len, reverse=True))
    reference = re.compile(rf"(?<![\w.])(?:{names})(?![\w.])", re.IGNORECASE)

    def swap(match: re.Match[str]) -> str:
        return renamed[match.group().lower()]

    for part, root in work.trees.items():
        for link in root.iter(W_HYPERLINK):
            anchor = link.get(W_ANCHOR)
            if anchor and anchor.lower() in renamed:
                link.set(W_ANCHOR, renamed[anchor.lower()])
                work.dirty.add(part)
        for instruction in _field_structure(root)[0]:
            new = reference.sub(swap, instruction.text)
            if new != instruction.text:
                _write_instruction(instruction.instr, new)
                work.dirty.add(part)
        for element in root.iter(W_FLDSIMPLE):
            old = element.get(W_INSTR) or ""
            new = reference.sub(swap, old)
            if new != old:
                element.set(W_INSTR, new)
                work.dirty.add(part)
    return len(renamed)


def _scrub_instruction(text: str, matcher: _ValueMatcher) -> str:
    candidates = (text, unquote(text)) if "%" in text else (text,)
    for candidate in candidates:
        new, values = matcher.sub("REDACTED", candidate)
        new, emails = _EMAIL.subn("REDACTED", new) if "@" in new else (new, 0)
        if values or emails:
            return new
    return text


@dataclass(slots=True, eq=False)
class _Field:
    """A complex field: its ``w:instrText`` elements (the instruction is their joined text)."""

    instr: list[Element] = field(default_factory=list)
    in_result: bool = False  # past the "separate" marker

    @property
    def text(self) -> str:
        return "".join(element.text or "" for element in self.instr)


_Chain = tuple  # (field, enclosing chain) | None: the fields an element is a result of


def _field_structure(root: Element) -> tuple[list[_Field], dict[Element, _Chain]]:
    """Complex fields of one part, in document order (they nest and may span paragraphs).

    Returns the fields and, for every text / character element lying in a field's
    *result* (between ``separate`` and ``end``), the chain of fields it is a result of
    (see :func:`_chain_fields`). Chains are shared, so unclosed or deeply nested
    fields cost linear time and memory.
    """
    fields: list[_Field] = []
    stack: list[_Field] = []
    chains: list[_Chain | None] = []  # chains[i]: result chain while stack[i] is innermost
    results: dict[Element, _Chain] = {}
    for node in root.iter(W_FLDCHAR, W_INSTRTEXT, W_T, M_T, *_CHAR_PIECES):
        if node.tag == W_FLDCHAR:
            kind = node.get(W_FLDCHARTYPE)
            if kind == "begin":
                fields.append(_Field())
                stack.append(fields[-1])
                chains.append(chains[-1] if chains else None)
            elif kind == "separate" and stack:
                if not stack[-1].in_result:
                    stack[-1].in_result = True
                    chains[-1] = (stack[-1], chains[-2] if len(chains) > 1 else None)
            elif kind == "end" and stack:
                stack.pop()
                chains.pop()
        elif node.tag == W_INSTRTEXT:
            if stack and not stack[-1].in_result:
                stack[-1].instr.append(node)
            else:  # instruction outside any open field: treat as a field of its own
                fields.append(_Field([node]))
        elif chains and chains[-1] is not None:
            results[node] = chains[-1]
    return fields, results


def _chain_fields(chain: _Chain | None) -> Iterable[_Field]:
    """The fields of a result chain, innermost first."""
    while chain is not None:
        yield chain[0]
        chain = chain[1]


def _write_instruction(elements: list[Element], text: str) -> None:
    """Write a rewritten instruction back over its elements.

    Only the changed middle (after the common prefix, before the common suffix) is
    rewritten, so text on either side of a nested field stays on its own side.
    """
    pieces, position = [], 0
    for element in elements:
        length = len(element.text or "")
        pieces.append(_Piece(element, position, position + length, True))
        position += length
    old = "".join(element.text or "" for element in elements)
    prefix = 0
    while prefix < min(len(old), len(text)) and old[prefix] == text[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(text)) - prefix and old[-1 - suffix] == text[-1 - suffix]:
        suffix += 1
    if old:
        if prefix >= len(old) - suffix:  # text was only inserted: widen by one unchanged character to anchor it
            prefix, suffix = (prefix - 1, suffix) if prefix else (prefix, suffix - 1)
        _rewrite(pieces, [(prefix, len(old) - suffix, text[prefix:len(text) - suffix])])
        return
    _set_text(elements[0], text)  # nothing was there before


def _scrub_fields(work: _Working, matcher: _ValueMatcher) -> int:
    """Scrub redacted values and e-mail addresses from field codes.

    The instruction of a complex field is scrubbed as one string, so a value split
    over several ``w:instrText`` elements is still found.
    """
    matcher = matcher.for_markup()  # field switches and bookmark ids hold short numbers
    scrubbed = 0
    for part, _ in work.text_parts:
        root = work.trees[part]
        for instruction in _field_structure(root)[0]:
            old = instruction.text
            new = _scrub_instruction(old, matcher)
            if new != old:
                _write_instruction(instruction.instr, new)
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


def _neutralise_hyperlink_instruction(instruction: str) -> str:
    """Replace the URL argument of a HYPERLINK instruction (switches and their arguments kept)."""
    if (head := _HYPERLINK_FIELD.match(instruction)) is None:
        return instruction
    skip = False
    for token in _FIELD_TOKEN.finditer(instruction, head.end()):
        text = token.group()
        if text.startswith("\\"):
            skip = text.lower() in _SWITCHES_WITH_ARGUMENT
            continue
        if skip:
            skip = False
            continue
        if text.strip('"') == REDACTED_URL:
            return instruction
        return f'{instruction[:token.start()]}"{REDACTED_URL}"{instruction[token.end():]}'
    return instruction


def _hyperlink_fields_touched(
    paragraph: _Paragraph, edits: list[Edit], results: dict[Element, _Chain]
) -> tuple[list[_Field], list[Element]]:
    """HYPERLINK fields (complex, ``w:fldSimple``) whose *result* text overlaps an edit."""
    complex_fields: dict[_Field, None] = {}
    simple_fields: dict[Element, None] = {}
    ends = [piece.end for piece in paragraph.pieces]
    for start, end, _ in edits:
        for i in _overlapping(paragraph.pieces, ends, start, end):
            element = paragraph.pieces[i].element
            for instruction in _chain_fields(results.get(element)):
                if _HYPERLINK_FIELD.match(instruction.text):
                    complex_fields[instruction] = None
            for ancestor in element.iterancestors(W_FLDSIMPLE):
                if _HYPERLINK_FIELD.match(ancestor.get(W_INSTR) or ""):
                    simple_fields[ancestor] = None
    return list(complex_fields), list(simple_fields)


def _unlink_hyperlink_fields(
    work: _Working, complex_fields: dict[_Field, str], simple_fields: dict[Element, str]
) -> int:
    """Point flagged HYPERLINK fields at a dummy URL, like flagged ``w:hyperlink`` elements."""
    unlinked = 0
    for instruction, part in complex_fields.items():
        old = instruction.text
        if (new := _neutralise_hyperlink_instruction(old)) != old:
            _write_instruction(instruction.instr, new)
            work.dirty.add(part)
            unlinked += 1
    for element, part in simple_fields.items():
        old = element.get(W_INSTR) or ""
        if (new := _neutralise_hyperlink_instruction(old)) != old:
            element.set(W_INSTR, new)
            work.dirty.add(part)
            unlinked += 1
    return unlinked


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
                target = work.package.find(rel.get("Target", ""))
                if target and target.lower().startswith("docprops/"):  # never delete other parts
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
    """Copy the entry header; the per-entry comment is dropped on purpose (it can hold names)."""
    clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    clone.compress_type = info.compress_type
    clone.external_attr = info.external_attr
    clone.create_system = info.create_system
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
        flagged_fields: dict[_Field, str] = {}
        flagged_simple: dict[Element, str] = {}
        field_results: dict[str, dict[Element, _Chain]] = {}
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
            if segment.part not in field_results:
                field_results[segment.part] = _field_structure(work.trees[segment.part])[1]
            touched_fields, touched_simple = _hyperlink_fields_touched(
                paragraph, edits, field_results[segment.part]
            )
            flagged_fields.update(dict.fromkeys(touched_fields, segment.part))
            flagged_simple.update(dict.fromkeys(touched_simple, segment.part))
            _rewrite(paragraph.pieces, edits)
            work.dirty.add(segment.part)
            by_location[segment.location] += len(edits)

        matcher = _ValueMatcher.for_redaction(values, keep_values)
        bookmarks = _rename_bookmarks(work, matcher)  # before the text passes, which would overwrite the names
        hyperlinks = _scrub_hyperlinks(work, matcher, flagged)
        hyperlinks += _unlink_hyperlink_fields(work, flagged_fields, flagged_simple)
        fields = _scrub_fields(work, matcher)
        cleared, authors, removed = _scrub_metadata(work) if scrub_metadata else ([], 0, set())
        residual_fixes = _verify_residuals(work, matcher)  # always last, before zipping

        report = {
            "replacements_applied": sum(by_location.values()),
            "by_location": dict(by_location),
            "hyperlinks_unlinked": hyperlinks,
            "fields_scrubbed": fields,
            "bookmarks_renamed": bookmarks,
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
        try:
            root = _parse_xml(name, package.raw[name])
        except DocxError:
            if _is_optional_xml(name, package.content_type(name)):
                continue
            raise
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
        try:
            root = _parse_xml(name, package.raw[name])
        except DocxError:
            if _is_optional_xml(name, package.content_type(name)):
                continue
            raise
        findings.extend(_scan_tree(name, root, matcher, fix=False))
    return findings
