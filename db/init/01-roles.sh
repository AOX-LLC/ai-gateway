#!/bin/sh
# Runs once, when the postgres data volume is first created.
# Table grants for gateway_app are applied by the gateway's migration, not here.
set -eu

psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_password="$GATEWAY_APP_DB_PASSWORD" \
    -v db_name="$POSTGRES_DB" <<'SQL'
CREATE ROLE gateway_app LOGIN PASSWORD :'app_password';
GRANT CONNECT ON DATABASE :"db_name" TO gateway_app;
GRANT USAGE ON SCHEMA public TO gateway_app;
SQL

psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v owner="$POSTGRES_USER" <<'SQL'
CREATE DATABASE ai_gateway_test OWNER :"owner";
SQL
