"""Optional Google owner identity and bounded data limits."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GoogleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOOGLE_", extra="ignore")
    owner_email: str = ""
    oauth_credentials_file: str = ""
    write_user_ids: str = ""
    max_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    max_chars: int = Field(default=30_000, ge=1000, le=100_000)
    max_rows: int = Field(default=300, ge=1, le=1000)
    max_columns: int = Field(default=32, ge=8, le=100)
    max_cell_chars: int = Field(default=5000, ge=100, le=20_000)
    max_tabs: int = Field(default=32, ge=1, le=100)
    suggestion_rows_per_call: int = Field(default=20, ge=1, le=100)
    suggestion_rows_per_day: int = Field(default=100, ge=1, le=1000)
    max_edit_cells: int = Field(default=100, ge=1, le=100)
    edit_unresolved_retention_days: int = Field(default=90, ge=90, le=3650)
    copy_qualified: bool = False
    timeout_s: float = Field(default=15, ge=1, le=45)

    @property
    def configured(self) -> bool:
        return bool(self.owner_email and self.oauth_credentials_file)

    @property
    def writers(self) -> frozenset[str]:
        return frozenset(part.strip() for part in self.write_user_ids.split(",") if part.strip())
