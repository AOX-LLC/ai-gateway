"""Read the handbook's Markdown documents and cut them into chunks for search.

Setup-only: the documents live in `servers/handbook/documents/`, outside every Python
package and every image, and only the setup container has them mounted. The running
handbook server never imports this module. Each document is `DOC-###.md`: YAML front matter
(id, title, category, classification, updated, and optionally superseded_by), then a `#`
title and `##` sections.
Everything here is fictional (see servers/handbook/data/README.md).
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from handbook_server.models import Category, DocumentId

Classification = Literal["general", "restricted"]

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
    superseded_by: DocumentId | None = None
    """The id of the edition that replaces this one."""


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    category: str
    classification: str
    updated: date
    body: str
    superseded_by: str | None = None


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
        front.id,
        front.title,
        front.category,
        front.classification,
        front.updated,
        body,
        front.superseded_by,
    )


def load_documents(folder: Path) -> list[Document]:
    """Every document in `folder`, in id order."""
    if not folder.is_dir():
        raise DocumentFileError(f"no handbook documents folder at {folder}")
    documents = [
        parse_document(entry.read_text(encoding="utf-8"), entry.name)
        for entry in folder.iterdir()
        if re.fullmatch(r"DOC-\d{3}\.md", entry.name)
    ]
    documents.sort(key=lambda document: document.id)
    ids = [document.id for document in documents]
    if len(ids) != len(set(ids)):
        raise DocumentFileError(f"duplicate document ids in {ids}")
    _check_supersession(documents)
    return documents


def _check_supersession(documents: list[Document]) -> None:
    """Every `superseded_by` names a published document that is itself current, so a reader
    who follows it lands on the edition that applies and never on a restricted one."""
    by_id = {document.id: document for document in documents}
    for document in documents:
        if document.superseded_by is None:
            continue
        target = by_id.get(document.superseded_by)
        if target is None:
            raise DocumentFileError(
                f"{document.id} is superseded by {document.superseded_by}, which does not exist"
            )
        if target.id == document.id:
            raise DocumentFileError(f"{document.id} cannot supersede itself")
        if target.classification == "restricted":
            raise DocumentFileError(f"{document.id} is superseded by a restricted document")
        if target.superseded_by is not None:
            raise DocumentFileError(
                f"{document.id} is superseded by {target.id}, which is itself superseded"
                f" by {target.superseded_by}; name the current edition"
            )


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
