"""One sample owns both normalized readings and its compatibility tree."""
from dataclasses import dataclass
from typing import Protocol

from hwmonitor.sensors import reading


@dataclass
class Snapshot:
    data: dict
    tree: dict | None


class Backend(Protocol):
    ready: bool

    def sample(self, gpu_id=None, nic_ids=None) -> Snapshot: ...
    def close(self): ...
    def request_reopen(self): ...


class WindowsBackend:
    def __init__(self, source):
        self.source = source

    @property
    def ready(self):
        return self.source.ready

    def sample(self, gpu_id=None, nic_ids=None):
        data = reading.read(gpu_id, nic_ids, self.source)
        return Snapshot(data, self.source.last_tree)

    def close(self):
        self.source.close()

    def request_reopen(self):
        self.source.request_reopen()
