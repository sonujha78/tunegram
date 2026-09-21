import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str = ""


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def load_config() -> Config:
    return Config(
        api_id=int(_require("API_ID")),
        api_hash=_require("API_HASH"),
        bot_token=_require("BOT_TOKEN"),
        session_string=os.getenv("SESSION_STRING", ""),
    )
