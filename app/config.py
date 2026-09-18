"""
Application configuration loaded from environment variables.

Required env vars:
  GEMINI_API_KEY  – Google AI Studio / Vertex API key.

Optional env vars:
  LLM_MODEL       – Model name (default: gemini-2.0-flash).
  HOST             – Bind address (default: 0.0.0.0).
  PORT             – Bind port (default: 8000).
  LLM_MAX_RETRIES  – How many times to retry a failed LLM call (default: 2).
"""

import os

from dotenv import load_dotenv

load_dotenv()  # reads .env if present

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-3.7-flash")
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))
LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "2"))

