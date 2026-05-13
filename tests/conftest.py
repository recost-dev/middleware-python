"""Shared fixtures for all recost tests."""

import http.server
import threading
from typing import Generator, Tuple

import pytest
from recost._interceptor import uninstall


@pytest.fixture(autouse=True)
def cleanup():
    """Ensure interceptor is uninstalled after each test."""
    yield
    uninstall()


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    status_to_return: int = 200
    retry_after_header: str = ""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(self.status_to_return)
        if self.retry_after_header:
            self.send_header("Retry-After", self.retry_after_header)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _start_server(status: int, retry_after: str = "") -> Tuple[http.server.HTTPServer, str]:
    handler = type(
        "H",
        (_StatusHandler,),
        {"status_to_return": status, "retry_after_header": retry_after},
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    return server, url


@pytest.fixture
def mock_http_server_200() -> Generator[http.server.HTTPServer, None, None]:
    server, url = _start_server(200)
    server.url = url  # type: ignore[attr-defined]
    yield server
    server.shutdown()


@pytest.fixture
def mock_http_server_401() -> Generator[http.server.HTTPServer, None, None]:
    server, url = _start_server(401)
    server.url = url  # type: ignore[attr-defined]
    yield server
    server.shutdown()


@pytest.fixture
def mock_http_server_429_retry_after_2() -> Generator[http.server.HTTPServer, None, None]:
    server, url = _start_server(429, retry_after="2")
    server.url = url  # type: ignore[attr-defined]
    yield server
    server.shutdown()
