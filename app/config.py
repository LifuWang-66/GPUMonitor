from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'GPU Monitor'
    environment: str = 'development'
    secret_key: str = 'change-me'
    database_url: str = 'sqlite:///./gpu_monitor.db'
    collector_interval_minutes: int = 10
    retention_days: int = 60
    monitor_hosts: str = '10.193.104.165,10.193.104.170,10.193.104.181,10.193.104.182,10.193.104.186'
    monitor_host_aliases: str = 'PZU-104-165,PZU-104-170,PZU-104-181,PZU-104-182,PZU-104-186'
    collector_ssh_username: str | None = None
    collector_ssh_password: str | None = None
    collector_ssh_key_path: str | None = None
    collector_ssh_port: int = 22
    ssh_connect_timeout_seconds: int = 8
    excluded_usernames: str = 'dataset_model,lost+found,tempuser'
    allowed_history_windows: List[int] = Field(default_factory=lambda: [7, 14, 20, 30])

    @field_validator('collector_ssh_username', 'collector_ssh_password', 'collector_ssh_key_path', mode='before')
    @classmethod
    def normalize_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or normalized.lower() in {'none', 'null', 'nil'}:
            return None
        return normalized

    @property
    def hosts(self) -> list[dict[str, str]]:
        addresses = [host.strip() for host in self.monitor_hosts.split(',') if host.strip()]
        aliases = [alias.strip() for alias in self.monitor_host_aliases.split(',') if alias.strip()]
        results: list[dict[str, str]] = []
        for index, address in enumerate(addresses):
            alias = aliases[index] if index < len(aliases) else address
            results.append({'name': alias, 'address': address})
        return results

    @property
    def excluded_users(self) -> set[str]:
        return {username.strip() for username in self.excluded_usernames.split(',') if username.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
