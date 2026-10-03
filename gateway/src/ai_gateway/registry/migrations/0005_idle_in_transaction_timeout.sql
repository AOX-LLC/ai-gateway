-- A session of the gateway's own role that sits inside a transaction doing nothing is ended after
-- 30 s: an idle transaction holds its locks. (db/init/01-roles.sh sets this when the volume is
-- created; this covers volumes that already exist.)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_app') THEN
        ALTER ROLE gateway_app SET idle_in_transaction_session_timeout = '30000ms';
    END IF;
END $$;
