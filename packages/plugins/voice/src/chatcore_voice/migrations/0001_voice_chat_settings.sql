CREATE TABLE voice_chat_settings (
    chat_key text PRIMARY KEY,
    transcribe boolean NOT NULL DEFAULT true,
    wake_word_enabled boolean NOT NULL DEFAULT false,
    wake_words text[] NOT NULL DEFAULT '{}',
    voice_reply boolean NOT NULL DEFAULT true,
    show_transcript boolean,
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
