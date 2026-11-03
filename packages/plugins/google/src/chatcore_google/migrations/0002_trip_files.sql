CREATE TABLE google_trip_files (
    chat_key text NOT NULL,
    file_id text NOT NULL,
    trip_id text NOT NULL,
    url text NOT NULL,
    name text NOT NULL,
    mime text NOT NULL,
    role text NOT NULL CHECK (role IN ('itinerary', 'reference')),
    owner text,
    requester text NOT NULL,
    version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, file_id)
);
CREATE INDEX google_trip_files_trip_idx ON google_trip_files(chat_key, trip_id);
CREATE UNIQUE INDEX google_trip_files_primary_idx ON google_trip_files(chat_key, trip_id)
    WHERE role = 'itinerary';
INSERT INTO google_trip_files(chat_key, file_id, trip_id, url, name, mime, role, owner, requester, version)
SELECT chat_key, file_id, trip_id,
       'https://docs.google.com/spreadsheets/d/' || file_id || '/edit',
       'Legacy linked spreadsheet', 'application/vnd.google-apps.spreadsheet',
       'reference', NULL, requester_key, 'legacy'
FROM google_itinerary_links;
