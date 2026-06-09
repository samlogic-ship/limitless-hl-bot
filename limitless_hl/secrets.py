from __future__ import annotations

import os
import subprocess


def get_secret(name: str, *, service: str | None = None, account: str | None = None) -> str | None:
    env_value = os.environ.get(name)
    if env_value:
        return env_value.strip()
    keychain_service = service or _default_service(name)
    if keychain_service is None:
        return None
    keychain_account = account or name
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", keychain_account, "-s", keychain_service, "-w"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def _default_service(name: str) -> str | None:
    return {
        "LIMITLESS_TOKEN_ID": "limitless-token-id",
        "LIMITLESS_TOKEN_SECRET": "limitless-token-secret",
        "LIMITLESS_PRIVATE_KEY": "limitless-private-key",
    }.get(name)
