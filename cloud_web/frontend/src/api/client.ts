import axios from "axios";
import { ElMessage } from "element-plus";

export interface UploadPolicy {
  host: string;
  accessid: string;
  policy: string;
  signature: string;
  key: string;
  expire_at: string;
  max_bytes: number;
}

/** 当前用户的额度快照，预检卡用它判断「这条能不能传」。 */
export interface Quota {
  is_member: boolean;
  points: number;
  max_seconds: number;
  minutes_used: number;
  minutes_limit: number;
}

export interface UserProfile {
  id: number;
  phone: string;
  nickname: string;
  avatar: string;
  is_staff: boolean;
  points: number;
  quota?: Quota;
}

export interface TaskDetail {
  id: string;
  status: TaskStatus;
  progress: number;
  duration_seconds: number;
  size_bytes: number;
  width: number;
  height: number;
  fps: number;
  metrics: Record<string, number | boolean>;
  all_green: boolean;
  points_cost: number;
  error_code: string;
  error_message: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** 原片签名地址（对比视图用） */
  preview_url?: string;
  /** 成片签名地址 */
  download_url?: string;
  report_url?: string;
}

export type TaskStatus =
  | "awaiting_upload"
  | "pending"
  | "leased"
  | "running"
  | "verifying"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "expired";

export interface TaskListItem {
  id: string;
  status: TaskStatus;
  progress: number;
  duration_seconds: number;
  width: number;
  height: number;
  created_at: string;
  finished_at: string | null;
}

export const http = axios.create({ baseURL: "/api/v1", timeout: 30_000 });

http.interceptors.request.use((config) => {
  const token = localStorage.getItem("token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

http.interceptors.response.use(
  (res) => res,
  (error) => {
    const status = error.response?.status;
    const payload = error.response?.data;
    const message = payload?.message || "网络异常，请稍后重试";
    if (status === 401) {
      localStorage.removeItem("token");
      // 交给路由守卫处理跳转，这里只提示
      ElMessage.warning("登录已过期，请重新登录");
    } else {
      ElMessage.error(message);
    }
    return Promise.reject(error);
  }
);

export const api = {
  /** 发送登录验证码。dev_code 只在本地开发（console 通道）时返回。 */
  smsSend: (phone: string) =>
    http.post<{ retry_after: number; dev_code?: string }>("/auth/sms/send", { phone }),
  /** 验证码登录：新手机号自动建号。 */
  smsLogin: (phone: string, code: string) =>
    http.post<{ token: string; user: UserProfile; is_new: boolean }>("/auth/sms/login", {
      phone,
      code,
    }),
  me: () => http.get<UserProfile>("/auth/me"),

  signUpload: (size: number) =>
    http.post<{ task_id: string; upload: UploadPolicy }>("/uploads/sign", { size }),

  confirmUpload: (
    taskId: string,
    meta: { duration_ms: number; width: number; height: number; fps: number }
  ) => http.post(`/tasks/${taskId}/confirm`, meta),

  taskDetail: (taskId: string) => http.get<TaskDetail>(`/tasks/${taskId}`),
  taskList: () => http.get<TaskListItem[]>("/tasks"),
  cancelTask: (taskId: string) => http.post(`/tasks/${taskId}/cancel`),

  signIn: () => http.post<{ already?: boolean; capped?: boolean; points?: number; streak?: number }>(
    "/billing/sign-in"
  ),
  points: () => http.get("/billing/points"),
};

/** 浏览器直传 OSS。fetch 拿不到上传进度，所以用 XHR。 */
export function uploadToOss(
  policy: UploadPolicy,
  file: File,
  onProgress?: (ratio: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("key", policy.key);
    form.append("policy", policy.policy);
    form.append("OSSAccessKeyId", policy.accessid);
    form.append("signature", policy.signature);
    form.append("success_action_status", "200");
    form.append("file", file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", policy.host, true);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(1);
        resolve();
      } else {
        reject(new Error(`上传失败（${xhr.status}），请检查网络后重试`));
      }
    };
    xhr.onerror = () => reject(new Error("上传中断，请检查网络后重试"));
    xhr.ontimeout = () => reject(new Error("上传超时，请重试"));
    xhr.send(form);
  });
}

export interface VideoMeta {
  duration_ms: number;
  width: number;
  height: number;
  fps: number;
}

/** 读本地视频的时长/分辨率（浏览器读不到帧率，交给服务端 ffprobe 补）。 */
export function probeVideo(file: File): Promise<VideoMeta> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      resolve({
        duration_ms: Math.round((video.duration || 0) * 1000),
        width: video.videoWidth,
        height: video.videoHeight,
        fps: 0,
      });
      URL.revokeObjectURL(url);
      video.remove();
    };
    video.onerror = () => {
      URL.revokeObjectURL(url);
      video.remove();
      reject(new Error("无法读取该视频，请换一个文件"));
    };
    video.src = url;
  });
}

/** 从本地视频抓一帧画到 canvas，用来做水印粗定位和缩略图。 */
export function grabFrame(file: File, atSecond = 0.5, maxWidth = 320): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.preload = "auto";
    video.muted = true;
    video.playsInline = true;
    video.onloadeddata = () => {
      video.currentTime = Math.min(atSecond, (video.duration || 1) / 2);
    };
    video.onseeked = () => {
      const scale = Math.min(1, maxWidth / (video.videoWidth || maxWidth));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round((video.videoWidth || maxWidth) * scale);
      canvas.height = Math.round((video.videoHeight || maxWidth) * scale);
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        URL.revokeObjectURL(url);
        reject(new Error("无法创建画布"));
        return;
      }
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const dataUrl = canvas.toDataURL("image/jpeg", 0.72);
      URL.revokeObjectURL(url);
      video.remove();
      resolve(dataUrl);
    };
    video.onerror = () => {
      URL.revokeObjectURL(url);
      video.remove();
      reject(new Error("抽帧失败"));
    };
    video.src = url;
  });
}

/** 用缩略图猜水印大概在哪个角（白字水印通常比背景亮）。只用于给用户预期，判错也不拦。 */
export function guessWatermarkCorner(
  image: HTMLImageElement
): { corner: string; confidence: number } | null {
  const w = 96;
  const h = Math.max(1, Math.round((image.height / image.width) * w));
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;
  ctx.drawImage(image, 0, 0, w, h);
  const { data } = ctx.getImageData(0, 0, w, h);

  const lum = (x: number, y: number) => {
    const i = (y * w + x) * 4;
    return 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
  };

  let global = 0;
  for (let y = 0; y < h; y += 1) for (let x = 0; x < w; x += 1) global += lum(x, y);
  global /= w * h;

  const bw = Math.max(6, Math.round(w * 0.34));
  const bh = Math.max(4, Math.round(h * 0.34));
  const corners = [
    { name: "左上角", x0: 0, y0: 0 },
    { name: "右上角", x0: w - bw, y0: 0 },
    { name: "左下角", x0: 0, y0: h - bh },
    { name: "右下角", x0: w - bw, y0: h - bh },
  ];

  let best = { name: "", delta: -Infinity };
  for (const c of corners) {
    let sum = 0;
    for (let y = c.y0; y < c.y0 + bh; y += 1) for (let x = c.x0; x < c.x0 + bw; x += 1) sum += lum(x, y);
    const delta = sum / (bw * bh) - global;
    if (delta > best.delta) best = { name: c.name, delta };
  }

  // 差得太少说明整片均匀，不猜
  if (best.delta < 10) return null;
  return { corner: best.name, confidence: Math.min(1, (best.delta - 10) / 40) };
}

export function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m} 分 ${s} 秒`;
}

export function formatSize(bytes: number): string {
  if (!bytes) return "—";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
