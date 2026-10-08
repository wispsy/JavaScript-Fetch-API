"""A standard-library implementation of the core JavaScript Fetch API for Python."""

from __future__ import annotations

import gzip
import json
import math
import re
import zlib
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from threading import Lock
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request as URLRequest, build_opener

from .jsonobject import JSONObject, JSONValue
from .promise import Promise

type BodyInit = str | bytes | bytearray | memoryview | None
type HeadersInit = Mapping[str, str] | Iterable[tuple[str, str]]
type Redirect = Literal["follow", "error", "manual"]

_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_OPTIONS = frozenset({"method", "headers", "body", "redirect", "timeout"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_NULL_BODY_STATUSES = frozenset({204, 205, 304})
_network = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fetch")


class FetchError(TypeError):
    """A network failure, distinct from an HTTP error status on a Response."""


class Headers(MutableMapping[str, str]):
    """Case-insensitive headers with ``get``, ``set``, ``append`` and ``delete``."""

    def __init__(self, values: HeadersInit | None = None) -> None:
        self._values: dict[str, str] = {}
        self._immutable = False
        if values is not None:
            pairs = values.items() if isinstance(values, Mapping) else values
            for name, value in pairs:
                self.append(name, value)

    @staticmethod
    def _name(name: str) -> str:
        if not isinstance(name, str) or not _TOKEN.fullmatch(name):
            raise TypeError("Header names must be valid HTTP tokens")
        return name.lower()

    @staticmethod
    def _value(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("Header values must be strings")
        if any(character in value for character in "\r\n\0"):
            raise TypeError("Header values cannot contain a newline or null byte")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as error:
            raise TypeError("Header values must be representable as HTTP bytes") from error
        return value.strip(" \t")

    def _check_mutable(self) -> None:
        if self._immutable:
            raise TypeError("Headers on a fetched response are immutable")

    def __getitem__(self, name: str) -> str:
        return self._values[self._name(name)]

    def __setitem__(self, name: str, value: str) -> None:
        self._check_mutable()
        self._values[self._name(name)] = self._value(value)

    def __delitem__(self, name: str) -> None:
        self._check_mutable()
        del self._values[self._name(name)]

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._values))

    def __len__(self) -> int:
        return len(self._values)

    def set(self, name: str, value: str) -> None:
        self[name] = value

    def append(self, name: str, value: str) -> None:
        self._check_mutable()
        name, value = self._name(name), self._value(value)
        if name in self._values:
            value = f"{self._values[name]}, {value}"
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._check_mutable()
        self._values.pop(self._name(name), None)

    def has(self, name: str) -> bool:
        return self._name(name) in self._values

    def entries(self) -> Iterator[tuple[str, str]]:
        return iter(self.items())


def _body_bytes(body: BodyInit, headers: Headers) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, str):
        if not headers.has("content-type"):
            headers.set("content-type", "text/plain;charset=UTF-8")
        return body.encode("utf-8")
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body)
    raise TypeError("body must be text or bytes; use json.dumps() for a JSON request")


def _parse_json(data: bytes) -> JSONValue:
    def invalid_constant(value: str):
        raise json.JSONDecodeError(f"Invalid JSON constant: {value}", value, 0)

    return json.loads(
        data.decode("utf-8-sig", errors="replace"),
        object_hook=JSONObject,
        parse_constant=invalid_constant,
    )


class _Body:
    def _initialize_body(self, data: bytes | None) -> None:
        self._body: Promise[bytes] | None = None if data is None else Promise.resolve(data)
        self._body_used = False
        self._body_lock = Lock()

    @property
    def bodyUsed(self) -> bool:
        with self._body_lock:
            return self._body_used

    def _mark_body_used(self) -> None:
        with self._body_lock:
            if self._body_used:
                raise TypeError("Body has already been consumed; clone it before reading")
            if self._body is not None:
                self._body_used = True

    def _consume(self, transform) -> Promise:
        try:
            self._mark_body_used()
        except TypeError as error:
            return Promise.reject(error)
        body = self._body if self._body is not None else Promise.resolve(b"")
        return body.then(transform)

    def json(self) -> Promise[JSONValue]:
        """Consume and parse JSON; objects, including nested objects, are JSONObject."""
        return self._consume(_parse_json)

    def text(self) -> Promise[str]:
        """Consume UTF-8 text, replacing invalid byte sequences like JavaScript does."""
        return self._consume(lambda data: data.decode("utf-8-sig", errors="replace"))

    def bytes(self) -> Promise[bytes]:
        """Consume the body as Python bytes."""
        return self._consume(lambda data: data)

    def arrayBuffer(self) -> Promise[bytes]:
        """Python's immutable bytes represent JavaScript's ArrayBuffer result."""
        return self.bytes()


def _url(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("resource must be an absolute HTTP(S) URL or a Request")
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Missing HTTP(S) scheme or hostname")
        if parts.username is not None or parts.password is not None:
            raise ValueError("Credentials in URLs are unsupported")
        hostname = parts.hostname.encode("idna").decode("ascii")
        if ":" in hostname:
            hostname = f"[{hostname}]"
        authority = hostname if parts.port is None else f"{hostname}:{parts.port}"
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        return urlunsplit((parts.scheme, authority, path, query, ""))
    except (ValueError, UnicodeError) as error:
        raise TypeError("resource must be a valid absolute HTTP(S) URL without credentials") from error


def _options(options: Mapping[str, object] | None, keywords: dict) -> dict:
    if options is not None and not isinstance(options, Mapping):
        raise TypeError("options must be a mapping")
    values = dict(options or {})
    values.update(keywords)
    unsupported = values.keys() - _OPTIONS
    if unsupported:
        raise TypeError(f"Unsupported fetch options: {', '.join(sorted(map(str, unsupported)))}")
    return values


class Request(_Body):
    """A URL plus request options. Options override an existing Request's settings."""

    def __init__(
        self,
        resource: str | Request,
        options: Mapping[str, object] | None = None,
        **keywords: object,
    ) -> None:
        values = _options(options, keywords)
        original = resource if isinstance(resource, Request) else None
        if original is not None and original.bodyUsed:
            raise TypeError("Cannot copy a Request whose body has been consumed")
        self._url = original.url if original is not None else _url(resource)
        method = values.get("method", original.method if original is not None else "GET")
        if not isinstance(method, str) or not _TOKEN.fullmatch(method):
            raise TypeError("method must be a valid HTTP token")
        if method.upper() in {"CONNECT", "TRACE", "TRACK"}:
            raise TypeError("CONNECT, TRACE and TRACK are forbidden fetch methods")
        if method.upper() in {"DELETE", "GET", "HEAD", "OPTIONS", "POST", "PUT"}:
            method = method.upper()
        self._method = method
        self._headers = Headers(values.get("headers", original.headers if original else None))
        body = values.get("body", original._data if original is not None else None)
        self._data = _body_bytes(body, self.headers)
        if method in {"GET", "HEAD"} and self._data is not None:
            raise TypeError("GET and HEAD requests cannot have a body")
        self._redirect = values.get("redirect", original.redirect if original else "follow")
        if self._redirect not in {"follow", "error", "manual"}:
            raise TypeError("redirect must be 'follow', 'error' or 'manual'")
        timeout = values.get("timeout", original.timeout if original else None)
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise TypeError("timeout must be a positive, finite number of seconds or None")
            if not math.isfinite(timeout) or timeout <= 0:
                raise TypeError("timeout must be a positive, finite number of seconds or None")
        self._timeout = timeout
        self._initialize_body(self._data)

    @property
    def url(self) -> str:
        return self._url

    @property
    def method(self) -> str:
        return self._method

    @property
    def headers(self) -> Headers:
        return self._headers

    @property
    def redirect(self) -> Redirect:
        return self._redirect

    @property
    def timeout(self) -> float | None:
        return self._timeout

    def clone(self) -> Request:
        """Copy a request before its body has been sent or read."""
        return Request(self)


class Response(_Body):
    """HTTP metadata and one-use, promise-returning body readers."""

    def __init__(
        self,
        body: BodyInit = None,
        *,
        status: int = 200,
        statusText: str = "",
        headers: HeadersInit | None = None,
    ) -> None:
        if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
            raise ValueError("Response status must be an integer between 200 and 599")
        if not isinstance(statusText, str) or any(char in statusText for char in "\r\n"):
            raise TypeError("statusText must be a string without newlines")
        self._status, self._status_text = status, statusText
        self._headers = Headers(headers)
        data = _body_bytes(body, self.headers)
        if status in _NULL_BODY_STATUSES and data is not None:
            raise TypeError("Responses with status 204, 205 or 304 cannot have a body")
        self._url, self._redirected, self._type = "", False, "default"
        self._initialize_body(data)

    @property
    def ok(self) -> bool:
        return 200 <= self.status <= 299

    @property
    def status(self) -> int:
        return self._status

    @property
    def statusText(self) -> str:
        return self._status_text

    @property
    def headers(self) -> Headers:
        return self._headers

    @property
    def url(self) -> str:
        return self._url

    @property
    def redirected(self) -> bool:
        return self._redirected

    @property
    def type(self) -> str:
        return self._type

    def clone(self) -> Response:
        """Share the buffered bytes with a new, independently consumable Response."""
        with self._body_lock:
            if self._body_used:
                raise TypeError("Cannot clone a Response whose body has been consumed")
            cloned = Response(headers=self.headers)
            cloned._status, cloned._status_text = self.status, self.statusText
            cloned._url, cloned._redirected, cloned._type = self.url, self.redirected, self.type
            cloned._body = self._body
            cloned.headers._immutable = self.headers._immutable
            return cloned


class _NoRedirect(HTTPRedirectHandler):
    # Let HTTPErrorProcessor expose the response, without urllib interpreting
    # Location first. _send applies the Fetch redirect rules itself.
    def http_error_302(self, request, stream, code, message, headers):
        return None

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _read_body(stream) -> bytes:
    try:
        with stream:
            data = stream.read()
            encodings = stream.headers.get("Content-Encoding", "").lower().split(",")
            for encoding in reversed(encodings):
                encoding = encoding.strip()
                if encoding == "gzip":
                    data = gzip.decompress(data)
                elif encoding == "deflate":
                    data = zlib.decompress(data)
                elif encoding not in {"", "identity"}:
                    raise FetchError("Unsupported response content encoding")
            return data
    except (OSError, EOFError, HTTPException, zlib.error) as error:
        raise FetchError("Reading the response body failed") from error


def _send(request: Request) -> Response:
    opener = build_opener(_NoRedirect())
    url, method, data = request.url, request.method, request._data
    headers = Headers(request.headers)
    redirects = 0
    while True:
        outgoing = URLRequest(url, data=data, headers=dict(headers), method=method)
        try:
            stream = opener.open(outgoing, timeout=request.timeout)
        except HTTPError as response:
            # urllib treats HTTP errors as exceptions; fetch resolves them normally.
            stream = response
        except (URLError, OSError, HTTPException) as error:
            raise FetchError("Network request failed") from error
        status = stream.code
        location = stream.headers.get("Location")
        if status in _REDIRECT_STATUSES and location is not None and request.redirect != "manual":
            stream.close()
            if request.redirect == "error":
                raise FetchError("Redirect encountered with redirect='error'")
            if redirects >= 20:
                raise FetchError("Too many redirects")
            try:
                next_url = _url(urljoin(url, location))
            except (TypeError, ValueError) as error:
                raise FetchError("Invalid redirect URL") from error
            if _origin(url) != _origin(next_url):
                for name in ("authorization", "proxy-authorization", "cookie", "host"):
                    headers.delete(name)
            if (status in {301, 302} and method == "POST") or (
                status == 303 and method not in {"GET", "HEAD"}
            ):
                method, data = "GET", None
                for name in (
                    "content-encoding", "content-language", "content-location",
                    "content-type", "content-length",
                ):
                    headers.delete(name)
            url = next_url
            redirects += 1
            continue
        response = Response(headers=stream.headers.items())
        response._status, response._status_text = status, str(stream.reason or "")
        response._url, response._redirected, response._type = url, redirects > 0, "basic"
        response.headers._immutable = True
        if method == "HEAD" or status in _NULL_BODY_STATUSES:
            stream.close()
        else:
            response._body = Promise._from_future(_network.submit(_read_body, stream))
        return response


def fetch(
    resource: str | Request,
    options: Mapping[str, object] | None = None,
    **keywords: object,
) -> Promise[Response]:
    """Start an HTTP(S) request and return a Promise resolving to its Response.

    ``resource`` is an absolute URL or Request. ``options`` accepts ``method``
    (default GET), ``headers``, ``body``, ``redirect`` (default follow), and
    ``timeout`` (seconds, default None). The same options may be passed as Python
    keyword arguments; keywords override the options mapping.

    HTTP 4xx/5xx responses fulfill the Promise; inspect ``response.ok`` or
    ``response.status``. Invalid requests and network errors reject it. The
    Promise fulfills when headers arrive; body readers wait for the body bytes.
    Browser-only options and streaming bodies are outside this implementation.
    """
    try:
        request = Request(resource, options, **keywords)
        if isinstance(resource, Request):
            resource._mark_body_used()
        request._mark_body_used()
    except (TypeError, ValueError) as error:
        return Promise.reject(error)
    return Promise._from_future(_network.submit(_send, request))
