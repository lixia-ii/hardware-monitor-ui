"""Stateful alerts, independent from the GUI and wall-clock changes."""
class AlertEngine:
    def __init__(self):
        self.states = {}

    def check(self, data, cfg, now):
        messages = []
        active_keys = set()
        for dev, label in (("cpu", "CPU"), ("gpu", "显卡")):
            identity = (data.get("gpu_id") or "unknown") if dev == "gpu" else "cpu"
            temp, limit = data.get(dev + "_temp"), cfg[dev + "_temp"]
            for fan in (False, True):
                key = (identity, "fan" if fan else "temp")
                active_keys.add(key)
                state = self.states.setdefault(key, {"since": None, "active": False, "last": -float("inf"), "seen": None})
                enabled = cfg["fan_enabled"] if fan else cfg["enabled"]
                rpm = data.get(dev + "_fan")
                valid = temp is not None and (not fan or rpm is not None)
                # Missing samples and long sampling gaps must break the sustained condition.
                if not enabled or not valid or (state["seen"] is not None and now - state["seen"] > 35):
                    state.update(since=None, active=False)
                state["seen"] = now
                if not enabled or not valid:
                    continue
                bad = temp >= limit and (not fan or rpm < cfg["fan_min"])
                recovered = temp <= limit - cfg["hysteresis"] or (fan and rpm >= cfg["fan_min"])
                if recovered:
                    state.update(since=None, active=False)
                if not bad:
                    if not state["active"]:
                        state["since"] = None
                    continue
                if state["since"] is None:
                    state["since"] = now
                duration = cfg["fan_duration"] if fan else cfg["duration"]
                if now - state["since"] >= duration:
                    state["active"] = True
                    if now - state["last"] >= cfg["cooldown"]:
                        state["last"] = now
                        messages.append(f"{label} 高温 {temp:.0f}°C，风扇仅 {rpm:.0f} RPM" if fan
                                        else f"{label} 温度 {temp:.0f}°C，持续超过 {limit}°C")
        for key, state in self.states.items():
            if key not in active_keys:
                state.update(since=None, active=False, seen=None)
        return messages
