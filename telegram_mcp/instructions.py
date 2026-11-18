"""Постоянные инструкции работы и отдельные пожелания пользователя."""

import os
import tempfile

from .paths import INSTRUCTIONS_PATH, PROJECT_DIR, TELEGRAM_DATA

PREFERENCES_PATH = TELEGRAM_DATA / 'preferences.md'
DEFAULT_PREFERENCES_PATH = PROJECT_DIR / 'prompts/default_preferences.md'
MAX_LENGTH = 4000


def read_preferences():
    path = PREFERENCES_PATH if PREFERENCES_PATH.exists() else DEFAULT_PREFERENCES_PATH
    return path.read_text(encoding='utf-8')


def read(default_path=None):
    base = (default_path or INSTRUCTIONS_PATH).read_text(encoding='utf-8').rstrip()
    return base + '\n\nПожелания пользователя к содержанию и оформлению:\n' + read_preferences()


def validate_preferences(text):
    if not text.strip():
        raise ValueError('Пожелания не могут быть пустыми. Напиши, что тебе интересно и как оформить выжимку.')
    if len(text.encode('utf-16-le')) // 2 > MAX_LENGTH:
        raise ValueError(f'Пожелания слишком длинные: максимум {MAX_LENGTH} символов Telegram.')


def save_preferences(text):
    validate_preferences(text)
    PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=PREFERENCES_PATH.parent, prefix='.preferences-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text.strip() + '\n')
        os.replace(temporary, PREFERENCES_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
