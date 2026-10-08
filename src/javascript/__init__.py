"""JavaScript-style fetch, response objects and promises for Python."""

from .fetch import FetchError, Headers, Request, Response, fetch
from .jsonobject import JSONObject, JSONValue
from .promise import Promise

__all__ = [
    "FetchError",
    "Headers",
    "JSONObject",
    "JSONValue",
    "Promise",
    "Request",
    "Response",
    "fetch",
]


def main() -> None:
    print("Hello from javascript!")
