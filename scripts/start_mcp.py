"""Подготовить и запустить MCP-сервер с туннелем ngrok."""

import os
import errno
from pathlib import Path
import platform
import getpass
import signal
import socket
import subprocess
import sys
import time
import venv


PROJECT_DIR = Path(__file__).resolve().parents[1]


def check_port_available():
    with socket.socket() as sock:
        # Как у HTTP-сервера: допускаем перезапуск после TCP TIME_WAIT,
        # но не занимаем порт, на котором уже слушает другой сервер.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", 8765))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise RuntimeError(
                    "Порт 8765 занят другим процессом. "
                    "Проверьте его командой: ss -ltnp 'sport = :8765'"
                ) from error
            raise


def stop_processes(processes):
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    architectures = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    arch = architectures.get(platform.machine())
    if platform.system() != "Linux" or arch is None:
        raise RuntimeError("Поддерживается Linux x86_64 или arm64.")

    # Проверяем порт до установки и запуска программ.
    check_port_available()

    environment = PROJECT_DIR / ".venv"
    python = environment / "bin/python"
    if Path(sys.prefix).resolve() != environment.resolve():
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        os.execv(str(python), [str(python), str(Path(__file__).resolve())])

    subprocess.run(
        [str(python), "-m", "pip", "install", "-r", str(PROJECT_DIR / "requirements.txt")],
        check=True,
    )

    import ngrok

    token = os.environ.get("NGROK_AUTHTOKEN", "").strip()
    if not token:
        if not sys.stdin.isatty():
            raise RuntimeError("Запустите в терминале или задайте NGROK_AUTHTOKEN.")
        print("Токен: https://dashboard.ngrok.com/get-started/your-authtoken")
        token = getpass.getpass("Вставьте ngrok authtoken (ввод скрыт): ").strip()
    if not token:
        raise RuntimeError("Токен ngrok не указан.")

    processes = []
    listener = None
    try:
        server = subprocess.Popen(
            [str(python), "-m", "telegram_mcp.mcp_server"],
            cwd=PROJECT_DIR,
            start_new_session=True,
        )
        processes.append(server)
        deadline = time.monotonic() + 10
        while True:
            if server.poll() is not None:
                raise RuntimeError("MCP-сервер завершился при запуске.")
            try:
                with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("MCP-сервер не запустился за 10 секунд.")
                time.sleep(0.2)

        listener = ngrok.forward(
            "127.0.0.1:8765",
            authtoken=token,
            request_header_remove="host",
            request_header_add="host:localhost:8765",
        )
        print(
            f"\nServer URL для ChatGPT: {listener.url().rstrip('/')}/mcp\n"
            "Название: My PC Test. Authentication: No authentication.\n"
            "Для остановки сервера и туннеля нажмите Ctrl+C.",
            flush=True,
        )
        while True:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(
                        "MCP-сервер "
                        f"завершился с кодом {process.returncode}."
                    )
            time.sleep(0.2)
    finally:
        try:
            if listener is not None:
                ngrok.disconnect(listener.url())
        finally:
            stop_processes(processes)


def handle_termination(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_termination)
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        sys.exit(130)
    except Exception as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        sys.exit(1)
