-- The gateway reports the schema version on /healthz, so its role may read which
-- migrations have been applied. Read only, like the rest of its access.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'gateway_app') THEN
        GRANT SELECT ON schema_migrations TO gateway_app;
    END IF;
END
$$;
