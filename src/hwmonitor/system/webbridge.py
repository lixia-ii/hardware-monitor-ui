"""Bounded RPC queue: HTTP workers never call Tk or mutate desktop state."""
from concurrent.futures import Future, TimeoutError
import queue


class WebError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Bridge:
    def __init__(self):
        self.pending = queue.Queue(maxsize=32)

    def call(self, action, payload=None):
        future = Future()
        try:
            self.pending.put_nowait((future, action, payload))
        except queue.Full:
            raise WebError(503, "服务繁忙，请稍后重试") from None
        try:
            return future.result(timeout=8)
        except TimeoutError:
            # Cancel only queued operations. A running mutation must finish before
            # its caller receives a result, so a timeout cannot apply later unseen.
            if future.cancel():
                raise WebError(503, "电脑端暂未响应，请稍后重试") from None
            return future.result()

    def drain(self, dispatch):
        for _ in range(8):
            try:
                future, action, payload = self.pending.get_nowait()
            except queue.Empty:
                return
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(dispatch(action, payload))
                except Exception as error:
                    future.set_exception(error)
