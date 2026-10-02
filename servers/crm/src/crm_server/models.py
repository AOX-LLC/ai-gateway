"""What CRM tools accept and return. Fictional data throughout.

Inputs forbid extra fields and bound every value, so each schema is closed (see
mcp_common.schema_contract). Outputs are an explicit allowlist: a field that is not
declared here cannot reach a client, which is how the internal columns stay internal.
Money is an integer number of cents.
"""

from datetime import date, datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, Field

from mcp_common.notice import FictionalOutput
from mcp_common.toolset import NO_CONTROL_CHARACTERS, StrictInput

Tier = Literal["bronze", "silver", "gold"]
Stage = Literal["prospecting", "proposal", "negotiation", "won", "lost"]
NoteKind = Literal["call", "email", "meeting", "note"]
Region = Literal[
    "northeast", "mid-atlantic", "southeast", "great-lakes", "gulf-coast", "pacific-northwest"
]
REGIONS: tuple[Region, ...] = get_args(Region)
"""The one list of regions: the type below and the seed both come from it."""

AccountId = Annotated[str, Field(pattern=r"^ACC-[0-9]{5}$", max_length=9)]
StaffHandle = Annotated[str, Field(pattern=r"^[a-z]+\.[a-z]+$", max_length=40)]

MAX_NOTES_RETURNED = 10


class SearchAccountsInput(BaseModel):
    model_config = StrictInput

    query: str = Field(min_length=2, max_length=100, pattern=NO_CONTROL_CHARACTERS)
    tier: Tier | None = None
    region: Region | None = None
    limit: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=10000)


class GetAccountInput(BaseModel):
    model_config = StrictInput

    account_id: AccountId


class ListDealsInput(BaseModel):
    model_config = StrictInput

    account_id: AccountId | None = None
    stage: Stage | None = None
    limit: int = Field(default=10, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=10000)


class AccountSummary(BaseModel):
    id: str
    name: str
    industry: str
    region: Region
    tier: Tier
    account_manager: str


class SearchAccountsOutput(FictionalOutput):
    accounts: list[AccountSummary]


class Contact(BaseModel):
    id: str
    full_name: str
    title: str
    email: str
    phone: str


class ActivityNote(BaseModel):
    id: int
    kind: NoteKind
    author: str
    deal_id: str | None
    body: str
    occurred_at: datetime


class AccountDetail(FictionalOutput):
    id: str
    name: str
    industry: str
    region: Region
    tier: Tier
    account_manager: str
    about: str
    created_at: datetime
    contacts: list[Contact]
    notes: list[ActivityNote]
    """The newest activity notes (at most 10), newest first."""
    notes_total: int
    """How many notes the account has, so a client can tell when some are omitted."""


class Deal(BaseModel):
    id: str
    account_id: str
    name: str
    stage: Stage
    amount_cents: int
    """The deal's value in cents."""
    close_date: date
    owner: str


class ListDealsOutput(FictionalOutput):
    deals: list[Deal]
