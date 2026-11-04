ALTER TABLE google_edit_results ALTER COLUMN source_id DROP NOT NULL;
ALTER TABLE google_edit_results DROP CONSTRAINT google_edit_results_kind_check;
ALTER TABLE google_edit_results ADD CONSTRAINT google_edit_results_kind_check
    CHECK (kind IN ('edit_doc', 'edit_sheet', 'browser_edit_doc', 'create_doc'));
ALTER TABLE google_edit_results DROP CONSTRAINT google_edit_results_source_id_check;
ALTER TABLE google_edit_results ADD CONSTRAINT google_edit_results_source_id_check
    CHECK (
        (kind = 'create_doc' AND source_id IS NULL)
        OR (kind <> 'create_doc' AND source_id IS NOT NULL
            AND char_length(source_id) BETWEEN 1 AND 200)
    );
