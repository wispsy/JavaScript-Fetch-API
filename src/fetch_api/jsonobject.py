"""JSON objects with both dictionary lookup and JavaScript-style attribute access."""

from __future__ import annotations


class JSONObject(dict[str, "JSONValue"]):
    """A JSON object: ``obj['name']`` and ``obj.name`` access the same value.

    Parsed nested objects also use this class. Keys that overlap dictionary
    methods, such as ``items`` or ``get``, should be accessed with brackets.
    Arrays stay lists and JSON primitives retain their normal Python types.
    """

    def __getattr__(self, name: str) -> JSONValue:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name: str, value: JSONValue) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError as error:
            raise AttributeError(name) from error


type JSONValue = JSONObject | list[JSONValue] | str | int | float | bool | None
