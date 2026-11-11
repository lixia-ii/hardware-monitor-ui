"""Public configuration projection and strict, optimistic management updates."""
import copy
import hashlib
import json
import re

from hwmonitor import settings
from hwmonitor.core import history
from hwmonitor.system import credentials
from hwmonitor.system.webbridge import WebError

EDITABLE = {"close_action", "topmost", "interval", "gpu_id", "nic_ids", "display", "history_enabled",
            "alerts", "floating", "routers"}


# The desktop rewrites the floating position on every drag or edge snap; keeping it
# out of the projection prevents unrelated web saves from failing as conflicts.
DESKTOP_ONLY = {"floating": {"x", "y"}}


def public_settings(config):
    result = {key: copy.deepcopy(config[key]) for key in EDITABLE}
    for key, fields in DESKTOP_ONLY.items():
        for field in fields:
            result[key].pop(field, None)
    for device in result["routers"]:
        device["password_saved"] = bool(device.pop("password_protected", ""))
    return result


def revision(config):
    return hashlib.sha256(json.dumps(public_settings(config), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def update(config, payload):
    if payload.get("revision") != revision(config):
        raise WebError(409, "设置已被其他页面或电脑端修改，请重新载入后再保存")
    patch = payload.get("changes")
    if not isinstance(patch, dict) or set(patch) - EDITABLE:
        raise WebError(400, "包含不支持的设置")
    candidate = copy.deepcopy(config)
    for key, value in patch.items():
        if key in ("alerts", "floating", "display"):
            if not isinstance(value, dict) or (key != "display" and set(value) - (set(config[key]) - DESKTOP_ONLY.get(key, set()))):
                raise WebError(400, "设置字段无效")
            if key == "display":
                if len(value) > 2000 or any(not isinstance(p, dict) or set(p) - {"visible", "name", "order"} for p in value.values()):
                    raise WebError(400, "显示配置无效")
                candidate[key].update(copy.deepcopy(value))
            else:
                candidate[key].update(copy.deepcopy(value))
        elif key == "routers":
            if not isinstance(value, list) or len(value) > 32:
                raise WebError(400, "最多支持 32 台设备")
            old = {item["id"]: item for item in config["routers"]}
            devices, used = [], set()
            for item in value:
                if not isinstance(item, dict) or set(item) - {"id", "name", "type", "url", "nic", "enabled", "notify_clients", "password", "password_saved", "reconnect"}:
                    raise WebError(400, "设备字段无效")
                device = copy.deepcopy(item)
                ident = device.get("id")
                if not isinstance(ident, str) or not re.fullmatch(r"[\w-]{1,64}", ident) or ident in used:
                    raise WebError(400, "设备编号无效或重复")
                used.add(ident)
                kind = device.get("type", "openwrt")
                if kind not in ("openwrt", "hwmonitor", "tplink") or not settings.device_url(device.get("url"), kind):
                    raise WebError(400, "设备类型或地址无效")
                device["url"] = settings.device_url(device["url"], kind)
                if kind == "openwrt":
                    device.pop("type", None)
                password = device.pop("password", "")
                reconnect = device.pop("reconnect", False)
                device.pop("password_saved", None)
                if not isinstance(password, str) or len(password) > 256:
                    raise WebError(400, "设备密码无效")
                if kind == "tplink":
                    saved = old.get(ident, {})
                    # The stored secret belongs to that exact router: a new address must
                    # never receive it, otherwise an edited URL could harvest the password.
                    same = saved.get("type") == kind and saved.get("url") == device["url"]
                    device["password_protected"] = saved.get("password_protected", "") if same else ""
                    if password or reconnect:
                        try:
                            password = password or credentials.unprotect(device["password_protected"])
                            if not password:
                                raise ValueError()
                            device["password_protected"] = credentials.protect(password)
                        except (OSError, ValueError, UnicodeError):
                            raise WebError(400, "设备密码无法保存，请重新输入") from None
                devices.append(device)
            candidate[key] = devices
        else:
            candidate[key] = copy.deepcopy(value)
    validated = settings.validate(candidate)
    # Reject coercion, out-of-range values, unknown/invalid fields instead of
    # silently accepting a different value from the one the user submitted.
    for key in patch:
        if key == "routers":
            for before, after in zip(candidate[key], validated[key]):
                expected = dict(before)
                expected.setdefault("nic", None)
                if not equivalent(expected, after):
                    raise WebError(400, "设备设置无效，请检查名称、开关和地址")
            if len(candidate[key]) != len(validated[key]):
                raise WebError(400, "设备设置无效")
        elif not equivalent(candidate[key], validated[key]):
            raise WebError(400, "设置值无效或超出允许范围")
    # Python equality treats 0/1 as booleans, so check scalar types too.
    for key in ("topmost", "history_enabled"):
        if key in patch and not isinstance(patch[key], bool):
            raise WebError(400, "开关值必须为布尔值")
    if "interval" in patch and isinstance(patch["interval"], bool):
        raise WebError(400, "刷新间隔无效")
    if "routers" in patch:
        removed = {d["id"] for d in config["routers"]} - {d["id"] for d in validated["routers"]}
        stale = {"router:" + ident for ident in removed} | {"router-settings:" + ident for ident in removed}
        prefixes = tuple("sensor:device:" + ident + ":" for ident in removed)
        validated["collapsed"] = [k for k in validated["collapsed"] if k not in stale]
        validated["display"] = {k: v for k, v in validated["display"].items() if k not in stale and not k.startswith(prefixes)}
    return validated


def equivalent(before, after):
    if isinstance(before, dict) and isinstance(after, dict):
        return before.keys() == after.keys() and all(equivalent(before[k], after[k]) for k in before)
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(equivalent(a, b) for a, b in zip(before, after))
    if isinstance(before, bool) != isinstance(after, bool):
        return False
    return before == after


def history_query(snapshot, params, export=False):
    """Pure computation on a desktop snapshot; runs on the HTTP worker, not the Tk thread."""
    try:
        hours = int(params.get("hours", ["1"])[0])
        interval = int(params.get("interval", ["30" if hours == 1 else "300"])[0])
        if hours not in (1, 24) or interval not in (0, 30, 60, 300, 900):
            raise ValueError()
    except (ValueError, TypeError, IndexError):
        raise WebError(400, "历史查询范围无效") from None
    rows = snapshot["rows"]
    end = snapshot["time"]
    start = end - hours * 3600
    groups = history.gpu_groups((d.get("gpu_id"), d.get("gpu_name")) for _, d in rows)
    selected = params.get("gpu", [None])[0]
    if selected not in groups:
        current = snapshot["gpu_id"]
        selected = next((k for k, (_, ids) in groups.items() if current in ids), next(iter(groups), None))
    gpu_ids = groups[selected][1] if selected else frozenset((None,))
    nic_ids = snapshot["nic_ids"]
    net = None if nic_ids is None else "|".join(sorted(nic_ids))
    filtered = [(t, history.filter_sample(values, gpu_ids, net)) for t, values in rows if start <= t <= end]
    if export:
        return {"rows": filtered}
    charts = {key: history.segments(rows, key, start, end, 600, gpu_ids=gpu_ids,
                                   net_selection=net, interval=interval) for key in history.METRICS}
    peaks = {key: max((v[key] for _, v in filtered if v.get(key) is not None), default=None) for key in history.METRICS}
    return {"start": start, "end": end, "charts": charts, "peaks": peaks, "count": len(filtered),
            "gpus": [[key, name] for key, (name, _) in groups.items()], "gpu": selected}
