from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/qc_lab"
    app_env: str = "dev"

    # 演示用角色令牌（隔离开发环境，生产须替换为正式鉴权）
    inspector_token: str = "token-inspector-001"
    reviewer_token: str = "token-reviewer-001"
    admin_token: str = "token-admin-001"

    @property
    def role_tokens(self) -> dict[str, str]:
        return {
            "inspector": self.inspector_token,
            "reviewer": self.reviewer_token,
            "admin": self.admin_token,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
