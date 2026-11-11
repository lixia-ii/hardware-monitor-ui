"""Debounced terminal presence changes from successful, fresh router samples."""


class DevicePresence:
    def __init__(self):
        self.identity = None
        self.reset()

    def reset(self):
        self.known = None
        self.pending = {}
        self.sample_time = None

    def update(self, config, reading):
        identity = (config.get("url"), config.get("password_protected"))
        if self.identity != identity:
            self.identity = identity
            self.reset()
        if not config.get("notify_clients", True) or reading.get("offline") or reading.get("terminals") is None:
            self.reset()
            return []
        now = reading.get("terminals_sample_time")
        if now is None or now == self.sample_time:
            return []
        self.sample_time = now
        current = {t["id"]: t for t in reading["terminals"]}
        if self.known is None:
            self.known = current
            return []  # Initial/recovered snapshot establishes a baseline silently.
        events = []
        for ident in sorted(self.known.keys() | current.keys()):
            online = ident in current
            if online == (ident in self.known):
                self.pending.pop(ident, None)
                if online:
                    self.known[ident] = current[ident]
                continue
            since = self.pending.setdefault(ident, now)
            if now - since < 3:
                continue
            terminal = current[ident] if online else self.known[ident]
            events.append({"router_id": config["id"], "router_name": config["name"],
                           "name": terminal["name"], "online": online})
            if online:
                self.known[ident] = terminal
            else:
                del self.known[ident]
            self.pending.pop(ident, None)
        return events
