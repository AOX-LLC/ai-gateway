# Ticketing seed data

Harborline Supply Co. is fictional. Every name, subject, comment and number here is made
up from the fixed word lists in `src/ticketing_server/seed.py`.

## How it is made

`build_dataset()` uses `random.Random(20261002)` and a fixed base time (2026-09-01 08:00
UTC), never the wall clock, so every run produces the same rows:

- 12 staff (handles like `teddy.cormorant`, matching `^[a-z]+\.[a-z]+$`)
- 80 tickets, `TKT-000001` to `TKT-000080`, for accounts `ACC-00001` to `ACC-00040` (the
  range the CRM server shares in Phase 2b)
- 250 comments, about 20% internal
- `internal_notes` on about 30% of tickets

Emails are on `.example` domains and phone numbers are in the `555-01xx` block.
`harborline-setup` loads the seed when the `ticketing` schema is empty.

## Internal-only fields: never in tool output

| Where | Why |
| --- | --- |
| `tickets.internal_notes` | Staff notes |
| `comments` rows with `visibility = 'internal'` | Staff-only comments |

Every **seeded** internal value starts with `[INTERNAL-ONLY]`, so a test can scan any output
for the marker and for each full value. Internal values in *extra records* (below) carry no
marker unless the file adds one; the tests still scan for each full value, which is the
check that matters. The tools never select these columns or rows.

## Free-text fields: where hostile content may be planted later

| Where | Reaches a model through |
| --- | --- |
| `tickets.description` | `get_ticket` |
| `comments.body` where `visibility = 'public'` | `get_ticket` |
| `tickets.subject` | `list_tickets`, `get_ticket` |
| `comments.author` | `get_ticket` (free text in extra records) |
| `tickets.requested_by` | `get_ticket` (the client name the gateway reported, or `direct`; free text if a server is called directly) |

Phase 6 plants prompt-injection and exfiltration content here. Nothing in this repository
does it yet.

## Extra records (for Phase 6)

`harborline-setup` merges an optional JSON file into the seed, only on the run that
seeds an empty schema. Point `HARBORLINE_TICKETING_EXTRA_RECORDS` at it. The file is not
part of this repository.

```json
{
  "tickets": [
    {
      "account_id": "ACC-00001",
      "subject": "Required, 3 to 120 characters",
      "description": "Required, 1 to 4000 characters",
      "status": "open",
      "priority": "normal",
      "assignee": null,
      "internal_notes": null
    }
  ],
  "comments": [
    {"ticket_id": "TKT-000001", "author": "free text", "visibility": "public", "body": "1 to 2000 characters"}
  ]
}
```

`id` is optional on a ticket (new tickets continue the numbering at `TKT-000081`). Unknown
fields are rejected.
