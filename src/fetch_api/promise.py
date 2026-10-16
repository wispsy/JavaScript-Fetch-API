"""Awaitable promises with JavaScript-style chaining, using only the standard library."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Generic, TypeVar

T = TypeVar("T")
U = TypeVar("U")

# Keep user callbacks off the network workers and execute them in registration order.
_callbacks = ThreadPoolExecutor(max_workers=1, thread_name_prefix="promise")


class Promise(Generic[T]):
    """A value that will be fulfilled or rejected once.

    An executor, when supplied, runs immediately with ``resolve`` and ``reject``
    callbacks. Handlers run on a separate callback thread. A handler may return a
    value or another Promise; returning a Promise adopts its eventual outcome.
    ``await promise`` is nonblocking; ``promise.result()`` is a blocking Python
    convenience for scripts without an asyncio event loop.
    """

    def __init__(
        self,
        executor: Callable[[Callable[[T], None], Callable[[BaseException], None]], None],
    ) -> None:
        self._initialize()
        try:
            executor(self._resolve, self._reject)
        except BaseException as error:
            self._reject(error)

    def _initialize(self) -> None:
        self._future: Future[T] = Future()
        self._lock = Lock()
        self._claimed = False

    @classmethod
    def _pending(cls) -> Promise:
        promise = cls.__new__(cls)
        promise._initialize()
        return promise

    @classmethod
    def _from_future(cls, future: Future[T]) -> Promise[T]:
        promise = cls._pending()
        promise._resolve(future)
        return promise

    def _claim(self) -> bool:
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True

    def _resolve(self, value) -> None:
        if not self._claim():
            return
        if value is self:
            self._future.set_exception(TypeError("A Promise cannot resolve to itself"))
        elif isinstance(value, (Promise, Future)):
            future = value._future if isinstance(value, Promise) else value
            future.add_done_callback(self._adopt)
        else:
            self._future.set_result(value)

    def _adopt(self, future: Future) -> None:
        try:
            value = future.result()
        except BaseException as error:
            self._future.set_exception(error)
        else:
            self._future.set_result(value)

    def _reject(self, error: BaseException) -> None:
        if self._claim():
            if not isinstance(error, BaseException):
                error = Exception(error)
            self._future.set_exception(error)

    @classmethod
    def resolve(cls, value: T | Promise[T]) -> Promise[T]:
        """Return an existing Promise, or a fulfilled Promise containing a value."""
        if isinstance(value, cls):
            return value
        promise = cls._pending()
        promise._resolve(value)
        return promise

    @classmethod
    def reject(cls, error: BaseException) -> Promise:
        """Return a rejected Promise. Python exceptions represent rejection reasons."""
        promise = cls._pending()
        promise._reject(error)
        return promise

    def then(
        self,
        on_fulfilled: Callable[[T], U | Promise[U]] | None = None,
        on_rejected: Callable[[BaseException], U | Promise[U]] | None = None,
    ) -> Promise[U]:
        """Chain a handler, forwarding an outcome when its handler is omitted."""
        following = self._pending()

        def run(future: Future[T]) -> None:
            try:
                try:
                    value = future.result()
                except BaseException as error:
                    if not callable(on_rejected):
                        following._reject(error)
                        return
                    outcome = on_rejected(error)
                else:
                    outcome = on_fulfilled(value) if callable(on_fulfilled) else value
                following._resolve(outcome)
            except BaseException as error:
                following._reject(error)

        self._future.add_done_callback(lambda future: _callbacks.submit(run, future))
        return following

    def catch(self, on_rejected: Callable[[BaseException], U | Promise[U]]) -> Promise[U]:
        """Handle rejection, equivalent to ``then(None, on_rejected)``."""
        return self.then(None, on_rejected)

    def finally_(self, on_finally: Callable[[], object]) -> Promise[T]:
        """Run cleanup and preserve the outcome unless cleanup itself fails.

        Python reserves the word ``finally``, so its method is named ``finally_``.
        A returned Promise is awaited before the original outcome is forwarded.
        """
        if not callable(on_finally):
            return self.then()

        def fulfilled(value):
            return Promise.resolve(on_finally()).then(lambda _: value)

        def rejected(error):
            return Promise.resolve(on_finally()).then(lambda _: Promise.reject(error))

        return self.then(fulfilled, rejected)

    @classmethod
    def all(cls, values: Iterable[T | Promise[T]]) -> Promise[list[T]]:
        """Fulfill with input-ordered results, or reject on the first failure."""
        combined = cls._pending()
        try:
            promises = [cls.resolve(value) for value in values]
        except BaseException as error:
            combined._reject(error)
            return combined
        results = [None] * len(promises)
        remaining = len(promises)
        lock = Lock()
        if not remaining:
            combined._resolve([])
            return combined

        def complete(index, value):
            nonlocal remaining
            with lock:
                results[index] = value
                remaining -= 1
                if remaining == 0:
                    combined._resolve(results)

        for index, promise in enumerate(promises):
            promise.then(lambda value, index=index: complete(index, value), combined._reject)
        return combined

    @classmethod
    def race(cls, values: Iterable[T | Promise[T]]) -> Promise[T]:
        """Adopt the first outcome; an empty iterable leaves the Promise pending."""
        raced = cls._pending()
        try:
            for value in values:
                cls.resolve(value).then(raced._resolve, raced._reject)
        except BaseException as error:
            raced._reject(error)
        return raced

    def result(self, timeout: float | None = None) -> T:
        """Wait for the outcome. The timeout limits waiting, not the HTTP request."""
        return self._future.result(timeout)

    def __await__(self):
        # Cancelling one waiter must not cancel a Promise shared by other callers.
        return asyncio.shield(asyncio.wrap_future(self._future)).__await__()
