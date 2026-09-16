-- Optional: demote the application from PostgreSQL administrator to a plain role.
--
-- The default deployment registers the application's managed identity as an
-- Entra administrator of the server, because a non-admin identity can only be
-- introduced to PostgreSQL from inside the virtual network. If your environment
-- can reach the server privately -- a jump host, a self-hosted agent, Bastion,
-- or a peered network -- run this instead and remove the administrator entry.
--
-- Run as an Entra administrator, connected to the `cloudlens` database:
--
--   psql "host=<server>.postgres.database.azure.com dbname=cloudlens \
--         user=<your-upn> password=$(az account get-access-token \
--         --resource-type oss-rdbms --query accessToken -o tsv) sslmode=require"
--
-- Then, in the portal or with `az postgres flexible-server ad-admin delete`,
-- remove the managed identity from the server's Entra administrators.

-- :identity_name is the managed identity's name, e.g. id-cloudlens.
SELECT * FROM pgaadauth_create_principal(:'identity_name', false, false);

GRANT CONNECT ON DATABASE cloudlens TO :"identity_name";
GRANT USAGE, CREATE ON SCHEMA public TO :"identity_name";

-- The application creates and replaces its own tables during ingest, so it needs
-- DDL on the schema rather than only DML on today's tables.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"identity_name";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"identity_name";

-- The role that SQL written by the language model is executed as.
--
-- This is the boundary that keeps a model-authored query inside the caller's
-- scope. The text checks in `query()` are a filter on a string and cannot be
-- one: PostgreSQL ships functions that take SQL as a text argument and run it,
-- and a schema qualifier hidden inside a string literal is invisible to any
-- scanner. So the statement runs as a role that owns nothing and is granted
-- nothing beyond the temporary view carrying that request's scope.
--
-- Created here because it has to be. The application creates this role itself
-- when it is a PostgreSQL administrator, which is the default deployment; an
-- application demoted by this script cannot, and refuses to run model queries
-- at all rather than running them with its own rights. Without the two
-- statements below, the assistant on a hardened deployment answers every
-- question about cost data with a configuration error.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cloudlens_query') THEN
        CREATE ROLE cloudlens_query NOLOGIN;
    END IF;
END
$$;

-- SET ROLE is only permitted to a role you are a member of.
GRANT cloudlens_query TO :"identity_name";
