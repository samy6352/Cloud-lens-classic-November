"""Deployment-wide ingest policy; staging may still build local database caches."""
import os


def read_only_mode() -> bool:
    return os.getenv("CLOUDLENS_READ_ONLY", "").strip().lower() in {"1", "true", "yes"}


def require_writable() -> None:
    if read_only_mode():
        raise PermissionError("CLOUDLENS_READ_ONLY is enabled; changes and ingest workers are disabled.")
