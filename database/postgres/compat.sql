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

-- SQLite compares a TEXT column with a plain number as text (the number takes the column's
-- TEXT affinity); PostgreSQL has no text-vs-number operators. These follow SQLite's rule.
DO $$
DECLARE number_type text; op record;
BEGIN
    FOREACH number_type IN ARRAY ARRAY['bigint', 'integer', 'numeric'] LOOP
        FOR op IN SELECT * FROM (VALUES ('=', 'eq', '=', '<>'), ('<>', 'ne', '<>', '='),
                                        ('<', 'lt', '>', '>='), ('<=', 'le', '>=', '>'),
                                        ('>', 'gt', '<', '<='), ('>=', 'ge', '<=', '<')) AS v(sym, name, commutator, negator) LOOP
            IF NOT EXISTS (SELECT 1 FROM pg_operator WHERE oprcode <> 0 AND oprname = op.sym AND oprleft = 'text'::regtype
                           AND oprright = number_type::regtype) THEN
                EXECUTE format('CREATE OR REPLACE FUNCTION sqlite_%1$s(text, %2$s) RETURNS boolean LANGUAGE sql IMMUTABLE AS $f$ SELECT $1 %3$s $2::text $f$', op.name, number_type, op.sym);
                EXECUTE format('CREATE OR REPLACE FUNCTION sqlite_%1$s(%2$s, text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $f$ SELECT $1::text %3$s $2 $f$', op.name, number_type, op.sym);
                EXECUTE format('CREATE OPERATOR %1$s (LEFTARG = text, RIGHTARG = %2$s, FUNCTION = sqlite_%3$s, COMMUTATOR = %4$s, NEGATOR = %5$s)', op.sym, number_type, op.name, op.commutator, op.negator);
                EXECUTE format('CREATE OPERATOR %1$s (LEFTARG = %2$s, RIGHTARG = text, FUNCTION = sqlite_%3$s, COMMUTATOR = %4$s, NEGATOR = %5$s)', op.sym, number_type, op.name, op.commutator, op.negator);
            END IF;
        END LOOP;
    END LOOP;
END $$;
