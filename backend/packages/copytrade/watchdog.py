"""跟单系统 watchdog — 监控主进程，挂了自动重启.

用法:
    python copytrade/watchdog.py                  # 默认启动多账号模式
    python copytrade/watchdog.py --dry-run        # dry-run 模式
    python copytrade/watchdog.py --account main   # 只跑指定账号

特性:
    - 主进程退出后自动重启（指数退避: 5s → 10s → 20s → ... → 最大 300s）
    - 连续成功运行 10 分钟后重置退避
    - 崩溃日志写入 copytrade/watchdog.log
    - Ctrl+C 优雅退出，不重启
"""

import datetime
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_PACKAGES_DIR = Path(__file__).resolve().parent.parent
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

from copytrade.paths import PACKAGE_DIR, WATCHDOG_LOG_PATH

SCRIPT_DIR = str(PACKAGE_DIR)
LOG_PATH = str(WATCHDOG_LOG_PATH)
MAIN_SCRIPT = str(PACKAGE_DIR / "main.py")

# 退避参数
BASE_DELAY = 5
BASE_DELAY = 5
MAX_DELAY = 300
STABLE_THRESHOLD = 600  # 连续运行 10 分钟视为稳定，重置退避


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def build_cmd(args: list[str]) -> list[str]:
    cmd = [sys.executable, "-u", MAIN_SCRIPT]
    cmd.extend(args)
    return cmd


def main() -> int:
    # 透传参数给 main.py（跳过 watchdog.py 自身）
    passthrough = sys.argv[1:]

    cmd = build_cmd(passthrough)
    log(f"watchdog 启动: {' '.join(cmd)}")

    consecutive_failures = 0
    stop_requested = False

    def _signal_handler(sig, frame):
        nonlocal stop_requested
        stop_requested = True
        log("收到停止信号，等待子进程退出...")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    while not stop_requested:
        start_ts = time.time()
        proc = None
        try:
            proc = subprocess.Popen(cmd)
            log(f"主进程已启动 (PID={proc.pid})")

            # 等待进程结束，同时响应停止信号
            while proc.poll() is None:
                if stop_requested:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    log("主进程已终止（用户请求）")
                    return 0
                time.sleep(1)

            rc = proc.returncode
        except Exception as e:
            rc = -1
            log(f"启动主进程异常: {e}")

        elapsed = time.time() - start_ts

        if stop_requested:
            return 0

        # 分析退出原因
        if elapsed >= STABLE_THRESHOLD:
            consecutive_failures = 0  # 运行够久，重置退避

        consecutive_failures += 1
        delay = min(BASE_DELAY * (2 ** (consecutive_failures - 1)), MAX_DELAY)

        log(f"主进程退出: code={rc}, 运行了 {elapsed:.0f}s, "
            f"第 {consecutive_failures} 次连续失败, {delay}s 后重启")

        # 等待退避时间，期间可被中断
        wait_until = time.time() + delay
        while time.time() < wait_until and not stop_requested:
            time.sleep(1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
