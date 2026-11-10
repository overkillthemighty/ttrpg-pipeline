"""Settings, read once from environment variables (see .env.example)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None else v.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [x.strip() for x in _str(name).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secret_key: str
    base_url: str
    repo_url: str
    cookie_secure: bool
    session_days: int

    admin_username: str
    admin_password: str

    oidc_enabled: bool
    oidc_discovery_url: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_scopes: str
    oidc_button_label: str
    oidc_username_claim: str
    oidc_groups_claim: str
    oidc_allowed_users: list[str]
    oidc_allowed_groups: list[str]

    nc_url: str
    nc_user: str
    nc_password: str
    nc_results_dir: str
    nc_session_logs_dir: str
    nc_verify_tls: bool
    nc_notify_enabled: bool
    nc_notify_admin_user: str
    nc_notify_admin_password: str
    nc_notify_target_user: str

    whisper_model: str
    whisper_model_cpu: str
    language: str
    device: str
    compute_type: str
    batch_size: int
    threads: int
    hf_token: str
    diarize_model: str

    keep_log_on_success: bool
    result_retention_days: int
    max_file_gb: float
    max_chunk_mb: int

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ttrpg.db"

    @property
    def nextcloud_enabled(self) -> bool:
        return bool(self.nc_url and self.nc_user and self.nc_password)


def _secret(data_dir: Path) -> str:
    env = _str("SECRET_KEY")
    if env:
        return env
    p = data_dir / "secret_key"
    if p.exists():
        return p.read_text().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    p.write_text(key)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return key


def load() -> Settings:
    data_dir = Path(_str("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        secret_key=_secret(data_dir),
        base_url=_str("BASE_URL").rstrip("/"),
        cookie_secure=_bool("SESSION_COOKIE_SECURE", True),
        session_days=_int("SESSION_DAYS", 14),
        admin_username=_str("ADMIN_USERNAME", "admin"),
        admin_password=_str("ADMIN_PASSWORD"),
        oidc_enabled=_bool("OIDC_ENABLED", False),
        oidc_discovery_url=_str("OIDC_DISCOVERY_URL"),
        oidc_client_id=_str("OIDC_CLIENT_ID"),
        oidc_client_secret=_str("OIDC_CLIENT_SECRET"),
        oidc_scopes=_str("OIDC_SCOPES", "openid profile email"),
        oidc_button_label=_str("OIDC_BUTTON_LABEL", "Sign in with Nextcloud"),
        oidc_username_claim=_str("OIDC_USERNAME_CLAIM", "preferred_username"),
        oidc_groups_claim=_str("OIDC_GROUPS_CLAIM", "groups"),
        oidc_allowed_users=_list("OIDC_ALLOWED_USERS"),
        oidc_allowed_groups=_list("OIDC_ALLOWED_GROUPS"),
        repo_url=_str("REPO_URL", "https://github.com/overkillthemighty/ttrpg-pipeline").rstrip("/"),
        nc_url=_str("NEXTCLOUD_URL").rstrip("/"),
        nc_user=_str("NEXTCLOUD_USER"),
        nc_password=_str("NEXTCLOUD_APP_PASSWORD"),
        nc_results_dir=_str("NEXTCLOUD_RESULTS_DIR", "Documents/Riftward").strip("/"),
        nc_session_logs_dir=_str("NEXTCLOUD_SESSION_LOGS_DIR", "Documents/Riftward/Claude/Session Logs").strip("/"),
        nc_verify_tls=_bool("NEXTCLOUD_VERIFY_TLS", True),
        nc_notify_enabled=_bool("NEXTCLOUD_NOTIFY_ENABLED", False),
        nc_notify_admin_user=_str("NEXTCLOUD_NOTIFY_ADMIN_USER"),
        nc_notify_admin_password=_str("NEXTCLOUD_NOTIFY_ADMIN_APP_PASSWORD"),
        nc_notify_target_user=_str("NEXTCLOUD_NOTIFY_TARGET_USER"),
        whisper_model=_str("WHISPER_MODEL", "large-v3"),
        whisper_model_cpu=_str("WHISPER_MODEL_CPU", "small"),
        language=_str("WHISPER_LANGUAGE", "en"),
        device=_str("WHISPER_DEVICE", "auto"),
        compute_type=_str("WHISPER_COMPUTE_TYPE", "auto"),
        batch_size=_int("WHISPER_BATCH_SIZE", 8),
        threads=_int("WHISPER_THREADS", 0),
        hf_token=_str("HF_TOKEN"),
        diarize_model=_str("DIARIZE_MODEL"),
        keep_log_on_success=_bool("KEEP_LOG_ON_SUCCESS", False),
        result_retention_days=_int("RESULT_RETENTION_DAYS", 0),
        max_file_gb=_float("MAX_FILE_GB", 4.0),
        max_chunk_mb=_int("MAX_CHUNK_MB", 64),
    )


settings = load()
