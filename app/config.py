from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


WINDOW_LABEL_OVERRIDES = {90: '3 months', 180: '6 months', 365: '12 months'}


def format_window_label(days: int) -> str:
    if days in WINDOW_LABEL_OVERRIDES:
        return WINDOW_LABEL_OVERRIDES[days]
    return f'{days} day' if days == 1 else f'{days} days'


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
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: bool = True
    incident_form_url: str = 'https://docs.google.com/forms/d/1kAhrPkpn6kSLvp1n_d-UOCurd5c_X1IGXI-vqyIWBM8'
    excluded_usernames: str = 'dataset_model,lost+found,tempuser,smu'
    low_util_exempt_usernames: str = ''
    allowed_history_windows: List[int] = Field(default_factory=lambda: [1, 3, 7, 14, 30])
    allowed_user_history_windows: List[int] = Field(default_factory=lambda: [1, 3, 7, 14, 30, 90, 180, 365])
    max_custom_history_days: int = 1096
    user_aggregate_retention_days: int = 400

    @field_validator(
        'collector_ssh_username',
        'collector_ssh_password',
        'collector_ssh_key_path',
        'smtp_host',
        'smtp_username',
        'smtp_password',
        'smtp_from_email',
        mode='before',
    )
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
    def user_history_window_options(self) -> list[dict[str, object]]:
        return [{'days': days, 'label': format_window_label(days)} for days in self.allowed_user_history_windows]

    @property
    def user_aggregate_retention(self) -> int:
        """Daily user aggregates are kept longer so multi-month windows have data."""
        return max(self.retention_days, self.user_aggregate_retention_days)

    @property
    def excluded_users(self) -> set[str]:
        return {username.strip() for username in self.excluded_usernames.split(',') if username.strip()}

    @property
    def low_util_exempt_users(self) -> set[str]:
        return {username.strip() for username in self.low_util_exempt_usernames.split(',') if username.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
