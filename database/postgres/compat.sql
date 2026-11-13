-- SQLite catalog look-alike for code that inspects the schema (sqlite_master reads).
-- Applied after schema.sql and triggers.sql.
CREATE OR REPLACE VIEW sqlite_master AS
    SELECT 'table'::text AS type, c.relname::text AS name, c.relname::text AS tbl_name,
           'CREATE TABLE ' || c.relname AS sql
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    UNION ALL
    SELECT 'view', c.relname, c.relname, pg_get_viewdef(c.oid)
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'v' AND c.relname <> 'sqlite_master'
    UNION ALL
    SELECT 'index', i.indexname, i.tablename, i.indexdef
      FROM pg_indexes i WHERE i.schemaname = 'public'
    UNION ALL
    SELECT 'trigger', t.tgname, c.relname, pg_get_triggerdef(t.oid)
      FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND NOT t.tgisinternal;

-- SQLite compares a TEXT column with a number as text (type affinity); PostgreSQL has no
-- text = bigint operator. These keep SQLite's rule for = and <> only.
DO $$
DECLARE number_type text;
BEGIN
    FOREACH number_type IN ARRAY ARRAY['bigint', 'integer', 'numeric'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_operator WHERE oprname = '=' AND oprleft = 'text'::regtype
                       AND oprright = number_type::regtype) THEN
            EXECUTE format('CREATE FUNCTION sqlite_eq(text, %1$s) RETURNS boolean LANGUAGE sql IMMUTABLE AS $f$ SELECT $1 = $2::text $f$', number_type);
            EXECUTE format('CREATE FUNCTION sqlite_eq(%1$s, text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $f$ SELECT $1::text = $2 $f$', number_type);
            EXECUTE format('CREATE FUNCTION sqlite_ne(text, %1$s) RETURNS boolean LANGUAGE sql IMMUTABLE AS $f$ SELECT $1 <> $2::text $f$', number_type);
            EXECUTE format('CREATE FUNCTION sqlite_ne(%1$s, text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $f$ SELECT $1::text <> $2 $f$', number_type);
            EXECUTE format('CREATE OPERATOR = (LEFTARG = text, RIGHTARG = %1$s, FUNCTION = sqlite_eq, COMMUTATOR = =, NEGATOR = <>)', number_type);
            EXECUTE format('CREATE OPERATOR = (LEFTARG = %1$s, RIGHTARG = text, FUNCTION = sqlite_eq, COMMUTATOR = =, NEGATOR = <>)', number_type);
            EXECUTE format('CREATE OPERATOR <> (LEFTARG = text, RIGHTARG = %1$s, FUNCTION = sqlite_ne, COMMUTATOR = <>, NEGATOR = =)', number_type);
            EXECUTE format('CREATE OPERATOR <> (LEFTARG = %1$s, RIGHTARG = text, FUNCTION = sqlite_ne, COMMUTATOR = <>, NEGATOR = =)', number_type);
        END IF;
    END LOOP;
END $$;
