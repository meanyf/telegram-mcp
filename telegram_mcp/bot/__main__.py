"""Команды настройки и проверки Telegram-бота."""

from .delivery import main


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nОстановлено.')
    except Exception as error:
        raise SystemExit(f'Ошибка: {error}')
