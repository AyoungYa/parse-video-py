/**
 * 选片即预检。
 *
 * 目的：把「能不能处理」的判断全部提前到浏览器，一个字节都不传。
 * 现有竞品都是传完 500MB 才报「不支持」，这是最大的体验坑。
 *
 * 注意：这里的积分估算只用于展示预期，实际扣费以服务端 confirm 返回的
 * points_cost 为准（规则在 cloud_web/config/settings/base.py）。
 */

import { grabFrame, guessWatermarkCorner, probeVideo, type Quota, type VideoMeta } from "@/api/client";

export type CheckState = "pass" | "info" | "fail";

export interface PreflightCheck {
  label: string;
  state: CheckState;
  detail: string;
}

export interface PreflightReport {
  file: File;
  meta: VideoMeta;
  thumb: string;
  corner: string | null;
  checks: PreflightCheck[];
  /** 有任何一项 fail 就不允许继续 */
  blocked: boolean;
  /** 是否超出当前额度（不 block，交给服务端最终裁决） */
  overQuota: boolean;
  costPoints: number;
}

const MAX_BYTES = 500 * 1024 * 1024;
export const HARD_MAX_SECONDS = 30 * 60;
const ACCEPT_EXT = ["mp4", "mov", "avi", "mkv", "webm", "m4v"];

/** 与后端 points_for_duration 保持一致 */
export function estimatePoints(durationMs: number): number {
  const s = durationMs / 1000;
  if (s <= 30) return 1;
  if (s <= 300) return 3;
  return 3 * (Math.floor(s / 300) + 1);
}

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

export async function preflight(file: File, quota: Quota | null): Promise<PreflightReport> {
  const checks: PreflightCheck[] = [];
  const ext = extOf(file.name);
  const typeOk = file.type.startsWith("video/") || ACCEPT_EXT.includes(ext);

  if (!typeOk) {
    checks.push({
      label: "文件格式",
      state: "fail",
      detail: `不支持 ${ext ? "." + ext : "该格式"}，请用 mp4 / mov / avi / mkv`,
    });
    return {
      file,
      meta: { duration_ms: 0, width: 0, height: 0, fps: 0 },
      thumb: "",
      corner: null,
      checks,
      blocked: true,
      overQuota: false,
      costPoints: 0,
    };
  }
  checks.push({ label: "文件格式", state: "pass", detail: (ext || "mp4").toUpperCase() });

  if (file.size > MAX_BYTES) {
    checks.push({
      label: "文件大小",
      state: "fail",
      detail: `${(file.size / 1024 / 1024).toFixed(0)}MB 超过 500MB，可先压缩再传`,
    });
    return {
      file,
      meta: { duration_ms: 0, width: 0, height: 0, fps: 0 },
      thumb: "",
      corner: null,
      checks,
      blocked: true,
      overQuota: false,
      costPoints: 0,
    };
  }
  checks.push({
    label: "文件大小",
    state: "pass",
    detail: `${(file.size / 1024 / 1024).toFixed(1)}MB`,
  });

  // 读不出元数据就没法做任何时长判断，直接拒
  let meta: VideoMeta;
  try {
    meta = await probeVideo(file);
  } catch {
    checks.push({ label: "读取视频", state: "fail", detail: "文件可能已损坏，换一个试试" });
    return { file, meta: { duration_ms: 0, width: 0, height: 0, fps: 0 }, thumb: "", corner: null, checks, blocked: true, overQuota: false, costPoints: 0 };
  }

  const seconds = meta.duration_ms / 1000;
  if (!seconds) {
    checks.push({ label: "视频时长", state: "fail", detail: "读不到时长，文件可能不完整" });
    return { file, meta, thumb: "", corner: null, checks, blocked: true, overQuota: false, costPoints: 0 };
  }

  if (seconds > HARD_MAX_SECONDS) {
    checks.push({
      label: "视频时长",
      state: "fail",
      detail: `${(seconds / 60).toFixed(1)} 分钟，超出单条 30 分钟上限，请先分段`,
    });
  } else if (quota && seconds > quota.max_seconds) {
    checks.push({
      label: "视频时长",
      state: "info",
      detail: `${seconds.toFixed(1)} 秒，超出当前档位单条 ${quota.max_seconds} 秒上限`,
    });
  } else {
    checks.push({ label: "视频时长", state: "pass", detail: `${seconds.toFixed(1)} 秒` });
  }

  const costPoints = quota?.is_member ? 0 : estimatePoints(meta.duration_ms);
  const overQuota = !!quota && !quota.is_member && costPoints > quota.points;
  if (overQuota) {
    checks.push({
      label: "积分余额",
      state: "info",
      detail: `需要 ${costPoints} 分，当前 ${quota!.points} 分`,
    });
  }

  // 缩略图 + 水印粗定位（失败不影响主流程，静默降级）
  let thumb = "";
  let corner: string | null = null;
  try {
    thumb = await grabFrame(file);
    if (thumb) {
      const img = new Image();
      const found = await new Promise<{ corner: string } | null>((resolve) => {
        img.onload = () => {
          const r = guessWatermarkCorner(img);
          resolve(r ? { corner: r.corner } : null);
        };
        img.onerror = () => resolve(null);
        img.src = thumb;
      });
      corner = found?.corner ?? null;
      if (corner) {
        checks.push({ label: "水印位置", state: "info", detail: `检测到${corner}疑似水印` });
      }
    }
  } catch {
    /* 抽帧失败不阻挡上传 */
  }

  return {
    file,
    meta,
    thumb,
    corner,
    checks,
    blocked: checks.some((c) => c.state === "fail"),
    overQuota,
    costPoints,
  };
}
