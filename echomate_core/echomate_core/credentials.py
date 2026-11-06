import os
from pathlib import Path


CREDENTIALS_DIR = Path(
    os.environ.get("CREDENTIALS_DIRECTORY", "")
).resolve() if os.environ.get("CREDENTIALS_DIRECTORY") else None


def credential(name, fallback_env=None):
    """Read a systemd credential, then fall back to legacy sources."""
    env_name = fallback_env or name
    if CREDENTIALS_DIR:
        path = CREDENTIALS_DIR / name
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
        else:
            if value:
                return value

    if name.startswith("lark_"):
        legacy_name = "LARK_" + name.removeprefix("lark_").upper()
    elif name == "tavily_api_key":
        legacy_name = "TAVILY_API_KEY"
    else:
        legacy_name = env_name.upper()

    if name == "dashscope_api_key":
        legacy_name = "DASHSCOPE_API_KEY"

    value = (os.environ.get(legacy_name) or "").strip()
    if value:
        return value

    if name.startswith("lark_"):
        from dotenv import dotenv_values

        legacy_files = os.environ.get("ECHOMATE_LEGACY_ENV_FILES", "")
        for raw_path in legacy_files.split(os.pathsep):
            if not raw_path:
                continue
            path = Path(raw_path)
            if path.is_file():
                value = (dotenv_values(path).get(legacy_name) or "").strip()
                if value:
                    return value

    return (os.environ.get(env_name) or "").strip()


def config_value(name):
    """Read a non-secret setting from the legacy environment."""
    return (os.environ.get(name) or "").strip()
