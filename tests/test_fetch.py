"""Exercise fetch against a local HTTP server without external network services."""

import asyncio
import gzip
import json
import threading
import unittest
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlsplit

from fetch_api import FetchError, Headers, JSONObject, Promise, Request, Response, fetch


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _handle(self):
        parts = urlsplit(self.path)
        incoming = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        data = json.dumps({
            "method": self.command,
            "path": self.path,
            "body": incoming.decode("utf-8"),
            "headers": {name.lower(): value for name, value in self.headers.items()},
            "user": {"name": "Ada"},
            "rows": [{"id": 1}],
        }).encode()
        status, headers = 200, {"Content-Type": "application/json", "X-Example": "yes"}
        if parts.path.startswith("/status/"):
            status = int(parts.path.rsplit("/", 1)[1])
        elif parts.path.startswith("/redirect/"):
            status = int(parts.path.rsplit("/", 1)[1])
            headers["Location"] = parse_qs(parts.query).get("to", ["/json"])[0]
        elif parts.path == "/loop":
            status, headers["Location"] = 302, "/loop"
        elif parts.path == "/empty":
            status, data = 204, b""
        elif parts.path == "/invalid-json":
            data = b"not JSON"
        elif parts.path == "/gzip":
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        elif parts.path == "/deflate":
            data = zlib.compress(data)
            headers["Content-Encoding"] = "deflate"
        elif parts.path == "/invalid-gzip":
            data = gzip.compress(data)[:-5]
            headers["Content-Encoding"] = "gzip"
        elif parts.path == "/unsupported-encoding":
            headers["Content-Encoding"] = "br"
        elif parts.path == "/text":
            data = b"\xef\xbb\xbfHello \xc3\xa9\xff"
            headers["Content-Type"] = "text/plain; charset=iso-8859-1"
        elif parts.path == "/slow-headers":
            self.server.release_headers.wait(2)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", len(data) + (50 if parts.path == "/truncated" else 0))
        self.end_headers()
        self.wfile.flush()
        if parts.path == "/slow-body":
            self.server.release_body.wait(2)
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if parts.path == "/truncated":
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


@contextmanager
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.release_body = threading.Event()
    server.release_headers = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.release_body.set()
        server.release_headers.set()
        server.shutdown()
        server.server_close()
        thread.join()


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_context = local_server()
        cls.server, cls.base = cls.server_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server_context.__exit__(None, None, None)

    def test_default_get_and_response_metadata(self):
        promise = fetch(f"{self.base}/json?name=Ada")
        self.assertIsInstance(promise, Promise)
        response = promise.result(2)
        self.assertIsInstance(response, Response)
        self.assertTrue(response.ok)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.statusText, "OK")
        self.assertEqual(response.url, f"{self.base}/json?name=Ada")
        self.assertFalse(response.redirected)
        self.assertEqual(response.type, "basic")
        self.assertEqual(response.headers.get("X-EXAMPLE"), "yes")
        self.assertIsNone(response.headers.get("missing"))
        data = response.json().result(2)
        self.assertEqual(data.method, "GET")
        self.assertIsInstance(data, JSONObject)
        self.assertIsInstance(data.user, JSONObject)
        self.assertIsInstance(data.rows[0], JSONObject)
        self.assertEqual(data.user.name, "Ada")

    def test_methods_headers_and_bodies(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = fetch(f"{self.base}/json", {
                    "method": method,
                    "headers": {"Content-Type": "application/json", "X-Test": "yes"},
                    "body": json.dumps({"name": "Ada"}),
                }).result(2)
                data = response.json().result(2)
                self.assertEqual(data.method, method)
                self.assertEqual(json.loads(data.body), {"name": "Ada"})
                self.assertEqual(data.headers["content-type"], "application/json")
                self.assertEqual(data.headers["x-test"], "yes")

    def test_keyword_options_override_mapping_and_string_content_type(self):
        data = fetch(f"{self.base}/json", {"method": "PUT"},
                     method="post", body="hello").then(lambda response: response.json()).result(2)
        self.assertEqual(data.method, "POST")
        self.assertEqual(data.body, "hello")
        self.assertEqual(data.headers["content-type"], "text/plain;charset=UTF-8")

    def test_http_errors_fulfill_instead_of_reject(self):
        for status in (404, 500):
            with self.subTest(status=status):
                response = fetch(f"{self.base}/status/{status}").result(2)
                self.assertFalse(response.ok)
                self.assertEqual(response.status, status)
                self.assertEqual(response.json().result(2).method, "GET")

    def test_invalid_requests_reject_the_promise(self):
        cases = [
            ("relative/path", {}),
            ("file:///tmp/example", {}),
            ("http://user:password@example.com", {}),
            (f"{self.base}/json", {"method": "GET", "body": ""}),
            (f"{self.base}/json", {"method": "HEAD", "body": b""}),
            (f"{self.base}/json", {"method": "TRACE"}),
            (f"{self.base}/json", {"method": "bad method"}),
            (f"{self.base}/json", {"method": "POST", "body": {"name": "Ada"}}),
            (f"{self.base}/json", {"headers": {"X-Test": "unsafe\nvalue"}}),
            (f"{self.base}/json", {"redirect": "invalid"}),
            (f"{self.base}/json", {"timeout": 0}),
            (f"{self.base}/json", {"timeout": float("nan")}),
            (f"{self.base}/json", {"timeout": True}),
            (f"{self.base}/json", {"mode": "cors"}),
        ]
        for url, options in cases:
            with self.subTest(url=url, options=options):
                promise = fetch(url, options)
                self.assertIsInstance(promise, Promise)
                with self.assertRaises(TypeError):
                    promise.result(2)

    def test_network_failure_rejects(self):
        with patch("fetch_api.fetch.build_opener") as factory:
            factory.return_value.open.side_effect = URLError("No connection")
            with self.assertRaises(FetchError):
                fetch(f"{self.base}/json").result(2)

    def test_request_timeout(self):
        try:
            with self.assertRaises(FetchError):
                fetch(f"{self.base}/slow-headers", timeout=0.05).result(2)
        finally:
            self.server.release_headers.set()

    def test_headers_resolve_before_body_and_body_reader_waits(self):
        try:
            response = fetch(f"{self.base}/slow-body").result(1)
            self.assertEqual(response.status, 200)
            reader = response.json()
            with self.assertRaises(TimeoutError):
                reader.result(0.05)
        finally:
            self.server.release_body.set()
        self.assertEqual(reader.result(2).user.name, "Ada")

    def test_body_failure_rejects_reader_after_response_fulfills(self):
        response = fetch(f"{self.base}/truncated").result(2)
        self.assertTrue(response.ok)
        with self.assertRaises(FetchError):
            response.json().result(2)

    def test_single_use_clone_and_invalid_json(self):
        response = fetch(f"{self.base}/json").result(2)
        copied = response.clone()
        self.assertFalse(response.bodyUsed)
        reader = response.json()
        self.assertTrue(response.bodyUsed)
        self.assertEqual(reader.result(2).user.name, "Ada")
        self.assertIn('"name": "Ada"', copied.text().result(2))
        with self.assertRaises(TypeError):
            response.text().result(2)
        with self.assertRaises(TypeError):
            response.clone()
        invalid = fetch(f"{self.base}/invalid-json").result(2)
        with self.assertRaises(json.JSONDecodeError):
            invalid.json().result(2)
        self.assertTrue(invalid.bodyUsed)

    def test_null_bodies_remain_unused(self):
        for path, method in (("/empty", "GET"), ("/json", "HEAD"), ("/status/304", "GET")):
            with self.subTest(path=path, method=method):
                response = fetch(f"{self.base}{path}", method=method).result(2)
                self.assertEqual(response.text().result(2), "")
                self.assertEqual(response.bytes().result(2), b"")
                self.assertFalse(response.bodyUsed)
                with self.assertRaises(json.JSONDecodeError):
                    response.json().result(2)

    def test_binary_text_and_compressed_responses(self):
        self.assertEqual(fetch(f"{self.base}/text").then(lambda r: r.text()).result(2), "Hello é�")
        binary = fetch(f"{self.base}/json").then(lambda r: r.arrayBuffer()).result(2)
        self.assertIsInstance(binary, bytes)
        for path in ("/gzip", "/deflate"):
            with self.subTest(path=path):
                data = fetch(f"{self.base}{path}").then(lambda r: r.json()).result(2)
                self.assertEqual(data.user.name, "Ada")

    def test_redirect_modes_and_metadata(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                response = fetch(f"{self.base}/redirect/{status}").result(2)
                self.assertTrue(response.redirected)
                self.assertEqual(response.url, f"{self.base}/json")
                self.assertEqual(response.json().result(2).method, "GET")
        manual = fetch(f"{self.base}/redirect/302", redirect="manual").result(2)
        self.assertEqual(manual.status, 302)
        self.assertFalse(manual.ok)
        self.assertFalse(manual.redirected)
        self.assertEqual(manual.headers.get("location"), "/json")
        manual.bytes().result(2)
        for path, redirect in (("/redirect/302", "error"), ("/loop", "follow")):
            with self.subTest(path=path):
                with self.assertRaises(FetchError):
                    fetch(f"{self.base}{path}", redirect=redirect).result(2)

    def test_redirect_body_and_method_rules(self):
        for status, method, expected in (
            (301, "POST", "GET"), (302, "POST", "GET"), (303, "PUT", "GET"),
            (307, "POST", "POST"), (308, "PUT", "PUT"), (302, "PUT", "PUT"),
        ):
            with self.subTest(status=status, method=method):
                data = fetch(f"{self.base}/redirect/{status}", method=method, body="hello").then(
                    lambda response: response.json()
                ).result(2)
                self.assertEqual(data.method, expected)
                self.assertEqual(data.body, "" if expected == "GET" else "hello")
                if expected == "GET":
                    self.assertNotIn("content-type", data.headers)

    def test_invalid_redirect_targets(self):
        for target in ("file:///tmp/example", "http://[", "http://user:password@example.com"):
            with self.subTest(target=target):
                url = f"{self.base}/redirect/302?to={quote(target, safe='')}"
                with self.assertRaises(FetchError):
                    fetch(url).result(2)
                manual = fetch(url, redirect="manual").result(2)
                self.assertEqual(manual.status, 302)
                self.assertEqual(manual.statusText, "Found")
                self.assertEqual(manual.headers.get("location"), target)
                manual.bytes().result(2)

    def test_invalid_compressed_bodies(self):
        for path in ("/invalid-gzip", "/unsupported-encoding"):
            with self.subTest(path=path):
                response = fetch(f"{self.base}{path}").result(2)
                self.assertTrue(response.ok)
                with self.assertRaises(FetchError):
                    response.bytes().result(2)

    def test_cross_origin_redirect_drops_credentials(self):
        with local_server() as (_, destination):
            target = quote(f"{destination}/json", safe="")
            data = fetch(f"{self.base}/redirect/302?to={target}", headers={
                "Authorization": "Bearer test-fixture", "Cookie": "test=fixture", "X-Test": "kept",
            }).then(lambda response: response.json()).result(2)
            self.assertNotIn("authorization", data.headers)
            self.assertNotIn("cookie", data.headers)
            self.assertEqual(data.headers["x-test"], "kept")

    def test_request_objects_clones_and_option_overrides(self):
        request = Request(f"{self.base}/json", method="POST", body="hello", headers={"X-Test": "yes"})
        copied = request.clone()
        data = fetch(request).then(lambda response: response.json()).result(2)
        self.assertEqual(data.method, "POST")
        self.assertTrue(request.bodyUsed)
        with self.assertRaises(TypeError):
            fetch(request).result(2)
        data = fetch(copied, method="PUT", body=b"changed").then(lambda r: r.json()).result(2)
        self.assertEqual(data.method, "PUT")
        self.assertEqual(data.body, "changed")
        self.assertEqual(data.headers["x-test"], "yes")
        get = Request(f"{self.base}/json")
        for _ in range(2):
            fetch(get).then(lambda r: r.bytes()).result(2)
        self.assertFalse(get.bodyUsed)

    def test_async_await_and_promise_all(self):
        async def main():
            responses = await Promise.all([fetch(f"{self.base}/json"), fetch(f"{self.base}/status/404")])
            self.assertTrue(responses[0].ok)
            self.assertFalse(responses[1].ok)
            values = await Promise.all(response.json() for response in responses)
            self.assertEqual(values[0].user.name, "Ada")
        asyncio.run(main())

    def test_url_encoding_and_fragment(self):
        response = fetch(f"{self.base}/json?name=é space#fragment").result(2)
        self.assertEqual(response.url, f"{self.base}/json?name=%C3%A9%20space")
        self.assertEqual(response.json().result(2).path, "/json?name=%C3%A9%20space")


class ValueTests(unittest.TestCase):
    def test_headers_collection(self):
        headers = Headers([("X-Test", " first "), ("x-test", "second")])
        self.assertEqual(headers.get("X-TEST"), "first, second")
        headers.set("Accept", "application/json")
        self.assertTrue(headers.has("ACCEPT"))
        self.assertEqual(list(headers.entries())[0], ("accept", "application/json"))
        headers.delete("X-Test")
        headers.delete("missing")
        self.assertIsNone(headers.get("x-test"))
        with self.assertRaises(TypeError):
            headers.set("bad name", "value")

    def test_fetched_response_headers_are_immutable(self):
        with local_server() as (_, base):
            response = fetch(f"{base}/json").result(2)
            with self.assertRaises(TypeError):
                response.headers.set("X-Test", "changed")
            with self.assertRaises(TypeError):
                response.clone().headers.delete("Content-Type")
            response.bytes().result(2)

    def test_json_values_and_dictionary_collisions(self):
        for text, expected in (("null", None), ("true", True), ("42", 42), ('"hello"', "hello")):
            with self.subTest(text=text):
                self.assertEqual(Response(text).json().result(2), expected)
        values = Response('[{"items": [1], "user": {"name": "Ada"}}]').json().result(2)
        self.assertIsInstance(values, list)
        self.assertEqual(values[0]["items"], [1])
        self.assertEqual(values[0].user.name, "Ada")
        self.assertTrue(callable(values[0].items))
        values[0].name = "Grace"
        self.assertEqual(values[0]["name"], "Grace")
        del values[0].name
        with self.assertRaises(AttributeError):
            _ = values[0].name
        self.assertEqual(json.loads(json.dumps(values)), [{"items": [1], "user": {"name": "Ada"}}])
        for text in ("NaN", "Infinity", "-Infinity", "", "{bad}"):
            with self.subTest(text=text):
                with self.assertRaises(json.JSONDecodeError):
                    Response(text).json().result(2)

    def test_synthetic_response_validation(self):
        for status in (199, 600, True):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    Response(status=status)
        for status in (204, 205, 304):
            with self.subTest(status=status):
                with self.assertRaises(TypeError):
                    Response("", status=status)
        with self.assertRaises(TypeError):
            Response(statusText="bad\nreason")
        response = Response("hello", status=201, statusText="Created")
        self.assertTrue(response.ok)
        self.assertEqual(response.type, "default")
        self.assertEqual(response.headers.get("content-type"), "text/plain;charset=UTF-8")
        self.assertEqual(response.clone().text().result(2), "hello")
        self.assertEqual(response.text().result(2), "hello")


if __name__ == "__main__":
    unittest.main()
