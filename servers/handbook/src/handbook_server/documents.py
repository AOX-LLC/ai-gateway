"""Read the handbook's Markdown documents and cut them into chunks for search.

Each document is `documents/DOC-###.md`: YAML front matter (id, title, category,
classification, updated), then a `#` title and `##` sections. Everything here is
fictional (see data/README.md).
"""

import re
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Category = Literal["hr", "returns", "shipping", "security", "expenses"]
Classification = Literal["general", "restricted"]
DocumentId = Annotated[str, Field(pattern=r"^DOC-[0-9]{3}$", max_length=7)]

CHUNK_TARGET = 800
"""Characters a chunk aims at; a section shorter than this stays whole."""
CHUNK_OVERLAP = 120
"""Characters repeated at the start of the next chunk of a long section."""

_FRONT_MATTER = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^## +(?P<heading>.+?) *$", re.MULTILINE)


class DocumentFileError(ValueError):
    """A handbook file is malformed."""


class Front(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: DocumentId
    title: str = Field(min_length=1, max_length=200)
    category: Category
    classification: Classification
    updated: date


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    category: str
    classification: str
    updated: date
    body: str


@dataclass(frozen=True)
class Chunk:
    document_id: str
    ordinal: int
    heading: str
    text: str


def parse_document(source: str, name: str = "document") -> Document:
    """A document from its Markdown text. The front matter is real YAML, so a quoted title
    loses its quotes."""
    match = _FRONT_MATTER.match(source)
    if match is None:
        raise DocumentFileError(f"{name}: no front matter")
    raw: Any = yaml.safe_load(match["yaml"])
    if not isinstance(raw, dict):
        raise DocumentFileError(f"{name}: the front matter is not a mapping")
    front = Front.model_validate(raw)
    body = match["body"].strip()
    if not body:
        raise DocumentFileError(f"{name}: no body")
    return Document(
        front.id, front.title, front.category, front.classification, front.updated, body
    )


def load_documents() -> list[Document]:
    """Every packaged document, in id order."""
    folder = files("handbook_server").joinpath("documents")
    documents = [
        parse_document(entry.read_text(encoding="utf-8"), entry.name)
        for entry in folder.iterdir()
        if re.fullmatch(r"DOC-\d{3}\.md", entry.name)
    ]
    documents.sort(key=lambda document: document.id)
    ids = [document.id for document in documents]
    if len(ids) != len(set(ids)):
        raise DocumentFileError(f"duplicate document ids in {ids}")
    return documents


def chunk_document(document: Document) -> list[Chunk]:
    """Split at `##` headings, then cut a long section into pieces of about CHUNK_TARGET
    characters that overlap by about CHUNK_OVERLAP. Every chunk carries its heading; text
    before the first `##` (other than the `#` title line) gets the document title."""
    sections = _sections(document)
    chunks: list[Chunk] = []
    for heading, text in sections:
        for piece in _windows(text):
            chunks.append(Chunk(document.id, len(chunks), heading, piece))
    return chunks


def _sections(document: Document) -> list[tuple[str, str]]:
    matches = list(_SECTION.finditer(document.body))
    preamble_end = matches[0].start() if matches else len(document.body)
    preamble = "\n".join(
        line for line in document.body[:preamble_end].splitlines() if not line.startswith("# ")
    ).strip()
    sections = [(document.title, preamble)] if preamble else []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(document.body)
        text = document.body[match.end() : end].strip()
        if text:
            sections.append((match["heading"], text))
    return sections


def _windows(text: str) -> list[str]:
    if len(text) <= CHUNK_TARGET:
        return [text]
    pieces = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_TARGET, len(text))
        if end < len(text):
            end = _break_before(text, start, end)
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        start = _word_start(text, max(end - CHUNK_OVERLAP, start + 1))
    return pieces


def _break_before(text: str, start: int, end: int) -> int:
    """The end of the last sentence, else the last space, in the second half of the window."""
    floor = start + CHUNK_TARGET // 2
    sentence = text.rfind(". ", floor, end)
    if sentence != -1:
        return sentence + 1
    space = text.rfind(" ", floor, end)
    return space if space != -1 else end


def _word_start(text: str, position: int) -> int:
    """The start of the first whole word at or after `position`."""
    while position < len(text) and not text[position - 1].isspace():
        position += 1
    return position
