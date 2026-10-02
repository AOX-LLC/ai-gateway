-- credential_env names the environment variable whose value is sent to an upstream as a
-- Bearer token. Any uppercase name was allowed, so whoever could write the registry could
-- point it at another secret (say, the gateway's own database URL) and have it sent to a
-- server they control. Only names ending in _SERVICE_TOKEN are service credentials now.
--
-- A row that already names anything else is disabled and loses the name: it could not be
-- called with that credential any more, and calling it with none would be worse.
UPDATE upstream_servers
SET enabled = false, credential_env = NULL, updated_at = now()
WHERE credential_env IS NOT NULL AND credential_env !~ '_SERVICE_TOKEN$';

ALTER TABLE upstream_servers DROP CONSTRAINT upstream_servers_credential_env_check;
ALTER TABLE upstream_servers ADD CONSTRAINT upstream_servers_credential_env_check
    CHECK (credential_env ~ '^[A-Z][A-Z0-9_]{0,49}_SERVICE_TOKEN$');
