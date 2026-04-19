"""
Shared configuration loader — reads config.yaml + .env
"""
import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

load_dotenv()

_config = None


def load_config(path: str = "config.yaml") -> dict:
    global _config
    if _config is not None:
        return _config

    config_path = Path(path)
    if not config_path.exists():
        config_path = Path(__file__).parent / path
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path) as f:
        _config = yaml.safe_load(f)

    # Env overrides
    if os.getenv("AI_PROVIDER"):
        _config["ai"]["provider"] = os.getenv("AI_PROVIDER")
    if os.getenv("PROMETHEUS_URL"):
        _config["prometheus"]["url"] = os.getenv("PROMETHEUS_URL")
    if os.getenv("SLACK_WEBHOOK_URL"):
        _config["slack"]["webhook_url"] = os.getenv("SLACK_WEBHOOK_URL")

    return _config


def get_ai_provider() -> str:
    return load_config()["ai"]["provider"]


def get_anthropic_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment")
    return key


def get_openai_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise ValueError("OPENAI_API_KEY not set in environment")
    return key
