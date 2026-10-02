-- Harborline Supply Co. is fictional. This schema belongs to the CRM server alone: the
-- server's role (crm_app) may only SELECT, and only the public columns, and no other role
-- can see it (harborline-setup grants that access on every run). The mcp-common runner runs
-- this with the crm schema first on the search path.

CREATE TABLE accounts (
    -- ACC-00001..ACC-00040 is the fictional account range the ticketing server shares.
    id                    text        PRIMARY KEY CHECK (id ~ '^ACC-[0-9]{5}$'),
    name                  text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    industry              text        NOT NULL,
    region                text        NOT NULL,
    tier                  text        NOT NULL CHECK (tier IN ('bronze', 'silver', 'gold')),
    about                 text        NOT NULL CHECK (char_length(about) BETWEEN 1 AND 4000),
    account_manager       text        NOT NULL CHECK (account_manager ~ '^[a-z]+\.[a-z]+$'),
    created_at            timestamptz NOT NULL,
    -- Internal-only. Never returned by any tool, and not readable by the server's role.
    credit_limit_internal bigint      NOT NULL,
    risk_rating_internal  text        NOT NULL,
    internal_notes        text        NOT NULL
);

CREATE TABLE contacts (
    id         text PRIMARY KEY CHECK (id ~ '^CON-[0-9]{5}$'),
    account_id text NOT NULL REFERENCES accounts (id),
    full_name  text NOT NULL,
    title      text NOT NULL,
    email      text NOT NULL,
    phone      text NOT NULL
);

CREATE INDEX contacts_by_account ON contacts (account_id);

CREATE TABLE deals (
    id                text   PRIMARY KEY CHECK (id ~ '^DEAL-[0-9]{5}$'),
    account_id        text   NOT NULL REFERENCES accounts (id),
    name              text   NOT NULL,
    stage             text   NOT NULL
                             CHECK (stage IN ('prospecting', 'proposal', 'negotiation', 'won', 'lost')),
    amount_cents      bigint NOT NULL CHECK (amount_cents >= 0),
    close_date        date   NOT NULL,
    owner             text   NOT NULL CHECK (owner ~ '^[a-z]+\.[a-z]+$'),
    -- Internal-only. Never returned by any tool, and not readable by the server's role.
    floor_price_cents bigint NOT NULL
);

CREATE INDEX deals_by_account ON deals (account_id);
CREATE INDEX deals_by_close_date ON deals (close_date DESC, id DESC);

CREATE TABLE activity_notes (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id  text        NOT NULL REFERENCES accounts (id),
    deal_id     text        REFERENCES deals (id),
    author      text        NOT NULL CHECK (author ~ '^[a-z]+\.[a-z]+$'),
    kind        text        NOT NULL CHECK (kind IN ('call', 'email', 'meeting', 'note')),
    body        text        NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
    occurred_at timestamptz NOT NULL
);

CREATE INDEX activity_notes_by_account ON activity_notes (account_id, occurred_at DESC, id DESC);

-- No grants here: harborline-setup applies the crm_app role's grants on every run
-- (see harborline_setup.crm), so they cannot depend on when the role was created.
