"""The handbook seed: the documents, chunked and embedded.

Harborline Supply Co. does not exist; the documents were written for this repository. The
seed is deterministic: the documents are files, the chunking is a fixed rule and the model
is pinned and has no randomness, so every run produces the same rows. harborline-setup
computes the embeddings here, once; the server embeds only each query.
"""

from dataclasses import dataclass, field
from pathlib import Path

from psycopg import AsyncConnection

from handbook_server.embedding import Embedder, vector_literal
from harborline_setup.handbook_documents import Document, chunk_document, load_documents


@dataclass(frozen=True)
class ChunkRow:
    document_id: str
    ordinal: int
    heading: str
    text: str
    embedding: str
    """pgvector's text form of the unit-length vector."""


@dataclass
class Dataset:
    documents: list[Document] = field(default_factory=list)
    chunks: list[ChunkRow] = field(default_factory=list)

    def restricted_ids(self) -> set[str]:
        return {d.id for d in self.documents if d.classification == "restricted"}

    def restricted_texts(self) -> list[str]:
        """Every value that must never appear in a tool output: each restricted document's
        text and title, and each of its chunks."""
        restricted = self.restricted_ids()
        values = [d.body for d in self.documents if d.id in restricted]
        values += [d.title for d in self.documents if d.id in restricted]
        values += [c.text for c in self.chunks if c.document_id in restricted]
        return values

    def restricted_headings(self) -> list[str]:
        """The headings of the restricted documents' chunks that no published chunk shares.
        A heading such as "Purpose" is in published documents too, so seeing it in an output
        proves nothing; the rest are specific to a restricted document."""
        restricted = self.restricted_ids()
        published = {c.heading for c in self.chunks if c.document_id not in restricted}
        return sorted({c.heading for c in self.chunks if c.document_id in restricted} - published)


def embedding_text(document: Document, heading: str, text: str) -> str:
    """What is embedded for a chunk: the document title and the heading give a short passage
    the context it lacks on its own."""
    return f"{document.title}. {heading}. {text}"


def build_dataset(embedder: Embedder, documents_path: Path) -> Dataset:
    documents = load_documents(documents_path)
    pending = [(document, chunk) for document in documents for chunk in chunk_document(document)]
    vectors = embedder.embed(
        [embedding_text(document, chunk.heading, chunk.text) for document, chunk in pending]
    )
    rows = []
    for (document, chunk), vector in zip(pending, vectors, strict=True):
        if vector is None:
            raise ValueError(f"{document.id} chunk {chunk.ordinal} has no known tokens")
        rows.append(
            ChunkRow(
                chunk.document_id, chunk.ordinal, chunk.heading, chunk.text, vector_literal(vector)
            )
        )
    return Dataset(documents, rows)


async def is_seeded(connection: AsyncConnection) -> bool:
    cursor = await connection.execute("SELECT EXISTS (SELECT FROM documents)")
    row = await cursor.fetchone()
    return bool(row and row[0])


async def insert_dataset(connection: AsyncConnection, dataset: Dataset) -> None:
    """Write the dataset in one transaction."""
    async with connection.transaction(), connection.cursor() as cursor:
        await cursor.executemany(
            "INSERT INTO documents"
            " (id, title, category, classification, updated, body, superseded_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (d.id, d.title, d.category, d.classification, d.updated, d.body, d.superseded_by)
                for d in dataset.documents
            ],
        )
        await cursor.executemany(
            "INSERT INTO chunks (document_id, ordinal, heading, text, embedding)"
            " VALUES (%s, %s, %s, %s, %s::handbook.vector)",
            [(c.document_id, c.ordinal, c.heading, c.text, c.embedding) for c in dataset.chunks],
        )


async def sync_superseded(connection: AsyncConnection, documents_path: Path) -> int:
    """Make each stored document's `superseded_by` match its file, and return how many
    changed. A schema seeded before the column existed has none of the values, and the seed
    only runs when the schema is empty, so setup calls this on every repeat run."""
    documents = load_documents(documents_path)
    changed = 0
    async with connection.transaction(), connection.cursor() as cursor:
        for document in documents:
            await cursor.execute(
                "UPDATE documents SET superseded_by = %s"
                " WHERE id = %s AND superseded_by IS DISTINCT FROM %s",
                (document.superseded_by, document.id, document.superseded_by),
            )
            changed += cursor.rowcount
    return changed
