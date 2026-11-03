ALTER TABLE messages ADD COLUMN username text;
ALTER TABLE messages ADD COLUMN mentions jsonb NOT NULL DEFAULT '[]'::jsonb;
