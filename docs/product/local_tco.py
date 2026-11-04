#!/usr/bin/env python3
"""无痕匠 · 自建 GPU 工作站 vs 云 GPU 的 TCO 与决策门槛

用法：
    python3 docs/product/local_tco.py

配套文档：docs/product/算力部署方案.md §6
"""

from __future__ import annotations

# 云 GPU（阿里云 2026-11 包月价）
CLOUD_T4_MONTH = 1681.0  # gn6i 单卡 T4 16G
CLOUD_A10_MONTH = 3204.0  # gn7i 单卡 A10 24G

# 自建单卡工作站
BUILD = {
    "price": 30000.0,  # 整机（4090 24G + 平台）★ 按实际成交价替换
    "depreciation_months": 36,
    "kw_full": 0.60,  # 满载整机功率
    "kw_idle": 0.05,  # 闲时功率
    "price_kwh": 0.6,  # 电价 元/度
    "hours_full_per_day": 8,
}

# LaMa 这类小模型，4090 相对 T4 的吞吐倍数（★ 待实测标定）
SPEEDUPS = {"保守 4x": 4, "中值 5x": 5, "乐观 7x": 7}

# 云 T4 的 LaMa 吞吐规划值（帧/秒）
T4_FPS = 12.0
FPS_SOURCE = 30


def build_monthly_cost() -> tuple[float, float, float]:
    """返回 (折旧, 电费, 合计) 元/月。"""
    dep = BUILD["price"] / BUILD["depreciation_months"]
    kwh = (
        BUILD["kw_full"] * BUILD["hours_full_per_day"]
        + BUILD["kw_idle"] * (24 - BUILD["hours_full_per_day"])
    ) * 30
    return dep, kwh * BUILD["price_kwh"], dep + kwh * BUILD["price_kwh"]


def main() -> None:
    dep, elec, build_month = build_monthly_cost()

    print("=" * 62)
    print("月度成本")
    print("=" * 62)
    print(f"  自建单卡（¥{BUILD['price']:,.0f}，{BUILD['depreciation_months']} 个月折旧）")
    print(f"    折旧 {dep:>8.2f} + 电费 {elec:>7.2f} = {build_month:>8.2f} 元/月")
    print(f"  云 T4 包月：                    {CLOUD_T4_MONTH:>8.2f} 元/月")
    print(f"  云 A10 包月：                   {CLOUD_A10_MONTH:>8.2f} 元/月")
    print(f"\n  单看绝对值：自建比云 T4 便宜 {(1 - build_month / CLOUD_T4_MONTH) * 100:.0f}%")

    print("\n" + "=" * 62)
    print("等效算力口径（更诚实的对比）")
    print("=" * 62)
    for name, mul in SPEEDUPS.items():
        cloud_equiv = CLOUD_T4_MONTH * mul
        payback = BUILD["price"] / (cloud_equiv - build_month)
        print(f"  4090 ≈ {mul}x T4（{name}）：云上同等算力 {cloud_equiv:>8.0f} 元/月"
              f" → 回本 {payback:>4.1f} 个月")

    print("\n" + "=" * 62)
    print("日产能（跑 12 小时，50% 利用率）")
    print("=" * 62)
    for label, fps in [("云 T4", T4_FPS)] + [(f"自建 4090 ({n})", T4_FPS * m) for n, m in SPEEDUPS.items()]:
        mins = fps * 3600 * 12 * 0.5 / (60 * FPS_SOURCE)
        print(f"  {label:<18}{fps:>4.0f} fps → {mins:>5.0f} 视频分钟/天"
              f" ≈ {mins / 0.5:>4.0f} 条/30s 或 {mins / 5:>3.0f} 条/5min")

    print("\n" + "=" * 62)
    print("家宽上行瓶颈（本地 worker 回传成片）")
    print("=" * 62)
    for up in (30, 50, 100):
        print(f"  上行 {up} Mbps：", end="")
        parts = []
        for size_mb, desc in ((60, "1080P 1min"), (300, "1080P 5min"), (960, "4K 1min")):
            parts.append(f"{desc} {size_mb * 8 / up:.0f}s")
        print("  " + " | ".join(parts))

    print("\n  判读：5min 1080P 在 50Mbps 下上传 48s，渲染约 "
          f"{9000 / (T4_FPS * 4):.0f}s（4090 4x）→ 上传占 "
          f"{48 / (9000 / (T4_FPS * 4)) * 100:.0f}%。并发回传必须限 1。")


if __name__ == "__main__":
    main()
