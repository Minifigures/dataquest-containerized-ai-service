"""Configuration, read from the environment with local defaults.

Nothing here reads a file. A container gets its settings from the environment,
and a platform that assigns a port communicates it the same way, so the
environment is the single source of truth.
"""

import os

APP_NAME = "Prompt Optimizer API"
APP_VERSION = "1.0.0"

# Cloud platforms assign a port and pass it in PORT. The fallback is for local
# development only. See the Dockerfile for why reading this correctly needs a
# shell form CMD.
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")

DATABASE_URL = os.getenv("DATABASE_URL", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("MODEL", "gpt-4o-mini")

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "4000"))


def has_llm_credentials():
    return bool(OPENAI_API_KEY)
