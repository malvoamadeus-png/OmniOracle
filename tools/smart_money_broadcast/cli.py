from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from boards import BOARD_CATALOG, board_names, normalize_board_names
from copytrade_value import threshold_status
from discovery import discover_addresses
from http_client import ApiClient, normalize_address
from metrics import DEFAULT_CLOSED_POSITIONS_LIMIT, METRICS_COMPAT_VERSION, compute_address_metrics, is_metrics_compatible
from report import write_report
from store import DEFAULT_DB_PATH, SmartMoneyStore


OUTPUT_DIR = Path(__file__).resolve().parent / "output"
CLOSED_POSITIONS_LIMIT = DEFAULT_CLOSED_POSITIONS_LIMIT


def prompt_until_valid(label: str, parser: Callable[[str], object], default: Optional[object] = None) -> object:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            return parser(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"输入无效：{exc}")


def parse_int_min(min_value: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        value = int(raw)
        if value < min_value:
            raise ValueError(f"必须 >= {min_value}")
        return value

    return parse


def parse_float_min(min_value: float) -> Callable[[str], float]:
    def parse(raw: str) -> float:
        value = float(raw)
        if value < min_value:
            raise ValueError(f"必须 >= {min_value:g}")
        return value

    return parse


def prompt_yes_no(label: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{default_text}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "1", "是"}:
            return True
        if raw in {"n", "no", "0", "否"}:
            return False
        print("请输入 y 或 n")


def choose_menu(title: str, choices: Sequence[str], default_index: Optional[int] = None) -> int:
    print()
    print(title)
    for idx, label in enumerate(choices, start=1):
        print(f"  {idx}) {label}")
    while True:
        default = default_index if default_index is not None else None
        raw = input(f"请选择{f' [{default}]' if default is not None else ''}: ").strip()
        if not raw and default is not None:
            return int(default)
        try:
            value = int(raw)
        except ValueError:
            print("请输入数字")
            continue
        if 1 <= value <= len(choices):
            return value
        print(f"请输入 1 到 {len(choices)} 之间的数字")


def choose_boards(multiple: bool) -> List[str]:
    names = board_names()
    print()
    print("可选板块：")
    for idx, name in enumerate(names, start=1):
        cfg = BOARD_CATALOG[name]
        desc = cfg.source_kind
        if cfg.sport:
            desc += f" / {cfg.sport}"
        if cfg.tag_id is not None:
            desc += f" / tag={cfg.tag_id}"
        print(f"  {idx:>2}) {name} ({desc})")
    if multiple:
        prompt = "选择板块编号，可用逗号分隔，例如 1,3,4"
    else:
        prompt = "选择一个对比板块编号"
    while True:
        raw = input(f"{prompt}: ").strip()
        try:
            indexes = [int(part.strip()) for part in raw.split(",") if part.strip()]
            if not indexes:
                raise ValueError("至少选择一个板块")
            if not multiple and len(indexes) != 1:
                raise ValueError("只能选择一个板块")
            selected = [names[idx - 1] for idx in indexes if 1 <= idx <= len(names)]
            if len(selected) != len(indexes):
                raise ValueError("存在超出范围的编号")
            return normalize_board_names(selected)
        except Exception as exc:  # noqa: BLE001
            print(f"输入无效：{exc}")


def choose_policy() -> str:
    idx = choose_menu(
        "旧地址策略",
        [
            "reuse_old_metrics：旧地址计入目标数，优先复用上次指标",
            "skip_old：旧地址不计入目标数，必须继续找新地址",
            "refresh_old_metrics：旧地址计入目标数，但重新计算指标",
        ],
        default_index=1,
    )
    return ["reuse_old_metrics", "skip_old", "refresh_old_metrics"][idx - 1]


def fmt_cli_number(value: object, digits: int = 1) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return "NA"


def run_batch_discovery() -> int:
    print()
    print("批量发现并计算指标")
    boards = choose_boards(multiple=True)
    target_count = int(prompt_until_valid("目标地址总数", parse_int_min(1), 10))
    min_age_days = float(prompt_until_valid("最小地址年龄天数", parse_float_min(1.0), 120.0))
    min_trades = int(prompt_until_valid("最小交易次数（必须大于该值）", parse_int_min(0), 250))
    policy = choose_policy()

    print()
    print("即将执行：")
    print(f"  板块：{', '.join(boards)}")
    print(f"  目标地址总数：{target_count}")
    print(f"  最小地址年龄天数：{min_age_days:g}")
    print(f"  最小交易次数：>{min_trades}")
    print(f"  旧地址策略：{policy}")
    if not prompt_yes_no("确认开始", default=True):
        print("已取消")
        return 0

    client = ApiClient()
    store = SmartMoneyStore(DEFAULT_DB_PATH)
    try:
        result = discover_addresses(
            client,
            store,
            boards=boards,
            target_count=target_count,
            min_age_days=min_age_days,
            min_trades=min_trades,
            old_address_policy=policy,
            closed_positions_limit=CLOSED_POSITIONS_LIMIT,
        )
        print(f"\n[done] run_id={result.run_id} found={len(result.selected)}/{target_count}")
        warning = threshold_status()
        if warning:
            print(f"[warn] 跟单价值阈值不可用：{warning}")
        if result.failure_reasons:
            print(f"[filtered] {result.failure_reasons}")
        for idx, row in enumerate(result.selected, start=1):
            address = row["address"]
            board = row["board"]
            print(
                f"{idx:>3}. [{board}] {address} trades={row.get('user_stats_trades')} "
                f"age_days={fmt_cli_number(row.get('address_age_days'))} metrics_saved=yes"
            )
        return 0 if len(result.selected) >= target_count else 2
    finally:
        store.close()
        client.close()


def run_single_address_report() -> int:
    print()
    print("单地址分析并生成报告")
    while True:
        address = normalize_address(input("请输入地址: ").strip())
        if address:
            break
        print("地址不能为空")
    board = choose_boards(multiple=False)[0]
    refresh = prompt_yes_no("是否强制刷新该地址指标", default=False)

    client = ApiClient()
    store = SmartMoneyStore(DEFAULT_DB_PATH)
    try:
        metrics = None
        if not refresh:
            latest = store.latest_metrics_with_details(address, board)
            if latest is not None and is_metrics_compatible(latest.get("details"), CLOSED_POSITIONS_LIMIT):
                metrics = latest.get("metrics")
        if metrics is None and not refresh:
            cached_any = store.latest_metrics_any_board_with_details(address)
            if cached_any is not None and is_metrics_compatible(cached_any.get("details"), CLOSED_POSITIONS_LIMIT):
                metrics = cached_any.get("metrics")
                store.save_metrics(
                    address,
                    metrics,
                    {
                        "copied_from_latest_address_cache": True,
                        "closed_positions_limit": CLOSED_POSITIONS_LIMIT,
                        "metrics_compat_version": METRICS_COMPAT_VERSION,
                    },
                    board,
                )
        if metrics is None:
            result = compute_address_metrics(client, address, closed_positions_limit=CLOSED_POSITIONS_LIMIT)
            metrics = result.metrics
            store.save_metrics(address, metrics, result.details, board)
        store.upsert_address({"address": address}, board)
        cohort = store.cohort_metrics(board)
        path = write_report(address, metrics, cohort, board=board, output_dir=OUTPUT_DIR)
        store.save_report(address, path, board=board)
        warning = threshold_status()
        if warning:
            print(f"[warn] 跟单价值阈值不可用：{warning}")
        print(f"[report] {path}")
        return 0
    finally:
        store.close()
        client.close()


def show_cache_summary() -> int:
    store = SmartMoneyStore(DEFAULT_DB_PATH)
    try:
        summary = store.cache_summary()
    finally:
        store.close()
    print()
    print("本地缓存概况")
    print(f"  SQLite：{summary['db_path']}")
    print(f"  地址数：{summary['addresses']}")
    print(f"  指标快照：{summary['metrics_snapshots']}")
    print(f"  报告数：{summary['reports']}")
    boards = summary.get("boards") or {}
    if boards:
        print("  板块归属：")
        for board, count in boards.items():
            print(f"    {board}: {count}")
    else:
        print("  板块归属：暂无")
    return 0


def reset_data() -> int:
    print()
    print("清空本地运行数据")
    if not prompt_yes_no("这会删除 runtime/smart_money.sqlite 和 output/*.md，确认继续", default=False):
        print("已取消")
        return 0
    deleted_reports = 0
    if DEFAULT_DB_PATH.exists():
        DEFAULT_DB_PATH.unlink()
    if OUTPUT_DIR.exists():
        for path in OUTPUT_DIR.glob("*.md"):
            path.unlink()
            deleted_reports += 1
        if not any(OUTPUT_DIR.iterdir()):
            shutil.rmtree(OUTPUT_DIR)
    print(f"[reset] db_deleted={not DEFAULT_DB_PATH.exists()} reports_deleted={deleted_reports}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    if argv:
        print("正常入口不再使用命令行参数；请直接运行 python cli.py 并在菜单中选择功能。")
    while True:
        choice = choose_menu(
            "聪明钱播报工具",
            [
                "批量发现并计算指标",
                "单地址分析并生成报告",
                "查看本地缓存概况",
                "清空本地运行数据",
                "退出",
            ],
            default_index=1,
        )
        if choice == 1:
            run_batch_discovery()
        elif choice == 2:
            run_single_address_report()
        elif choice == 3:
            show_cache_summary()
        elif choice == 4:
            reset_data()
        elif choice == 5:
            return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
