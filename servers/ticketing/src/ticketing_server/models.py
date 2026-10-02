"""What tools accept and return. Fictional data throughout.

Inputs forbid extra fields and bound every value, so each schema is closed (see
mcp_common.schema_contract). Outputs are an explicit allowlist: a field that is not
declared here cannot reach a client, which is how internal notes stay internal.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from mcp_common.toolset import StrictInput

Status = Literal["open", "pending", "resolved", "closed"]
Priority = Literal["low", "normal", "high", "urgent"]

TicketId = Annotated[str, Field(pattern=r"^TKT-[0-9]{6}$", max_length=10)]
AccountId = Annotated[str, Field(pattern=r"^ACC-[0-9]{5}$", max_length=9)]
StaffHandle = Annotated[str, Field(pattern=r"^[a-z]+\.[a-z]+$", max_length=40)]


class ListTicketsInput(BaseModel):
    model_config = StrictInput

    status: Status | None = None
    priority: Priority | None = None
    account_id: AccountId | None = None
    limit: int = Field(default=10, ge=1, le=20)


class GetTicketInput(BaseModel):
    model_config = StrictInput

    ticket_id: TicketId


class CreateTicketInput(BaseModel):
    model_config = StrictInput

    subject: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    priority: Priority = "normal"
    account_id: AccountId


class AddCommentInput(BaseModel):
    model_config = StrictInput

    ticket_id: TicketId
    body: str = Field(min_length=1, max_length=2000)


class ChangeStatusInput(BaseModel):
    model_config = StrictInput

    ticket_id: TicketId
    status: Status


class AssignInput(BaseModel):
    model_config = StrictInput

    ticket_id: TicketId
    assignee: StaffHandle


class TicketSummary(BaseModel):
    id: str
    account_id: str
    subject: str
    status: Status
    priority: Priority
    assignee: str | None
    created_at: datetime
    updated_at: datetime


class PublicComment(BaseModel):
    id: int
    author: str
    body: str
    created_at: datetime


class TicketDetail(TicketSummary):
    description: str
    requested_by: str
    comments: list[PublicComment]


class ListTicketsOutput(BaseModel):
    tickets: list[TicketSummary]


class TicketRef(BaseModel):
    ticket_id: str


class CommentRef(TicketRef):
    comment_id: int


class StatusChange(TicketRef):
    status: Status


class Assignment(TicketRef):
    assignee: str
