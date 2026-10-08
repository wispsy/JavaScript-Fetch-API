import asyncio
import threading
import unittest

from javascript import Promise


def deferred():
    callbacks = {}
    promise = Promise(lambda resolve, reject: callbacks.update(resolve=resolve, reject=reject))
    return promise, callbacks["resolve"], callbacks["reject"]


class PromiseTests(unittest.TestCase):
    def test_executor_is_immediate_and_first_settlement_wins(self):
        called = []

        def executor(resolve, reject):
            called.append(True)
            resolve(1)
            reject(ValueError("ignored"))
            resolve(2)

        promise = Promise(executor)
        self.assertEqual(called, [True])
        self.assertEqual(promise.result(2), 1)
        self.assertIs(Promise.resolve(promise), promise)

    def test_chaining_adopts_promises_and_forwards_outcomes(self):
        self.assertEqual(Promise.resolve(2).then(lambda n: Promise.resolve(n * 3)).then().result(2), 6)
        pending, resolve, reject = deferred()
        adopted = Promise(lambda fulfill, fail: (fulfill(pending), fail(ValueError("ignored"))))
        with self.assertRaises(TimeoutError):
            adopted.result(0.01)
        resolve("ready")
        reject(ValueError("also ignored"))
        self.assertEqual(adopted.result(2), "ready")
        error = ValueError("failure")
        with self.assertRaises(ValueError) as raised:
            Promise.reject(error).then().result(2)
        self.assertIs(raised.exception, error)

    def test_exceptions_and_recovery(self):
        def fail(_):
            raise ValueError("bad input")

        self.assertEqual(Promise.resolve(1).then(fail).catch(lambda e: str(e)).result(2), "bad input")
        self.assertEqual(Promise.reject(ValueError("bad")).then(None, lambda _: 7).result(2), 7)

        def broken_executor(resolve, reject):
            raise RuntimeError("executor failed")

        with self.assertRaises(RuntimeError):
            Promise(broken_executor).result(2)

    def test_finally_preserves_outcome_and_waits_for_cleanup(self):
        cleanup, finish, _ = deferred()
        result = Promise.resolve(3).finally_(lambda: cleanup)
        with self.assertRaises(TimeoutError):
            result.result(0.01)
        finish("ignored")
        self.assertEqual(result.result(2), 3)
        error = ValueError("original")
        events = []
        with self.assertRaises(ValueError) as raised:
            Promise.reject(error).finally_(lambda: events.append("cleaned")).result(2)
        self.assertIs(raised.exception, error)
        self.assertEqual(events, ["cleaned"])
        with self.assertRaises(RuntimeError):
            Promise.resolve(3).finally_(lambda: Promise.reject(RuntimeError("cleanup failed"))).result(2)

    def test_all_orders_results_and_rejects_failures(self):
        first, resolve_first, _ = deferred()
        second, resolve_second, _ = deferred()
        combined = Promise.all([first, second, 3])
        resolve_second(2)
        resolve_first(1)
        self.assertEqual(combined.result(2), [1, 2, 3])
        self.assertEqual(Promise.all([]).result(2), [])
        with self.assertRaises(ValueError):
            Promise.all([1, Promise.reject(ValueError("failure"))]).result(2)

        def broken_iterable():
            yield 1
            raise RuntimeError("iteration failed")

        with self.assertRaises(RuntimeError):
            Promise.all(broken_iterable()).result(2)

    def test_race_and_self_resolution(self):
        pending, resolve, _ = deferred()
        self.assertEqual(Promise.race([pending, 4]).result(2), 4)
        resolve(5)
        with self.assertRaises(TimeoutError):
            Promise.race([]).result(0.01)
        promise, resolve, _ = deferred()
        resolve(promise)
        with self.assertRaises(TypeError):
            promise.result(2)
        original, resolve, _ = deferred()
        following = original.then(lambda _: following)
        resolve(1)
        with self.assertRaises(TypeError):
            following.result(2)

    def test_callbacks_are_asynchronous_and_registered_in_order(self):
        events = []
        current_thread = threading.get_ident()
        promise, resolve, _ = deferred()
        first = promise.then(lambda _: events.append((1, threading.get_ident())))
        second = promise.then(lambda _: events.append((2, threading.get_ident())))
        resolve(None)
        Promise.all([first, second]).result(2)
        self.assertEqual([entry[0] for entry in events], [1, 2])
        self.assertTrue(all(thread != current_thread for _, thread in events))

    def test_await_cancellation_does_not_cancel_shared_promise(self):
        async def main():
            promise, resolve, _ = deferred()

            async def wait():
                return await promise

            waiting = asyncio.create_task(wait())
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            resolve(42)
            self.assertEqual(await promise, 42)
            self.assertEqual(await promise, 42)

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
