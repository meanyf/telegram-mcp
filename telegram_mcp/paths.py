"""Общие пути проекта, независимые от текущего каталога."""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
TELEGRAM_DATA = PROJECT_DIR / '.local' / 'telegram'
INSTRUCTIONS_PATH = PROJECT_DIR / 'prompts' / 'mcp_instructions.md'
