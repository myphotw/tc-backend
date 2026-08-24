-- PREPARED ONLY: do not run without a PostgreSQL backup and maintenance approval.
-- The curation projection does not depend on this constraint.

-- 1) Preflight. This must return zero rows before VALIDATE CONSTRAINT.
SELECT relation.id, relation.file_id, relation.memorykeeper_tag_id
FROM common_file_tags AS relation
LEFT JOIN mk_tags AS tag ON tag.id = relation.memorykeeper_tag_id
WHERE relation.memorykeeper_tag_id IS NOT NULL
  AND tag.id IS NULL
ORDER BY relation.id;

-- 2) Short catalog lock to add an unvalidated constraint, then online validation.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30min';

DO $$
DECLARE
    fk_name text;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM common_file_tags AS relation
        LEFT JOIN mk_tags AS tag ON tag.id = relation.memorykeeper_tag_id
        WHERE relation.memorykeeper_tag_id IS NOT NULL
          AND tag.id IS NULL
    ) THEN
        RAISE EXCEPTION 'orphan memorykeeper_tag_id rows exist; aborting';
    END IF;

    SELECT constraint_row.conname
    INTO fk_name
    FROM pg_constraint AS constraint_row
    WHERE constraint_row.contype = 'f'
      AND constraint_row.conrelid = 'common_file_tags'::regclass
      AND constraint_row.confrelid = 'mk_tags'::regclass
      AND constraint_row.conkey = ARRAY[
          (
              SELECT attribute_row.attnum
              FROM pg_attribute AS attribute_row
              WHERE attribute_row.attrelid = 'common_file_tags'::regclass
                AND attribute_row.attname = 'memorykeeper_tag_id'
                AND NOT attribute_row.attisdropped
          )
      ]::smallint[]
    LIMIT 1;

    IF fk_name IS NULL THEN
        fk_name := 'common_file_tags_memorykeeper_tag_id_fkey';
        EXECUTE
            'ALTER TABLE common_file_tags '
            'ADD CONSTRAINT common_file_tags_memorykeeper_tag_id_fkey '
            'FOREIGN KEY (memorykeeper_tag_id) REFERENCES mk_tags(id) NOT VALID';
    END IF;

    EXECUTE format(
        'ALTER TABLE common_file_tags VALIDATE CONSTRAINT %I',
        fk_name
    );
END $$;
COMMIT;

-- 3) Postflight: confirm the FK definition and validation state.
SELECT conname, convalidated, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'common_file_tags'::regclass
  AND contype = 'f';
