# Integrating with the gateway: the 09 helper API

How the helper API of the 09 integrated demo (a separate service that triages a customer's message
with a model) uses this gateway: which client it is, what that client may do, how it gets its token,
and what the classifier needs from it. Everything here is the fictional Harborline Supply Co. and
its synthetic data. Nothing in it is a real customer, and nothing that 09 sends should be.

## The client

| | |
| --- | --- |
| Name | `harborline-helper-api` |
| Registered by | `docker compose run --rm -T admin seed-demo` (the fictional demo stack) |
| Endpoint | `http://127.0.0.1:4401/mcp` (MCP over streamable HTTP, protocol 2025-11-25), or the SSH forward or TLS front end in [Reaching the gateway from another machine](architecture.md#reaching-the-gateway-from-another-machine) |
| Auth | `Authorization: Bearer <token>` on every request, over a connection no one else can read |

### Scopes

Four tools, and the gateway lists exactly these to the client (anything else is not offered and is
refused if called):

| Tool | Effect | Why |
| --- | --- | --- |
| `crm__search_accounts` | read | find the account a message is about |
| `crm__get_account` | read | its contacts and notes |
| `crm__list_deals` | read | the account's deals |
| `tickets__create_ticket` | write | open one ticket for the person to handle |

`tickets__add_comment` is deliberately **not** in the scope. If the 09 story needs the helper to
comment on a ticket, say so in the project chat and the scope is widened in `DEMO_SCOPES_HELPER_API`
(`gateway/src/ai_gateway/admin/cli.py`) with the matching approval role (already `approver`).

### Caps a normal triage never meets

The allowlist (`config/allowlist.toml`) and the rate limits (`config/rate_limits.toml`) are tuned for
one message at a time:

| Rule | Value | Notes |
| --- | --- | --- |
| `crm__search_accounts`, `crm__list_deals`: `limit` | 1 to 5, **required** | a call that leaves it out gets the server's default of 10 and is refused |
| the same two: `offset` | 0 to 20 | a triage does not page |
| `tickets__create_ticket`: `description` | at most 1000 characters | the server allows 4000 |
| `crm__get_account` | 30 calls an hour per client | a triage reads one or two accounts |
| every write | needs a person's approval | `gateway-approver`; the demo's auto-approver is test tooling |

A call over a cap is refused with `Request blocked by gateway policy.` and no reason (the reason is
in the gateway's own records: the dashboard shows which layer stopped it). Send `limit` on every
search and deals call.

## Token delivery

- `seed-demo` prints a JSON object of tokens (`{"harborline-helper-api": "gw_...", ...}`) once, to
  stdout. Redirect it to a file that is not in git and mode 0600, read the helper's token from it, and
  delete the file. Running `seed-demo` again revokes the old tokens and issues new ones.
- For anything longer-lived, issue and rotate by hand: `gateway-admin token-issue --client
  harborline-helper-api --expires-in-days 30`, `token-rotate --client harborline-helper-api` (the old
  token keeps working for a grace period), `token-revoke <lookup id>`. A token is shown once and is
  stored only as a hash.
- 09 reads the token from its own environment or secret store (`GATEWAY_TOKEN`), never from a command
  line, a log or the repository. A token that is wrong, revoked or expired gets `401` with a
  `WWW-Authenticate: Bearer` header, and repeated failures lock that token id out for a while.

## What the layers do with the helper's traffic

Every call passes the same layers as any client's (see [The pipeline](architecture.md#the-pipeline)).
The ones 09 will notice:

- **classifier** judges the free text of results the helper reads and of the arguments of the ticket
  it writes. A text it judges to be an injection refuses the call. In `replay` mode (the default) it
  answers from recordings, see below.
- **egress** refuses a write that carries five or more customer values (ids, emails, phone numbers)
  that **the client** read in the last 30 minutes (in any of its MCP sessions: the ledger is per client,
  so opening a session per call changes nothing), or ten across one session's writes (that session is
  then refused every later write), or ten across all the client's writes in the window. Open the ticket
  with the account id (which is exempt) and a summary in your own words, not a paste of the contacts.
  A busy client that cites many read values in its tickets will meet the window's limit
  (`per_window` in `config/egress.toml`): say so, and it is raised for that client's traffic.
- **canary** refuses any write that carries a decoy value seeded in the data.
- **schema** refuses arguments that do not fit the tool's published input schema.

The acceptance test of all this is `scripts/run_redteam_check.sh`: a planted ticket tells an assistant
to export every customer and a scripted client does it, and the layers stop it
(`scripts/redteam/`, run in the CI stack and by hand).

## Replay recordings

The classifier calls a small model once for each piece of free text, and by default the stack never
calls one: `AGENT_CORE_MODE=replay` answers from the recordings in `config/recordings/`, so the demo
runs with no API key and bills nothing. The cost of that is that **a text with no recording is not
judged**: the call goes on and is recorded as `unclassified` (never as clean), and the dashboard
counts such calls apart.

So every text 09 will send through the gateway needs a recording:

1. Add the texts (the triage ticket's subject and description, the comments, the customer email as a
   ticket's text) to the classifier corpus, in `config/classifier_corpus/story_09.toml`, each with its
   `surface` (`tool_result` for what a read returns, `tool_arguments` for what a write sends), its
   `expect` and its `catchers` (see `config/classifier_corpus/README.md` for the rule).
2. `uv run scripts/record_classifier.py --count` says how many calls that needs and what they cost.
3. A person with an API key records them: `AGENT_CORE_MODE=record AGENT_CORE_ANTHROPIC_API_KEY=...
   uv run scripts/record_classifier.py`. It records only what is missing.
4. `uv run scripts/record_classifier.py --verify` replays every text and must report no miss; a test
   (`test_classifier_recordings.py`) fails the build on a missing recording.
5. Commit `config/recordings/`.

A text must match its recording exactly (it is hashed), so an email with a timestamp in it or a ticket
number that changes on every run will never be found: keep the demo's strings fixed.

In a deployment, set `AGENT_CORE_MODE=live` and a key: then an error or a timeout from the model
refuses the call (`classifier_unavailable`) instead of letting it through, and the spend limits in
`config/classifier.toml` apply: each client's share of the hour's billed spend and the gateway-wide
ceiling (replayed calls cost nothing and do not count).

## Checking the integration

```sh
docker compose up -d --build --wait
# the acceptance test: it registers the demo clients and an approver for the run itself, makes an
# honest triage as the helper (a search with limit 5, an account read, a deals read, one ticket), then
# the export attack in both modes, and removes what it made. The demo approver is the caller's to allow.
LAB_AUTO_APPROVE=yes scripts/run_redteam_check.sh
```

(`seed-demo` prints a new set of tokens and revokes the old ones each time it runs, so running it
yourself first and then the check would revoke the tokens you saved.)

The dashboard (http://127.0.0.1:4400) shows each of these calls, the layer that stopped any of them,
and the tokens and cost of the classifier's calls (replayed ones are marked "replayed, not billed").
