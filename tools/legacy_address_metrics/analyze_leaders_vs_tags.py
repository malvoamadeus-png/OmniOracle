"""
跟单地址 vs 标签地址 参数对比分析
对比 copytrade leaders 与 顶尖/高手/排除 标签组在所有核心指标上的差异
"""
import json, os, sys, sqlite3, warnings

# Fix Windows GBK terminal encoding
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib
import seaborn as sns
import requests
from pathlib import Path

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial"]
matplotlib.rcParams["axes.unicode_minus"] = False
warnings.filterwarnings("ignore")

OUT_DIR = Path("analysis_output")
OUT_DIR.mkdir(exist_ok=True)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_COPYTRADE_DIR = PROJECT_ROOT / "backend" / "packages" / "copytrade"
DB_PATH = "metrics_fresh.sqlite"
METRICS_COLS = [
    "total_pnl", "roi", "sharpe", "max_drawdown", "profit_factor",
    "win_rate", "ulcer_index", "equity_r2", "hhi", "position_size_cv", "total_trades",
]
METRIC_LABELS = {
    "total_pnl": "总盈亏 ($)", "roi": "ROI", "sharpe": "夏普比率",
    "max_drawdown": "最大回撤", "profit_factor": "盈亏比", "win_rate": "胜率",
    "ulcer_index": "溃疡指数", "equity_r2": "权益R²", "hhi": "HHI集中度",
    "position_size_cv": "仓位CV", "total_trades": "总交易数",
}
# ── 1. 收集跟单地址 ──────────────────────────────────────────
def load_leader_addresses() -> set:
    addrs = set()
    with open(BACKEND_COPYTRADE_DIR / "copytrade_config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    for a in cfg.get("leader_addresses", []):
        addrs.add(a.lower())
    # toml accounts
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    for toml_file in (BACKEND_COPYTRADE_DIR / "accounts").glob("*.toml"):
        if toml_file.name.startswith("_"):
            continue
        with open(toml_file, "rb") as f:
            acfg = tomllib.load(f)
        for a in acfg.get("leader_addresses", []):
            addrs.add(a.lower())
        for ovr in acfg.get("leader_overrides", {}).values():
            pass  # keys are already in leader_addresses
    return addrs

# ── 2. 从 Supabase 拉标签 ────────────────────────────────────
def load_tags_from_supabase() -> pd.DataFrame:
    from dotenv import load_dotenv
    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    r = requests.get(f"{url}/rest/v1/address_tags?select=address,tag", headers=headers)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    df["address"] = df["address"].str.lower()
    return df

# ── 3. 从 SQLite 拉指标 ──────────────────────────────────────
def load_metrics() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    cols = ", ".join(["address", "confidence"] + METRICS_COLS)
    df = pd.read_sql_query(f"SELECT {cols} FROM address_metrics", conn)
    conn.close()
    df["address"] = df["address"].str.lower()
    return df

# ── 4. 分组 & 合并 ───────────────────────────────────────────
def build_groups(leaders: set, tags_df: pd.DataFrame, metrics_df: pd.DataFrame) -> pd.DataFrame:
    """给每个地址打上 group 标签，跟单优先"""
    # 只保留有指标的地址
    merged = metrics_df.copy()
    # 标签映射
    tag_map = dict(zip(tags_df["address"], tags_df["tag"]))

    def assign_group(addr):
        if addr in leaders:
            return "跟单"
        t = tag_map.get(addr)
        if t in ("顶尖", "高手", "排除", "特殊策略", "待观察"):
            return t
        return None

    merged["group"] = merged["address"].apply(assign_group)
    merged = merged[merged["group"].notna()].copy()
    return merged

# ── 5. 汇总统计 ──────────────────────────────────────────────
def summary_table(df: pd.DataFrame) -> pd.DataFrame:
    groups = ["跟单", "顶尖", "高手", "排除"]
    rows = []
    for g in groups:
        sub = df[df["group"] == g]
        n = len(sub)
        for col in METRICS_COLS:
            s = sub[col].dropna()
            if len(s) == 0:
                continue
            rows.append({
                "组": g, "N": n, "指标": METRIC_LABELS.get(col, col),
                "均值": s.mean(), "中位数": s.median(),
                "标准差": s.std(), "最小": s.min(), "最大": s.max(),
            })
    return pd.DataFrame(rows)

# ── 6. 箱线图 ────────────────────────────────────────────────
def plot_boxplots(df: pd.DataFrame):
    groups_order = ["跟单", "顶尖", "高手", "排除"]
    palette = {"跟单": "#e74c3c", "顶尖": "#f39c12", "高手": "#3498db", "排除": "#95a5a6"}
    sub = df[df["group"].isin(groups_order)]

    fig, axes = plt.subplots(3, 4, figsize=(22, 14))
    axes = axes.flatten()
    plotted = 0
    for col in METRICS_COLS:
        data = sub[["group", col]].dropna()
        if data.empty:
            continue
        ax = axes[plotted]
        sns.boxplot(data=data, x="group", y=col, order=groups_order,
                    palette=palette, ax=ax, showfliers=False)
        ax.set_title(METRIC_LABELS.get(col, col), fontsize=13)
        ax.set_xlabel("")
        ax.set_ylabel("")
        plotted += 1
    for i in range(plotted, len(axes)):
        axes[i].set_visible(False)
    fig.suptitle("跟单 vs 标签组 — 各指标箱线图", fontsize=16, y=1.01)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "boxplots.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {OUT_DIR / 'boxplots.png'}")

# ── 7. 雷达图 ────────────────────────────────────────────────
def plot_radar(df: pd.DataFrame):
    groups_order = ["跟单", "顶尖", "高手", "排除"]
    colors = {"跟单": "#e74c3c", "顶尖": "#f39c12", "高手": "#3498db", "排除": "#95a5a6"}
    # 选取适合雷达图的指标（排除 total_pnl 和 total_trades 量级差异太大）
    radar_cols = ["roi", "sharpe", "max_drawdown", "profit_factor", "win_rate", "ulcer_index", "equity_r2"]
    radar_labels = [METRIC_LABELS[c] for c in radar_cols]

    # 计算各组中位数并标准化到 0-1
    medians = {}
    for g in groups_order:
        sub = df[df["group"] == g]
        medians[g] = [sub[c].dropna().median() if sub[c].dropna().shape[0] > 0 else 0 for c in radar_cols]
    all_vals = np.array(list(medians.values()))
    mins = all_vals.min(axis=0)
    maxs = all_vals.max(axis=0)
    rng = maxs - mins
    rng[rng == 0] = 1

    angles = np.linspace(0, 2 * np.pi, len(radar_cols), endpoint=False).tolist()
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(9, 9), subplot_kw=dict(polar=True))
    for g in groups_order:
        vals = ((np.array(medians[g]) - mins) / rng).tolist()
        vals += vals[:1]  # close the polygon
        ax.plot(angles, vals, "o-", label=g, color=colors[g], linewidth=2)
        ax.fill(angles, vals, alpha=0.1, color=colors[g])
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(radar_labels, fontsize=11)
    ax.set_title("各组指标中位数雷达图（标准化 0-1）", fontsize=14, pad=20)
    ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.1))
    fig.savefig(OUT_DIR / "radar.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {OUT_DIR / 'radar.png'}")

# ── 8. 热力图 ────────────────────────────────────────────────
def plot_heatmap(df: pd.DataFrame):
    groups_order = ["跟单", "顶尖", "高手", "排除"]
    rows = []
    for g in groups_order:
        sub = df[df["group"] == g]
        row = {"组": g}
        for c in METRICS_COLS:
            s = sub[c].dropna()
            row[METRIC_LABELS.get(c, c)] = s.median() if len(s) > 0 else np.nan
        rows.append(row)
    hm = pd.DataFrame(rows).set_index("组")
    # 标准化每列到 0-1 方便颜色映射
    hm_norm = (hm - hm.min()) / (hm.max() - hm.min())
    hm_norm = hm_norm.fillna(0)

    fig, ax = plt.subplots(figsize=(16, 5))
    sns.heatmap(hm_norm, annot=hm.round(4), fmt="", cmap="YlOrRd", ax=ax,
                linewidths=1, cbar_kws={"label": "标准化值"})
    ax.set_title("各组指标中位数热力图", fontsize=14)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "heatmap.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {OUT_DIR / 'heatmap.png'}")

# ── main ──────────────────────────────────────────────────────
def main():
    print("=== 跟单地址 vs 标签地址 参数对比分析 ===\n")

    print("[1/6] 加载跟单地址...")
    leaders = load_leader_addresses()
    print(f"  跟单地址: {len(leaders)} 个")

    print("[2/6] 从 Supabase 拉取标签...")
    tags_df = load_tags_from_supabase()
    print(f"  标签地址: {len(tags_df)} 个")
    for tag, cnt in tags_df["tag"].value_counts().items():
        print(f"    {tag}: {cnt}")

    print("[3/6] 从 SQLite 加载指标...")
    metrics_df = load_metrics()
    print(f"  总地址: {len(metrics_df)}")

    print("[4/6] 分组...")
    grouped = build_groups(leaders, tags_df, metrics_df)
    for g, cnt in grouped["group"].value_counts().items():
        overlap = ""
        if g != "跟单":
            n_overlap = len(set(grouped[grouped["group"] == g]["address"]) & leaders)
            overlap = f" (已排除 {n_overlap} 个跟单重叠)" if n_overlap == 0 else ""
        print(f"  {g}: {cnt} 个{overlap}")

    # 显示跟单地址与标签的重叠情况
    tagged_addrs = set(tags_df["address"])
    overlap_count = len(leaders & tagged_addrs)
    print(f"\n  跟单地址中有 {overlap_count}/{len(leaders)} 个也在标签列表中（已从标签组排除）")

    print("\n[5/6] 生成汇总统计表...")
    summary = summary_table(grouped)
    summary.to_csv(OUT_DIR / "summary_stats.csv", index=False, encoding="utf-8-sig")
    # 打印精简版
    for g in ["跟单", "顶尖", "高手", "排除"]:
        sub = summary[summary["组"] == g]
        if sub.empty:
            continue
        print(f"\n  ── {g} (N={sub.iloc[0]['N']}) ──")
        for _, row in sub.iterrows():
            print(f"    {row['指标']:10s}  均值={row['均值']:>12.4f}  中位数={row['中位数']:>12.4f}")
    print(f"\n  -> {OUT_DIR / 'summary_stats.csv'}")

    print("\n[6/6] 生成可视化图表...")
    plot_boxplots(grouped)
    plot_radar(grouped)
    plot_heatmap(grouped)

    print("\n=== 完成！所有输出在 analysis_output/ 目录 ===")


if __name__ == "__main__":
    main()
