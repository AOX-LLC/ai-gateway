"""Queries against the handbook schema, as the handbook_app role.

The role can read two views and nothing else, and both leave restricted documents out. The
queries also say `classification <> 'restricted'` themselves, inside every step that ranks
or limits, so the exclusion happens before ranking and LIMIT and never afterwards: a
filter applied after would leak through rank order, counts and truncated result sets.
"""

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from handbook_server.embedding import Embedder, vector_literal
from handbook_server.models import (
    SNIPPET_MAX_CHARS,
    DocumentOutput,
    SearchInput,
    SearchResult,
)
from mcp_common.toolset import ToolError

CANDIDATES = 20
"""How many chunks each of the two rankings contributes before they are fused."""
RRF_K = 60
"""The constant of reciprocal rank fusion: a rank r scores 1 / (RRF_K + r)."""

# Reciprocal rank fusion of a meaning ranking (cosine distance to the query's embedding)
# and a keyword ranking (ts_rank of a web-style query), over chunks; then each document is
# represented by its best chunk, and the best documents are returned.
#
# Both rankings scan the view's rows (about 150 chunks, roughly a millisecond); no index
# helps. The view is a security barrier and `tsv @@ q` is not leakproof, so the planner
# cannot use a GIN index on tsv through it (migration 0002 dropped the one that sat unused).
# Revisit when the handbook grows to thousands of chunks, without removing the barrier.
_SEARCH = """
WITH semantic AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> %(vector)s::handbook.vector, id) AS r
    FROM searchable_chunks
    WHERE classification <> 'restricted'
      AND %(use_vector)s
      AND (%(category)s::text IS NULL OR category = %(category)s)
    ORDER BY embedding <=> %(vector)s::handbook.vector, id
    LIMIT %(candidates)s
), lexical AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank(tsv, q) DESC, id) AS r
    FROM searchable_chunks, websearch_to_tsquery('english', %(query)s) AS q
    WHERE classification <> 'restricted'
      AND tsv @@ q
      AND (%(category)s::text IS NULL OR category = %(category)s)
    ORDER BY ts_rank(tsv, q) DESC, id
    LIMIT %(candidates)s
), fused AS (
    SELECT id, sum(1.0 / (%(k)s + r)) AS score
    FROM (SELECT id, r FROM semantic UNION ALL SELECT id, r FROM lexical) AS ranked
    GROUP BY id
), best_per_document AS (
    SELECT DISTINCT ON (c.document_id)
           c.id, c.document_id, c.title, c.category, c.heading, c.text, fused.score
    FROM fused
    JOIN searchable_chunks AS c ON c.id = fused.id
    WHERE c.classification <> 'restricted'
    ORDER BY c.document_id, fused.score DESC, c.id
)
SELECT document_id, title, category, heading, text, score
FROM best_per_document
ORDER BY score DESC, id
LIMIT %(limit)s
"""

_GET_DOCUMENT = """
SELECT id, title, category, updated, body
FROM published_documents
WHERE id = %s AND classification <> 'restricted'
"""


def document_not_found(document_id: str) -> ToolError:
    """The one answer for an unknown id and for a restricted one, so the two cannot be told
    apart."""
    return ToolError(f"Document '{document_id}' was not found.")


def snippet(text: str) -> str:
    """The passage cut to SNIPPET_MAX_CHARS, at a word boundary, with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= SNIPPET_MAX_CHARS:
        return flat
    cut = flat[: SNIPPET_MAX_CHARS - 1].rsplit(" ", 1)[0]
    return cut + "…"


class HandbookRepo:
    def __init__(self, pool: AsyncConnectionPool, embedder: Embedder) -> None:
        self._pool = pool
        self._embedder = embedder

    async def search(self, query: SearchInput) -> list[SearchResult]:
        (vector,) = self._embedder.embed([query.query])
        parameters = {
            "vector": vector_literal(vector) if vector is not None else None,
            "use_vector": vector is not None,
            "query": query.query,
            "category": query.category,
            "candidates": CANDIDATES,
            "k": RRF_K,
            "limit": query.limit,
        }
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_SEARCH, parameters)
            rows = await cursor.fetchall()
        return [
            SearchResult(
                document_id=row["document_id"],
                title=row["title"],
                category=row["category"],
                heading=row["heading"],
                snippet=snippet(row["text"]),
                rank=rank,
            )
            for rank, row in enumerate(rows, start=1)
        ]

    async def get_document(self, document_id: str) -> DocumentOutput:
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_GET_DOCUMENT, (document_id,))
            row = await cursor.fetchone()
        if row is None:
            raise document_not_found(document_id)
        return DocumentOutput.model_validate(row)
