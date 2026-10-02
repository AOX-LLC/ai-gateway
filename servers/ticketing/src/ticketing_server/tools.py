"""The ticketing tools: six, strict, and the only way anything reads or writes tickets."""

from mcp_common.toolset import CallInfo, StrictToolset
from ticketing_server.models import (
    AddCommentInput,
    AssignInput,
    Assignment,
    ChangeStatusInput,
    CommentRef,
    CreateTicketInput,
    GetTicketInput,
    ListTicketsInput,
    ListTicketsOutput,
    StatusChange,
    TicketDetail,
    TicketRef,
)
from ticketing_server.repo import TicketRepo

INSTRUCTIONS = (
    "Support tickets of Harborline Supply Co., a fictional company; all data is synthetic."
    " Comments you add are public. Internal staff notes are never available."
)


def build_toolset(repo: TicketRepo) -> StrictToolset:
    toolset = StrictToolset()

    async def list_tickets(arguments: ListTicketsInput, info: CallInfo) -> ListTicketsOutput:
        return ListTicketsOutput(tickets=await repo.list_tickets(arguments))

    async def get_ticket(arguments: GetTicketInput, info: CallInfo) -> TicketDetail:
        return await repo.get_ticket(arguments.ticket_id)

    async def create_ticket(arguments: CreateTicketInput, info: CallInfo) -> TicketRef:
        return await repo.create_ticket(
            arguments.subject,
            arguments.description,
            arguments.priority,
            arguments.account_id,
            info.requested_by,
        )

    async def add_comment(arguments: AddCommentInput, info: CallInfo) -> CommentRef:
        return await repo.add_comment(arguments.ticket_id, arguments.body, info.requested_by)

    async def change_status(arguments: ChangeStatusInput, info: CallInfo) -> StatusChange:
        return await repo.change_status(arguments.ticket_id, arguments.status)

    async def assign(arguments: AssignInput, info: CallInfo) -> Assignment:
        return await repo.assign(arguments.ticket_id, arguments.assignee)

    toolset.register(
        "list_tickets",
        "List support tickets, newest activity first, optionally filtered by status,"
        " priority or account.",
        ListTicketsInput,
        ListTicketsOutput,
        list_tickets,
        read_only=True,
    )
    toolset.register(
        "get_ticket",
        "Get one ticket with its public comments.",
        GetTicketInput,
        TicketDetail,
        get_ticket,
        read_only=True,
    )
    toolset.register(
        "create_ticket",
        "Open a new support ticket for an account.",
        CreateTicketInput,
        TicketRef,
        create_ticket,
        read_only=False,
    )
    toolset.register(
        "add_comment",
        "Add a public comment to a ticket.",
        AddCommentInput,
        CommentRef,
        add_comment,
        read_only=False,
    )
    toolset.register(
        "change_status",
        "Set a ticket's status.",
        ChangeStatusInput,
        StatusChange,
        change_status,
        read_only=False,
    )
    toolset.register(
        "assign",
        "Assign a ticket to a staff member by handle.",
        AssignInput,
        Assignment,
        assign,
        read_only=False,
    )
    return toolset
