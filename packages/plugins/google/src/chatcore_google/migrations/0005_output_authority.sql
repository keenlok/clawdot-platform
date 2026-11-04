ALTER TABLE google_edit_results
    ADD COLUMN output_owner_id TEXT,
    ADD COLUMN output_owner_email TEXT,
    ADD COLUMN output_thread_id TEXT,
    ADD COLUMN output_mode TEXT;

ALTER TABLE google_edit_results ADD CONSTRAINT google_output_binding_check CHECK (
    (output_owner_id IS NULL AND output_owner_email IS NULL
        AND output_thread_id IS NULL AND output_mode IS NULL)
    OR
    (output_owner_id IS NOT NULL AND char_length(output_owner_id) BETWEEN 1 AND 200
        AND output_owner_email IS NOT NULL
        AND char_length(output_owner_email) BETWEEN 1 AND 320
        AND (output_thread_id IS NULL OR char_length(output_thread_id) >= 0)
        AND output_mode IS NOT NULL
        AND ((kind = 'create_doc' AND output_mode = 'create' AND source_id IS NULL)
            OR (kind = 'edit_doc' AND output_mode IN ('copy', 'docx_import')
                AND source_id IS NOT NULL
                AND char_length(source_id) BETWEEN 1 AND 200)))
);

CREATE INDEX google_output_destination_idx
    ON google_edit_results(chat_key, destination_id)
    WHERE output_owner_id IS NOT NULL AND destination_id IS NOT NULL;
