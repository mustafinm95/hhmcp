from __future__ import annotations

import http.client
import threading
from http.server import HTTPServer

from hh_mcp.cli import _CallbackResult, _handler


def test_bad_oauth_state_does_not_poison_valid_callback() -> None:
    result = _CallbackResult()
    server = HTTPServer(("127.0.0.1", 0), _handler("/callback", "", "expected", result))
    host = f"127.0.0.1:{server.server_port}"
    server.RequestHandlerClass = _handler("/callback", host, "expected", result)

    def serve_two() -> None:
        server.handle_request()
        server.handle_request()

    thread = threading.Thread(target=serve_two)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    connection.request("GET", "/callback?code=attacker&state=wrong")
    assert connection.getresponse().status == 400
    assert result.finished.is_set() is False
    connection.close()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    connection.request("GET", "/callback?code=good-code&state=expected")
    assert connection.getresponse().status == 200
    connection.close()
    thread.join(2)
    server.server_close()
    assert not thread.is_alive()
    assert result.code == "good-code"
    assert result.finished.is_set() is True

