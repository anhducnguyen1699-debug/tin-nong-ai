"""Nạp cấu hình từ thư mục config/ và prompts/."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict:
    return load_yaml("settings.yaml")


def load_sources() -> dict:
    return load_yaml("sources.yaml")


def load_profile() -> str:
    return (CONFIG_DIR / "profile.md").read_text(encoding="utf-8")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def system_prompt(step: str) -> str:
    """Hướng dẫn hệ thống của một bước = vai trò của bước đó + hồ sơ người đọc."""
    return f"{load_prompt(step)}\n\n---\n\n{load_profile()}"
