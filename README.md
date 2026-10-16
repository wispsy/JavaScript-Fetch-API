# JavaScript-Fetch-API

A Python 3.14 implementation of the core JavaScript Fetch API, built with the
standard library. There are no runtime dependencies. The HTTP transport uses
`urllib.request`; requests run on background threads so `await` does not block
the asyncio event loop.

## Install

```sh
python -m pip install JavaScript-Fetch-API
```

The Python import remains `from javascript import fetch`.

## GET and JSON

Replace the example URL with an HTTP(S) endpoint that returns JSON:

```python
import asyncio

from javascript import JSONObject, fetch


async def main() -> None:
    response = await fetch("https://example.com/api/profile")  # method defaults to GET
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status}: {response.statusText}")

    data = await response.json()
    if isinstance(data, JSONObject):
        print(data["name"])
        print(data.name)  # attribute access works too


asyncio.run(main())
```

Like JavaScript, `fetch()` returns a `Promise[Response]`. `response.json()`
returns another promise, which resolves to parsed data. JSON objects become
dictionary-compatible `JSONObject` instances, including objects nested inside
objects or arrays. JSON arrays become lists, and strings, numbers, booleans and
`null` become Python strings, numbers, booleans and `None`.

Attribute access is a Python convenience. Use brackets for keys with spaces or
keys matching dictionary methods, such as `data["items"]` or `data["get"]`.
You can serialize a `JSONObject` with the standard `json.dumps()` function.

## Parameters

```python
fetch(resource, options=None, **keywords) -> Promise[Response]
```

`resource` is an absolute `http://` or `https://` URL, or a `Request` instance.
There is no document base URL for relative URLs. URL credentials are rejected.
`options` is an optional mapping. You may also use Python keyword arguments;
keywords override matching settings in the mapping.

| Option | Accepted value | Default |
| --- | --- | --- |
| `method` | HTTP method string, such as `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD` or `OPTIONS` | `GET` |
| `headers` | Mapping of string names to string values, iterable of `(name, value)` pairs, or `Headers` | Empty |
| `body` | `str`, `bytes`, `bytearray`, `memoryview`, or `None` | `None` |
| `redirect` | `"follow"`, `"error"`, or `"manual"` | `"follow"` |
| `timeout` | Positive, finite seconds, or `None`; limits individual socket operations | `None` |

`timeout` is a Python extension, not a JavaScript Fetch option. When `resource`
is a `Request`, omitted options inherit that request's settings. `GET` and
`HEAD` cannot include a body, even an empty one. `CONNECT`, `TRACE` and `TRACK`
are rejected. A string body receives `Content-Type: text/plain;charset=UTF-8`
unless you supply a content type; byte bodies receive no automatic content type.

For a JSON request, serialize explicitly, as you would with JavaScript's
`JSON.stringify()`:

```python
import json

response = await fetch(
    "https://example.com/api/profile",
    {
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"name": "Ada"}),
    },
)
```

The equivalent keyword form is
`await fetch(url, method="POST", headers=headers, body=json.dumps(data))`.

## Response

The fetch promise resolves when the server's response headers are available.
The body is read and buffered separately; its promise may still be pending or
may reject if the connection fails after the headers arrive.

| Property | Meaning |
| --- | --- |
| `ok` | `True` when `200 <= status <= 299` |
| `status` | HTTP status code |
| `statusText` | HTTP reason phrase, such as `"OK"` |
| `headers` | Case-insensitive `Headers` collection; fetched response headers are immutable |
| `url` | Final URL after redirects, without its fragment |
| `redirected` | Whether at least one redirect was followed |
| `type` | `"basic"` for fetched HTTP responses; `"default"` for constructed responses |
| `bodyUsed` | Whether this request or response body has been consumed |

| Method | Promise result |
| --- | --- |
| `json()` | `JSONObject`, list, or JSON primitive, depending on the document |
| `text()` | UTF-8 string; invalid sequences are replaced and an initial BOM is removed |
| `bytes()` | Python `bytes` |
| `arrayBuffer()` | Python `bytes`, representing the JavaScript binary result |

Body readers consume a body once. Calling another reader rejects with
`TypeError`, including after JSON parsing fails. Clone **before** reading if
you need more than one representation:

```python
response = await fetch(url)
copy = response.clone()
data = await response.json()
text = await copy.text()
```

`HEAD` responses and statuses `204`, `205`, and `304` have a null body. Their
text/binary readers return empty values without setting `bodyUsed`; `json()`
rejects because an empty body is not JSON.

HTTP `404` and `500` fulfill with a response whose `ok` is `False`. Invalid
requests reject with `TypeError`; network failures reject with `FetchError`, a
`TypeError` subclass. Invalid JSON rejects its reader with `json.JSONDecodeError`.
Check the HTTP status yourself when your application treats it as an error.

You can construct a response locally with
`Response('{"name":"Ada"}', status=200, headers={"Content-Type": "application/json"})`.

## Promises

Promises support `await`, `.then(on_fulfilled, on_rejected=None)`, `.catch()` and
`.finally_()`. Python reserves `finally`, so the method has a trailing underscore.
Handlers return a value or another `Promise`; returned promises are adopted,
and exceptions raised by handlers reject the next promise.

```python
from javascript import fetch


def read_json(response):
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status}")
    return response.json()


completed = (
    fetch("https://example.com/api/profile")
    .then(read_json)
    .then(lambda data: print(data.name))
    .catch(lambda error: print(f"Request failed: {error}"))
    .finally_(lambda: print("Finished"))
)
completed.result()  # blocking convenience for a synchronous script
```

`Promise(executor)` immediately calls `executor(resolve, reject)` and accepts
only the first resolution or rejection. Rejection reasons use Python exceptions.
`Promise.resolve(value)`, `Promise.reject(error)`, `Promise.all(iterable)` and
`Promise.race(iterable)` are also available. `all()` preserves input order and
rejects on the first failure. `race()` adopts the first settled outcome.

Use `await` inside asynchronous code. `.result(timeout=...)` blocks its calling
thread and only limits how long that caller waits; it does not stop the request.
Cancelling an asyncio waiter also leaves the shared promise running. Handlers
execute serially on a background callback thread; Python does not reproduce
JavaScript's event-loop microtask scheduling. Handlers should return promises
instead of blocking on `.result()` or returning raw Python coroutines.

## Headers and Request

`Headers` provides `.get()`, `.set()`, `.append()`, `.delete()`, `.has()`,
`.entries()`, `.keys()` and `.values()`, plus normal mapping access.
Names are case-insensitive, duplicate values are combined with `", "`, and
invalid HTTP names or values are rejected. Missing `.get()` lookups return
`None`.

`Request(url, options=None, **keywords)` supports the same options as `fetch()`
and exposes `url`, `method`, `headers`, `redirect`, `timeout`, `bodyUsed`, and
the same body readers. A request with a body is consumed when fetched; call
`.clone()` beforehand to reuse it. Requests without bodies may be reused.

## Compatibility scope

This implements core HTTP(S) fetch behavior in Python, rather than a complete
browser environment. Bodies are buffered in memory; `ReadableStream`, `Blob`,
`FormData`, and `URLSearchParams` objects are not provided. `gzip` and `deflate`
response encodings are decoded with the standard library.
Other response content encodings reject the body reader with `FetchError`.

Browser policies and options such as `mode`, `credentials`, `cache`, `referrer`,
`referrerPolicy`, `integrity`, `keepalive`, `signal`, `priority`, and `duplex`
are not implemented; unsupported options are rejected explicitly. There is no
cookie jar, browser cache, origin policy, or abort controller. Proxy environment
settings follow `urllib.request` defaults.

Redirects follow Fetch's method/body rules, with a limit of 20. Cross-origin
redirects remove authorization and cookie headers. `redirect="error"` rejects;
`redirect="manual"` exposes the actual redirect status, headers and body,
instead of a browser's filtered `opaqueredirect` response.

References: [Fetch Standard](https://fetch.spec.whatwg.org/),
[ECMAScript Promise specification](https://tc39.es/ecma262/multipage/control-abstraction-objects.html#sec-promise-objects),
and [Python urllib.request](https://docs.python.org/3/library/urllib.request.html).

## Verification

```sh
uv run python -m unittest discover -s tests -v
```

The tests use only the standard library and local loopback HTTP servers. To run
without installing the package or its build backend:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
