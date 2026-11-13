"""Tiny HTTP client on top of urllib (no third-party dependency).

Features: query params, JSON helpers, multipart/form-data upload, retries on
network errors / 5xx, optional TLS verification disable (DSM self-signed cert),
cookie jar, and *no* automatic redirect to non-http schemes: a redirect to a
``magnet:`` URI is returned as ``Response.redirect_location``.
"""
from __future__ import annotations

import http.client
import http.cookiejar
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = "synoarr/%s (+https://github.com/)" % __version__

Params = Optional[Union[Mapping[str, Any], Iterable[Tuple[str, Any]]]]
# files: field name -> (filename, content bytes, content type)
Files = Optional[Mapping[str, Tuple[str, bytes, str]]]


class HttpError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, body: bytes = b"", url: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
        self.url = url


class Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes, url: str,
                 redirect_location: Optional[str] = None):
        self.status = status
        self.headers = headers  # lower-cased keys
        self.body = body
        self.url = url
        self.redirect_location = redirect_location

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text())
        except ValueError as exc:
            snippet = self.text()[:200]
            raise HttpError("Réponse JSON invalide de %s: %s (%r)" % (redact(self.url), exc, snippet),
                            self.status, self.body, self.url)


def redact(url: str) -> str:
    """Hide secrets (tokens, api keys, passwords) from URLs before logging."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    q = []
    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        low = k.lower()
        if low in ("passwd", "password", "_sid", "sid", "account") or \
                low.endswith(("apikey", "api_key", "token", "passkey", "secret")):
            v = "***"
        q.append((k, v))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(q), parts.fragment))


class _NoRedirectToOtherSchemes(urllib.request.HTTPRedirectHandler):
    """Follow http(s) redirects, but stop on magnet:/other schemes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if not newurl.lower().startswith(("http://", "https://")):
            raise _SchemeRedirect(newurl, code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _SchemeRedirect(Exception):
    def __init__(self, location: str, code: int):
        super().__init__(location)
        self.location = location
        self.code = code


def build_multipart(fields: Mapping[str, Any], files: Files) -> Tuple[bytes, str]:
    boundary = "----synoarr" + uuid.uuid4().hex
    lines: List[bytes] = []
    for name, value in fields.items():
        lines.append(("--%s" % boundary).encode())
        lines.append(('Content-Disposition: form-data; name="%s"' % name).encode())
        lines.append(b"")
        lines.append(value if isinstance(value, bytes) else str(value).encode("utf-8"))
    for name, (filename, content, ctype) in (files or {}).items():
        safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        lines.append(("--%s" % boundary).encode())
        lines.append(('Content-Disposition: form-data; name="%s"; filename="%s"' % (name, safe_name)).encode("utf-8"))
        lines.append(("Content-Type: %s" % (ctype or "application/octet-stream")).encode())
        lines.append(b"")
        lines.append(content)
    lines.append(("--%s--" % boundary).encode())
    lines.append(b"")
    return b"\r\n".join(lines), "multipart/form-data; boundary=%s" % boundary


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 2, backoff: float = 1.5,
                 verify_ssl: bool = True, user_agent: str = USER_AGENT, use_proxy: bool = True):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.verify_ssl = verify_ssl
        self.user_agent = user_agent
        self.cookies = http.cookiejar.CookieJar()
        handlers: List[Any] = [urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirectToOtherSchemes()]
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        if not use_proxy:  # e.g. DSM on 127.0.0.1: never through http_proxy/https_proxy
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------------ public
    def get(self, url: str, params: Params = None, headers: Optional[Mapping[str, str]] = None,
            timeout: Optional[float] = None, raise_for_status: bool = True) -> Response:
        return self.request("GET", url, params=params, headers=headers, timeout=timeout,
                            raise_for_status=raise_for_status)

    def post(self, url: str, params: Params = None, data: Optional[Mapping[str, Any]] = None,
             json_body: Any = None, files: Files = None, headers: Optional[Mapping[str, str]] = None,
             timeout: Optional[float] = None, raise_for_status: bool = True) -> Response:
        return self.request("POST", url, params=params, data=data, json_body=json_body, files=files,
                            headers=headers, timeout=timeout, raise_for_status=raise_for_status)

    def request(self, method: str, url: str, params: Params = None, data: Optional[Mapping[str, Any]] = None,
                json_body: Any = None, files: Files = None, headers: Optional[Mapping[str, str]] = None,
                timeout: Optional[float] = None, raise_for_status: bool = True) -> Response:
        full_url = self._with_params(url, params)
        body: Optional[bytes] = None
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if files:
            body, ctype = build_multipart(data or {}, files)
            hdrs["Content-Type"] = ctype
        elif json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(list(data.items()), doseq=False).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            hdrs.update(headers)

        attempt = 0
        while True:
            attempt += 1
            try:
                return self._do(method, full_url, body, hdrs, timeout or self.timeout, raise_for_status)
            except HttpError as exc:
                retryable = exc.status is None or exc.status >= 500 or exc.status == 429
                if not retryable or attempt > self.retries:
                    raise
                wait = self.backoff * attempt
                log.debug("HTTP %s %s failed (%s), retry in %.1fs", method, redact(full_url), exc, wait)
                time.sleep(wait)

    # ---------------------------------------------------------------- private
    @staticmethod
    def _with_params(url: str, params: Params) -> str:
        if not params:
            return url
        items = list(params.items()) if isinstance(params, Mapping) else list(params)
        items = [(k, v) for k, v in items if v is not None]
        if not items:
            return url
        sep = "&" if "?" in url else "?"
        return url + sep + urllib.parse.urlencode(items)

    def _do(self, method: str, url: str, body: Optional[bytes], headers: Mapping[str, str],
            timeout: float, raise_for_status: bool) -> Response:
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                content = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return Response(resp.status, hdrs, content, resp.geturl())
        except _SchemeRedirect as red:
            return Response(red.code, {"location": red.location}, b"", url, redirect_location=red.location)
        except urllib.error.HTTPError as exc:
            content = b""
            try:
                content = exc.read()
            except Exception:  # pragma: no cover - defensive
                pass
            hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            location = hdrs.get("location", "")
            if exc.code in (301, 302, 303, 307, 308) and location and \
                    not location.lower().startswith(("http://", "https://", "/")):
                # urllib refuses redirects to magnet: & co. -> hand the location to the caller
                return Response(exc.code, hdrs, content, url, redirect_location=location)
            resp = Response(exc.code, hdrs, content, url)
            if raise_for_status:
                raise HttpError("HTTP %s pour %s" % (exc.code, redact(url)), exc.code, content, url)
            return resp
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, _SchemeRedirect):
                return Response(reason.code, {"location": reason.location}, b"", url,
                                redirect_location=reason.location)
            raise HttpError("Connexion impossible à %s: %s" % (redact(url), reason), None, b"", url)
        except (socket.timeout, TimeoutError) as exc:
            raise HttpError("Délai dépassé pour %s: %s" % (redact(url), exc), None, b"", url)
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise HttpError("Erreur réseau pour %s: %s" % (redact(url), exc), None, b"", url)
        except ValueError as exc:  # malformed URL (bad port, unknown scheme...)
            raise HttpError("URL invalide %s: %s" % (redact(url), exc), None, b"", url)
