"""Platform credential facade; platform libraries are loaded only on demand."""
import sys


def protect(password):
    if sys.platform == "linux":
        from hwmonitor.system.linux_credentials import protect as backend
    else:
        from hwmonitor.system.windows_credentials import protect as backend
    return backend(password)


def unprotect(value):
    if sys.platform == "linux":
        from hwmonitor.system.linux_credentials import unprotect as backend
    else:
        from hwmonitor.system.windows_credentials import unprotect as backend
    return backend(value)
