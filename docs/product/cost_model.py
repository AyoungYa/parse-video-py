#!/usr/bin/env python3
"""无痕匠 · 单条视频处理成本模型（可复跑）

用法：
    python3 docs/product/cost_model.py

用途：改动下方 PARAMS 里的实测值后重跑，核对包月定价与公平使用上限是否仍成立。
配套文档：docs/product/成本与选型.md
"""

from __future__ import annotations

# ============================================================
# 待实测校准的参数 —— 上线前必须用真实数据替换
# ============================================================
PARAMS = {
    "fps": 30,  # 源片帧率
    # LaMa 推理吞吐（帧/秒）。★ 待实测：在 T4 上跑一条 1 分钟样片即可标定。
    "lama_fps": {"T4": 12.0, "A10": 20.0},
    # GPU 小时价（包月摊销）。阿里云 2026-11：gn6i(T4) ¥1681/月，gn7i(A10) ¥3204/月
    "gpu_price_h": {"T4": 2.30, "A10": 4.39},
    # 每分钟视频的码率（MB）
    "bitrate_mb_per_min": {"1080P": 60.0, "4K": 240.0},
    # 4K 的 ROI 面积约为 1080P 的 4 倍 → LaMa 耗时按面积放大。★ 待实测
    "frame_cost_mult": {"1080P": 1.0, "4K": 4.0},
    "traffic_out_per_gb": 0.5,  # OSS 外网流出（闲时 0.25）
    "download_mult": 1.5,  # 预览 + 成片 + 余量
    "pay_fee_rate": 0.006,  # 微信支付手续费
}

# 固定成本（元/月）—— 三个口径，按阶段选
GPU_SELF = {
    "整机折旧（¥30,000 / 36 月）": 30000 / 36,
    "电费（日满载 8h，0.6kW，0.6元/度）": (0.60 * 8 + 0.05 * 16) * 30 * 0.6,
}
FIXED_NON_GPU = {
    "轻量服务器 2核4G (¥379/年)": 379 / 12,
    "OSS + 流量包 (¥60/年)": 60 / 12,
    "域名两枚": 10 / 12,
}
FIXED_COSTS = {
    "开发期（全本地 Mac）": {**FIXED_NON_GPU},
    "公测期（云 Web + 自建 4090）★推荐": {**FIXED_NON_GPU, **GPU_SELF},
    "规模化（云 GPU T4 补峰）": {**FIXED_NON_GPU, "GPU T4 包月": 1681.0},
}

BASE_UTIL = 0.5  # 规划基准利用率


def cost_per_min(res: str, gpu: str, util: float = BASE_UTIL) -> tuple[float, float, float]:
    """返回 (GPU 成本, 流量成本, 合计) —— 每分钟视频的变动成本，单位元。"""
    p = PARAMS
    frames = 60 * p["fps"]
    gpu_sec = frames / p["lama_fps"][gpu] * p["frame_cost_mult"][res]
    gpu_cost = gpu_sec / 3600 * p["gpu_price_h"][gpu] / util
    traffic = p["bitrate_mb_per_min"][res] / 1024 * p["download_mult"] * p["traffic_out_per_gb"]
    return gpu_cost, traffic, gpu_cost + traffic


def unit_cost(minutes: float, res: str = "1080P", gpu: str = "T4") -> float:
    return cost_per_min(res, gpu)[2] * minutes


def main() -> None:
    p = PARAMS
    base = cost_per_min("1080P", "T4")

    print("=" * 62)
    print("每分钟视频的变动成本（元）")
    print("=" * 62)
    print(f"{'场景':<30}{'GPU':>8}{'流量':>8}{'合计':>8}")
    for res in ("1080P", "4K"):
        gpus = ("T4", "A10") if res == "1080P" else ("A10",)
        for gpu in gpus:
            for util in (0.3, 0.5, 0.7):
                g, t, tot = cost_per_min(res, gpu, util)
                print(f"{res + ' / ' + gpu + ' / 利用率' + str(int(util * 100)) + '%':<30}"
                      f"{g:>8.3f}{t:>8.3f}{tot:>8.3f}")

    print(f"\n【规划基准】1080P / T4 / {int(BASE_UTIL * 100)}% 利用率 = {base[2]:.3f} 元/分钟")

    print("\n" + "=" * 62)
    print("单条视频加工成本")
    print("=" * 62)
    for minutes, desc in ((0.25, "15s"), (0.5, "30s"), (1, "1min"), (5, "5min"), (30, "30min")):
        print(f"{desc:<8}{minutes:>6.2f}min    1080P/T4: {unit_cost(minutes):>6.2f} 元"
              f"    4K/A10: {unit_cost(minutes, '4K', 'A10'):>7.2f} 元")

    print("\n" + "=" * 62)
    print("包月毛利（1080P / T4），含 0.6% 支付手续费")
    print("=" * 62)
    for price in (39, 49):
        print(f"\n定价 ¥{price}：")
        print(f"  {'月用量':<8}{'成本':>8}{'毛利':>10}{'毛利率':>10}")
        for m in (10, 20, 50, 100, 200, 300):
            cost = unit_cost(m) + price * p["pay_fee_rate"]
            margin = price - cost
            print(f"  {str(m) + ' min':<8}{cost:>8.2f}{margin:>10.2f}{margin / price * 100:>9.1f}%")

    print("\n" + "=" * 62)
    print("免费档 / 签到的成本暴露")
    print("=" * 62)
    print(f"试用用户 3条×15s：           {unit_cost(3 * 0.25):>6.2f} 元/人")
    print(f"试用用户 3条×30s（放大后）： {unit_cost(3 * 0.50):>6.2f} 元/人")
    print(f"签到送 1 分钟/天（重度）：   {unit_cost(1) * 30:>6.2f} 元/月/人  ← 黑洞")
    print(f"签到送 30 秒/天（修正后）：  {unit_cost(0.5) * 30:>6.2f} 元/月/人")

    print("\n" + "=" * 62)
    print("固定成本与盈亏平衡（三个口径）")
    print("=" * 62)
    typical = 20  # 典型用户月用量（分钟）★ 上线 4 周后用真实 P50 替换
    for label, items in FIXED_COSTS.items():
        total_fixed = sum(items.values())
        print(f"\n  【{label}】固定成本 {total_fixed:.2f} 元/月")
        for name, val in items.items():
            print(f"    {name:<32}{val:>9.2f}")
        for price in (39, 49):
            per_user = price - unit_cost(typical) - price * p["pay_fee_rate"]
            print(f"    包月 ¥{price}：单用户毛利 {per_user:.2f} 元 "
                  f"→ 打平需 {total_fixed / per_user:.0f} 个订阅")


if __name__ == "__main__":
    main()
