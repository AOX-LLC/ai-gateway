-- Harborline Supply Co. is fictional. This schema belongs to the ticketing server alone:
-- the server's role (ticketing_app) reads it and makes a few narrow writes, and no other
-- role can see it (harborline-setup grants that access). The mcp-common runner runs this with the ticketing schema first on
-- the search path.

CREATE SEQUENCE ticket_number;

CREATE TABLE staff (
    handle       text PRIMARY KEY CHECK (handle ~ '^[a-z]+\.[a-z]+$'),
    display_name text NOT NULL,
    team         text NOT NULL
);

CREATE TABLE tickets (
    id             text        PRIMARY KEY
                               DEFAULT 'TKT-' || lpad(nextval('ticket_number')::text, 6, '0')
                               CHECK (id ~ '^TKT-[0-9]{6}$'),
    -- ACC-00001..ACC-00040 is the fictional account range the CRM server shares.
    account_id     text        NOT NULL CHECK (account_id ~ '^ACC-[0-9]{5}$'),
    subject        text        NOT NULL CHECK (char_length(subject) BETWEEN 3 AND 120),
    description    text        NOT NULL CHECK (char_length(description) BETWEEN 1 AND 4000),
    status         text        NOT NULL DEFAULT 'open'
                               CHECK (status IN ('open', 'pending', 'resolved', 'closed')),
    priority       text        NOT NULL DEFAULT 'normal'
                               CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    assignee       text        REFERENCES staff (handle),
    -- Attribution only: the client name the gateway reported, or 'direct'.
    requested_by   text        NOT NULL,
    -- Staff-only. Never returned by any tool.
    internal_notes text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tickets_by_account ON tickets (account_id);
CREATE INDEX tickets_by_status ON tickets (status);

CREATE TABLE comments (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id    text        NOT NULL REFERENCES tickets (id),
    author       text        NOT NULL,
    -- Internal comments are staff-only and never returned by any tool.
    visibility   text        NOT NULL CHECK (visibility IN ('public', 'internal')),
    body         text        NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
    requested_by text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX comments_by_ticket ON comments (ticket_id);

-- No grants here: harborline-setup applies the ticketing_app role's grants on every run
-- (see harborline_setup.ticketing), so they cannot depend on when the role was created.
