CREATE TABLE tutor_profiles (
    user_key text PRIMARY KEY,
    target text NOT NULL CHECK (char_length(target) <= 40),
    level text NOT NULL CHECK (level IN ('A1','A2','B1','B2','C1','C2')),
    explain text NOT NULL CHECK (char_length(explain) <= 40),
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (target <> explain)
);
