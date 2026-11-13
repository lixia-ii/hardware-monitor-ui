"""Bounded RPC queue: HTTP workers never call Tk or mutate desktop state."""
from concurrent.futures import Future, TimeoutError
import queue
import threading


class WebError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Bridge:
    def __init__(self):
        self.pending = queue.Queue(maxsize=32)
        self.closed = False
        self.lock = threading.Lock()

    def call(self, action, payload=None):
        future = Future()
        try:
            with self.lock:
                if self.closed:
                    raise WebError(503, "管理服务正在停止")
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

    def close(self):
        with self.lock:
            self.closed = True
            while not self.pending.empty():
                future, _, _ = self.pending.get_nowait()
                if future.set_running_or_notify_cancel():
                    future.set_exception(WebError(503, "管理服务正在停止"))

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
