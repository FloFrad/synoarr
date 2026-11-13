"""Shared test helpers: a real local HTTP server driven by a Python callback.

Usage:
    def handler(req):            # req: FakeRequest
        if req.path == "/api":
            return 200, {"Content-Type": "application/json"}, b'{"ok": true}'
        return 404, {}, b""
    with FakeServer(handler) as srv:
        url = srv.url            # "http://127.0.0.1:<port>"
        ...
    srv.requests                 # list of FakeRequest received
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Tuple, Union

Body = Union[bytes, str, dict, list]


class FakeRequest:
    def __init__(self, method: str, raw_path: str, headers: Dict[str, str], body: bytes):
        self.method = method
        self.raw_path = raw_path
        parts = urllib.parse.urlsplit(raw_path)
        self.path = parts.path
        self.query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        self.headers = headers  # lower-cased keys
        self.body = body

    @property
    def form(self) -> Dict[str, str]:
        """Decoded application/x-www-form-urlencoded body."""
        ctype = self.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ctype:
            return dict(urllib.parse.parse_qsl(self.body.decode("utf-8"), keep_blank_values=True))
        return {}

    @property
    def multipart(self) -> Dict[str, Tuple[str, bytes]]:
        """Very small multipart parser: name -> (filename or "", content)."""
        ctype = self.headers.get("content-type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            return {}
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
        out = {}
        for part in self.body.split(b"--" + boundary):
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            head, _, content = part.partition(b"\r\n\r\n")
            disp = ""
            for line in head.decode("utf-8", "replace").split("\r\n"):
                if line.lower().startswith("content-disposition"):
                    disp = line
            name, filename = "", ""
            for item in disp.split(";"):
                item = item.strip()
                if item.startswith("name="):
                    name = item[5:].strip('"')
                elif item.startswith("filename="):
                    filename = item[9:].strip('"')
            out[name] = (filename, content)
        return out

    def params(self) -> Dict[str, str]:
        """Query + form + multipart text fields merged."""
        d = dict(self.query)
        d.update(self.form)
        for k, (fn, content) in self.multipart.items():
            if not fn:
                d[k] = content.decode("utf-8", "replace")
        return d


Handler = Callable[[FakeRequest], Tuple[int, Dict[str, str], Body]]


class FakeServer:
    def __init__(self, handler: Handler):
        self.handler = handler
        self.requests: List[FakeRequest] = []
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                req = FakeRequest(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                outer.requests.append(req)
                try:
                    status, headers, payload = outer.handler(req)
                except Exception as exc:  # surface handler bugs as 500
                    status, headers, payload = 500, {}, ("handler error: %r" % exc).encode()
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode("utf-8")
                    headers = dict(headers)
                    headers.setdefault("Content-Type", "application/json")
                elif isinstance(payload, str):
                    payload = payload.encode("utf-8")
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return "http://%s:%s" % (host, port)

    def __enter__(self) -> "FakeServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
