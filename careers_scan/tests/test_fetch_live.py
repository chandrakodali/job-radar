"""The real Fetcher against a local HTTP server: redirects, robots.txt, non-HTML, size cap, charsets."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from careers_scan.fetch import Fetcher


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p == "/robots.txt":
            return self._send(200, b"User-agent: *\nDisallow: /secret\n", "text/plain")
        if p == "/careers":
            return self._send(301, headers={"Location": "/careers/"})
        if p == "/careers/":
            return self._send(200, "<title>Careers</title><p>Email jobs@acme.com — café</p>".encode("utf-8"))
        if p == "/latin":
            return self._send(200, "<p>café</p>".encode("cp1252"), "text/html; charset=ISO-8859-1")
        if p == "/pdf":
            return self._send(200, b"%PDF-1.4", "application/pdf")
        if p == "/big":
            return self._send(200, b"<p>" + b"x" * 500_000 + b"</p>")
        if p == "/loop":
            return self._send(302, headers={"Location": "/loop"})
        if p == "/secret":
            return self._send(200, b"<p>should not be fetched</p>")
        return self._send(404, b"<title>Not Found</title>")


@pytest.fixture(scope="module")
def server():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except OSError:
        pytest.skip("cannot bind a local port")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_live_fetch_behaviour(server, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    f = Fetcher(host_delay=0, max_bytes=100_000, timeout=(3, 5))
    pg = f.page(server + "/careers")
    assert pg.ok and pg.url.endswith("/careers/") and "jobs@acme.com" in pg.text and "café" in pg.text
    assert "café" in f.page(server + "/latin").text
    assert f.page(server + "/pdf").error == "not_html"
    big = f.page(server + "/big")
    assert big.ok and len(big.text) <= 100_000 + 65536
    assert f.page(server + "/loop").error == "redirects"
    assert f.page(server + "/secret").error == "robots"
    assert f.page(server + "/missing").status == 404
    assert f.page("http://127.0.0.1:1/").error in ("connect", "timeout")
    assert f.page("https://nonexistent.invalid/").error in ("dns", "connect", "timeout")
