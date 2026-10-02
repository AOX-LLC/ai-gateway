"""The handbook documents, chunking and tools, against the seeded database as handbook_app."""

import re
import tomllib
from collections.abc import AsyncIterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, TextContent
from psycopg_pool import AsyncConnectionPool

from handbook_server.documents import (
    CHUNK_OVERLAP,
    CHUNK_TARGET,
    Document,
    DocumentFileError,
    chunk_document,
    load_documents,
    parse_document,
)
from handbook_server.embedding import Embedder, vector_literal
from handbook_server.models import SNIPPET_MAX_CHARS
from handbook_server.repo import HandbookRepo, snippet
from handbook_server.seed import Dataset
from handbook_server.tools import INSTRUCTIONS, build_toolset
from mcp_common.notice import FICTIONAL_NOTICE
from mcp_common.schema_contract import check_input_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_FILE = REPO_ROOT / "servers" / "handbook" / "evals" / "retrieval.toml"
RESTRICTED = {"DOC-023", "DOC-024", "DOC-030"}
CODE_PHRASES = ["HERON-LANTERN-7", "PELICAN-VESPER-31", "KESTREL-TALLOW-58"]


# --- the documents (no database needed) ----------------------------------------------------


def test_there_are_thirty_documents_and_three_are_restricted() -> None:
    documents = load_documents()

    assert [d.id for d in documents] == [f"DOC-{n:03d}" for n in range(1, 31)]
    assert {d.id for d in documents if d.classification == "restricted"} == RESTRICTED
    assert {d.category for d in documents} == {"hr", "returns", "shipping", "security", "expenses"}


def test_every_title_is_plain_text_without_quotes() -> None:
    documents = {d.id: d for d in load_documents()}

    assert documents["DOC-016"].title == "Shipping service levels: quick reference"
    for document in documents.values():
        assert not document.title.startswith(('"', "'"))
        assert not document.title.endswith(('"', "'"))
        assert document.body.startswith(f"# {document.title}")


def test_a_document_without_front_matter_is_refused() -> None:
    with pytest.raises(DocumentFileError, match="front matter"):
        parse_document("# Just a heading\n\n## Section\n\ntext")


def test_a_document_with_an_unknown_category_is_refused() -> None:
    source = "---\nid: DOC-001\ntitle: T\ncategory: legal\nclassification: general\n"
    source += "updated: 2026-01-01\n---\n# T\n\n## S\n\nbody\n"

    with pytest.raises(ValueError, match="category"):
        parse_document(source)


def test_chunks_are_cut_at_the_second_level_headings_and_carry_them() -> None:
    document = parse_document(
        "---\nid: DOC-001\ntitle: Paid time off\ncategory: hr\nclassification: general\n"
        "updated: 2026-01-01\n---\n# Paid time off\n\n## Accrual\n\nOne day a month.\n\n"
        "## Carry over\n\nFive days.\n"
    )

    chunks = chunk_document(document)

    assert [(c.ordinal, c.heading, c.text) for c in chunks] == [
        (0, "Accrual", "One day a month."),
        (1, "Carry over", "Five days."),
    ]


def test_a_long_section_is_cut_with_an_overlap_and_every_chunk_keeps_the_heading() -> None:
    sentences = [f"Sentence number {n} says something about the policy." for n in range(60)]
    text = " ".join(sentences)
    document = Document("DOC-001", "T", "hr", "general", __import__("datetime").date(2026, 1, 1),
                        f"# T\n\n## Long section\n\n{text}\n")  # fmt: skip

    chunks = chunk_document(document)

    assert len(chunks) > 3
    assert {c.heading for c in chunks} == {"Long section"}
    assert all(len(c.text) <= CHUNK_TARGET for c in chunks)
    for earlier, later in pairwise(chunks):
        tail = earlier.text[-CHUNK_OVERLAP:]
        # The next chunk begins inside the previous one, on a word boundary.
        assert later.text.split(" ")[0] in tail
        assert later.text[:20] in earlier.text
    assert " ".join(c.text for c in chunks).count("Sentence number 59") >= 1
    assert chunks[-1].text.endswith("policy.")


def test_every_packaged_document_is_chunked_with_its_headings() -> None:
    for document in load_documents():
        chunks = chunk_document(document)
        headings = re.findall(r"^## (.+)$", document.body, re.MULTILINE)
        assert chunks, document.id
        assert {c.heading for c in chunks} <= set(headings) | {document.title}
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_a_snippet_is_cut_to_400_characters_at_a_word() -> None:
    long = "word " * 200

    cut = snippet(long)

    assert len(cut) <= SNIPPET_MAX_CHARS
    assert cut.endswith("…")
    assert snippet("short  text\nhere") == "short text here"


def test_the_instructions_say_the_data_is_fictional() -> None:
    assert "fictional" in INSTRUCTIONS


# --- embeddings --------------------------------------------------------------------------------


def test_embeddings_are_unit_length_deterministic_and_256_wide(embedder: Embedder) -> None:
    first = embedder.embed(["vacation days", "return an order"])
    second = embedder.embed(["vacation days", "return an order"])

    assert first == second
    for vector in first:
        assert vector is not None
        assert len(vector) == 256
        assert sum(v * v for v in vector) == pytest.approx(1.0, abs=1e-4)


def test_a_text_with_no_known_tokens_has_no_vector(embedder: Embedder) -> None:
    assert embedder.embed([""]) == [None]


def test_a_vector_literal_is_pgvectors_text_form() -> None:
    assert vector_literal([0.5, -1.0, 0.0]) == "[0.5,-1,0]"


# --- the tools -----------------------------------------------------------------------------------


@pytest.fixture
async def client(handbook_pool: AsyncConnectionPool, embedder: Embedder) -> AsyncIterator[Client]:
    server = build_toolset(HandbookRepo(handbook_pool, embedder)).build_server(
        "handbook", "0", INSTRUCTIONS
    )
    async with Client(server, mode="legacy") as connected:
        yield connected


def _text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


@pytest.mark.integration
@pytest.mark.anyio
class TestTools:
    async def test_two_read_only_tools_with_closed_schemas(self, client: Client) -> None:
        tools = (await client.list_tools()).tools

        assert {t.name for t in tools} == {"search", "get_document"}
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)
        for tool in tools:
            assert check_input_schema(tool.input_schema) == [], tool.name

    async def test_search_returns_ranked_documents_with_a_snippet_and_the_notice(
        self, client: Client
    ) -> None:
        found = _structured(await client.call_tool("search", {"query": "vacation days"}))

        results = found["results"]
        assert found["notice"] == FICTIONAL_NOTICE
        assert 1 <= len(results) <= 5
        assert [r["rank"] for r in results] == list(range(1, len(results) + 1))
        assert set(results[0]) == {
            "document_id", "title", "category", "heading", "snippet", "rank",
        }  # fmt: skip
        assert all(len(r["snippet"]) <= 400 for r in results)
        assert len({r["document_id"] for r in results}) == len(results)

    async def test_search_finds_the_time_off_document_for_a_keyword(self, client: Client) -> None:
        results = _structured(await client.call_tool("search", {"query": "parental leave"}))[
            "results"
        ]

        assert "DOC-002" in [r["document_id"] for r in results[:3]]

    async def test_search_can_be_limited_to_a_category(self, client: Client) -> None:
        results = _structured(
            await client.call_tool(
                "search", {"query": "policy", "category": "expenses", "limit": 10}
            )
        )["results"]

        assert results
        assert {r["category"] for r in results} == {"expenses"}

    async def test_search_limit_is_honoured(self, client: Client) -> None:
        for limit in (1, 3, 10):
            results = _structured(
                await client.call_tool("search", {"query": "shipping policy", "limit": limit})
            )["results"]
            assert len(results) <= limit
        assert len(results) == 10

    @pytest.mark.parametrize(
        "arguments",
        [
            {"query": "a"},
            {"query": "x" * 201},
            {"query": "returns", "limit": 0},
            {"query": "returns", "limit": 11},
            {"query": "returns", "category": "legal"},
            {"query": "returns", "offset": 1},
        ],
    )
    async def test_search_refuses_out_of_bounds_values(
        self, client: Client, arguments: dict[str, Any]
    ) -> None:
        with pytest.raises(MCPError) as error:
            await client.call_tool("search", arguments)

        assert error.value.code == INVALID_PARAMS

    async def test_a_query_of_only_stop_words_returns_a_result_list_not_an_error(
        self, client: Client
    ) -> None:
        found = _structured(await client.call_tool("search", {"query": "the and of"}))

        assert isinstance(found["results"], list)

    async def test_get_document_returns_the_text_of_a_published_document(
        self, client: Client, handbook_data: Dataset
    ) -> None:
        document = next(d for d in handbook_data.documents if d.id == "DOC-016")

        found = _structured(await client.call_tool("get_document", {"document_id": "DOC-016"}))

        assert found["notice"] == FICTIONAL_NOTICE
        assert found["title"] == "Shipping service levels: quick reference"
        assert found["category"] == "shipping"
        assert found["updated"] == "2025-12-01"
        assert found["body"] == document.body

    async def test_a_restricted_document_looks_exactly_like_one_that_does_not_exist(
        self, client: Client
    ) -> None:
        for restricted_id, missing_id in zip(
            sorted(RESTRICTED), ["DOC-998", "DOC-997", "DOC-996"], strict=True
        ):
            restricted = await client.call_tool("get_document", {"document_id": restricted_id})
            missing = await client.call_tool("get_document", {"document_id": missing_id})

            assert restricted.is_error is True
            assert missing.is_error is True
            assert restricted.structured_content == missing.structured_content
            assert _text(restricted).replace(restricted_id, "ID") == (
                _text(missing).replace(missing_id, "ID")
            )
            assert _text(restricted) == f"Document '{restricted_id}' was not found."

    @pytest.mark.parametrize("document_id", ["DOC-1", "doc-001", "DOC-0001", "DOC-001; DROP", ""])
    async def test_get_document_refuses_a_malformed_id(
        self, client: Client, document_id: str
    ) -> None:
        with pytest.raises(MCPError) as error:
            await client.call_tool("get_document", {"document_id": document_id})

        assert error.value.code == INVALID_PARAMS

    @pytest.mark.parametrize("tool", ["search", "get_document"])
    async def test_an_unknown_argument_is_refused(self, client: Client, tool: str) -> None:
        arguments = {"query": "returns"} if tool == "search" else {"document_id": "DOC-001"}

        with pytest.raises(MCPError) as error:
            await client.call_tool(tool, {**arguments, "classification": "restricted"})

        assert error.value.code == INVALID_PARAMS

    async def test_no_search_ever_returns_a_restricted_document_or_its_text(
        self, client: Client, handbook_data: Dataset
    ) -> None:
        probes = [
            "wire transfer verification callback",
            "incident escalation roster",
            "vendor payment approval thresholds",
            *CODE_PHRASES,
            *[d.title for d in handbook_data.documents if d.id in RESTRICTED],
        ]
        for query in probes:
            for category in (None, "security", "expenses"):
                arguments: dict[str, Any] = {"query": query, "limit": 10}
                if category:
                    arguments["category"] = category
                found = _structured(await client.call_tool("search", arguments))
                assert RESTRICTED.isdisjoint(r["document_id"] for r in found["results"]), query
                text = str(found)
                assert not [phrase for phrase in CODE_PHRASES if phrase in text], query

    async def test_the_view_hides_restricted_documents_from_the_role(
        self, handbook_app_url: str, handbook_data: Dataset
    ) -> None:
        async with await psycopg.AsyncConnection.connect(handbook_app_url) as connection:
            await connection.execute("SET search_path TO handbook")
            cursor = await connection.execute(
                "SELECT count(*) FROM published_documents"
                " WHERE id = ANY(%s) OR classification = 'restricted'",
                (sorted(RESTRICTED),),
            )
            hidden = await cursor.fetchone()
            cursor = await connection.execute("SELECT count(*) FROM searchable_chunks")
            chunks = await cursor.fetchone()
            cursor = await connection.execute(
                "SELECT count(*) FROM searchable_chunks WHERE document_id = ANY(%s)",
                (sorted(RESTRICTED),),
            )
            restricted_chunks = await cursor.fetchone()

        published = [c for c in handbook_data.chunks if c.document_id not in RESTRICTED]
        assert hidden == (0,)
        assert restricted_chunks == (0,)
        assert chunks == (len(published),)


# --- the retrieval eval ------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.anyio
async def test_retrieval_recall_at_3_meets_the_threshold_and_no_probe_leaks(
    client: Client, handbook_data: Dataset
) -> None:
    spec = tomllib.loads(EVAL_FILE.read_text(encoding="utf-8"))
    threshold = float(spec["threshold"])
    pairs = spec["pair"]
    ranks = []
    for pair in pairs:
        found = _structured(await client.call_tool("search", {"query": pair["query"], "limit": 10}))
        ids = [r["document_id"] for r in found["results"]]
        positions = [ids.index(e) + 1 for e in pair["expected"] if e in ids]
        ranks.append(min(positions) if positions else None)
    passed = [rank is not None and rank <= 3 for rank in ranks]
    recall = sum(passed) / len(pairs)

    print("\nretrieval eval: recall@3 per pair (rank of the best expected document)")
    for pair, rank, ok in zip(pairs, ranks, passed, strict=True):
        shown = "not in the top 10" if rank is None else f"rank {rank}"
        print(f"  {'PASS' if ok else 'FAIL'}  {shown:<18} {pair['expected']}  {pair['query']}")
    print(f"retrieval eval: recall@3 = {sum(passed)}/{len(pairs)} = {recall:.3f}"
          f" (threshold {threshold})")  # fmt: skip

    assert recall >= threshold

    restricted_texts = handbook_data.restricted_texts()
    for probe in spec["restricted_probe"]:
        found = _structured(
            await client.call_tool("search", {"query": probe["query"], "limit": 10})
        )
        everything = str(found)
        assert probe["target"] not in [r["document_id"] for r in found["results"]]
        assert RESTRICTED.isdisjoint(r["document_id"] for r in found["results"])
        assert not [text for text in restricted_texts if text in everything]
