-- Whether a tool reads or writes is decided here, by a reviewed policy, never by the
-- upstream's own annotations: an upstream that is wrong or hostile can claim any hint.
-- A tool with no row here is treated as a write (fail closed).
CREATE TABLE tool_policies (
    namespace   text        NOT NULL CHECK (
                                namespace ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'
                                AND length(namespace) <= 24
                            ),
    -- The upstream's own tool name, without the namespace prefix.
    tool        text        NOT NULL CHECK (tool ~ '^[A-Za-z0-9_-]{1,64}$'),
    effect      text        NOT NULL CHECK (effect IN ('read', 'write')),
    notes       text        NOT NULL DEFAULT '',
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, tool)
);

-- Read only, like the rest of the gateway role's access to the registry.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'gateway_app') THEN
        GRANT SELECT ON tool_policies TO gateway_app;
    END IF;
END
$$;
