-- The client registry: who may call the gateway, with which credentials, for which
-- tools, and which upstream MCP servers the gateway proxies.

CREATE TABLE clients (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text        NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9-]{1,62}$'),
    description text        NOT NULL DEFAULT '',
    status      text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Only the SHA-256 of a token is stored. lookup_id is the public part of the token
-- and finds the row; the hash is then compared in constant time.
CREATE TABLE client_tokens (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id    uuid        NOT NULL REFERENCES clients (id) ON DELETE CASCADE,
    lookup_id    text        NOT NULL UNIQUE CHECK (lookup_id ~ '^[a-z2-7]{8}$'),
    token_sha256 bytea       NOT NULL CHECK (octet_length(token_sha256) = 32),
    label        text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    revoked_at   timestamptz,
    last_used_at timestamptz
);

CREATE INDEX client_tokens_unrevoked_by_client
    ON client_tokens (client_id)
    WHERE revoked_at IS NULL;

-- A scope is one exposed tool name, '<namespace>__<tool>'. There is deliberately no
-- foreign key to a tool table: tools come from upstream servers at runtime, and a
-- grant must survive its upstream being down.
CREATE TABLE client_scopes (
    client_id  uuid        NOT NULL REFERENCES clients (id) ON DELETE CASCADE,
    tool       text        NOT NULL CHECK (
                               tool ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*__[A-Za-z0-9_-]+$'
                               AND length(tool) <= 64
                           ),
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, tool)
);

-- A namespace never contains '__' and never ends in '_', so splitting an exposed
-- tool name on its first '__' is unambiguous.
CREATE TABLE upstream_servers (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace          text        NOT NULL UNIQUE CHECK (
                                       namespace ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'
                                       AND length(namespace) <= 24
                                   ),
    url                text        NOT NULL CHECK (url ~ '^https?://'),
    enabled            boolean     NOT NULL DEFAULT true,
    connect_timeout_ms integer     NOT NULL DEFAULT 5000
                                   CHECK (connect_timeout_ms BETWEEN 100 AND 60000),
    call_timeout_ms    integer     NOT NULL DEFAULT 30000
                                   CHECK (call_timeout_ms BETWEEN 100 AND 600000),
    -- The NAME of an environment variable holding the upstream credential, never the value.
    credential_env     text        CHECK (credential_env ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- The gateway itself connects as gateway_app, which may read the registry and record
-- when a token was last used, nothing else. The role exists in the Compose stack but
-- not necessarily in a bare test database.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'gateway_app') THEN
        GRANT SELECT ON clients, client_tokens, client_scopes, upstream_servers TO gateway_app;
        GRANT UPDATE (last_used_at) ON client_tokens TO gateway_app;
    END IF;
END
$$;
