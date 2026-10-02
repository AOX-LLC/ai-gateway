"""What handbook tools accept and return. Fictional data throughout.

Inputs forbid extra fields and bound every value (see mcp_common.schema_contract). Outputs
are an explicit allowlist; restricted documents have no way in, because the database role
cannot read them in the first place.
"""

from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field

from handbook_server.documents import Category
from mcp_common.notice import FictionalOutput
from mcp_common.toolset import StrictInput

DocumentId = Annotated[str, Field(pattern=r"^DOC-[0-9]{3}$", max_length=7)]

SNIPPET_MAX_CHARS = 400


class SearchInput(BaseModel):
    model_config = StrictInput

    query: str = Field(min_length=2, max_length=200)
    category: Category | None = None
    limit: int = Field(default=5, ge=1, le=10)


class GetDocumentInput(BaseModel):
    model_config = StrictInput

    document_id: DocumentId


class SearchResult(BaseModel):
    document_id: str
    title: str
    category: Category
    heading: str
    snippet: str = Field(max_length=SNIPPET_MAX_CHARS)
    """The best-matching passage of the document, cut to 400 characters."""
    rank: int
    """1 for the best match."""


class SearchOutput(FictionalOutput):
    results: list[SearchResult]


class DocumentOutput(FictionalOutput):
    id: str
    title: str
    category: Category
    updated: date
    body: str
