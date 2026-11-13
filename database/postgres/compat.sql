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
