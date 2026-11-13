"""Select runtime before importing desktop or platform libraries."""
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="硬件监控：Windows 桌面 / Linux 服务器")
    parser.add_argument("--headless", action="store_true", help="运行无桌面服务器")
    parser.add_argument("--check", action="store_true", help="检查环境和采集能力后退出")
    parser.add_argument("--host", default="127.0.0.1", help="后台监听 IPv4 地址，默认仅本机")
    parser.add_argument("--port", type=int, default=7076, help="后台管理端口")
    # Legacy: the data endpoint is now a setting; this only migrates an old command line into it.
    parser.add_argument("--data-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--nvidia", action="store_true", help="启用可选 NVIDIA 查询")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    # The windowed desktop build has no console to show argparse errors; ignore unknown flags there.
    args, unknown = parser.parse_known_args(argv)
    if sys.platform == "linux" or args.headless or args.check:
        if unknown:
            parser.error("无法识别的参数: " + " ".join(unknown))
        if not 1 <= args.port <= 65535:
            parser.error("端口必须在 1–65535 之间")
        if args.data_port is not None and not 1024 <= args.data_port <= 65535:
            parser.error("传感器数据接口端口必须在 1024–65535 之间")
        from hwmonitor.server import main as server_main
        return server_main(args)
    from hwmonitor.desktop import desktop_main
    return desktop_main()
