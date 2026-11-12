<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api, formatSize, grabFrame, uploadToOss, type Quota } from "@/api/client";
import { preflight, type PreflightReport } from "@/composables/usePreflight";
import { useUserStore } from "@/stores/user";

const props = defineProps<{ quota: Quota | null }>();
const emit = defineEmits<{ (e: "submitted", taskId: string): void }>();

const router = useRouter();
const user = useUserStore();

const over = ref(false);
const probing = ref(false);
const uploading = ref(false);
const uploadRatio = ref(0);
const report = ref<PreflightReport | null>(null);
const errorMsg = ref("");
const stage = ref("");
const inputRef = ref<HTMLInputElement | null>(null);
const localVideoUrl = ref("");
const showPreviewPlayer = ref(false);

function releaseVideoUrl() {
  if (localVideoUrl.value) URL.revokeObjectURL(localVideoUrl.value);
  localVideoUrl.value = "";
  showPreviewPlayer.value = false;
}

const maxSecondsText = computed(() => {
  const s = props.quota?.max_seconds ?? 30;
  return s >= 60 ? `${Math.round(s / 60)} 分钟` : `${s} 秒`;
});

const orientation = computed(() => {
  const m = report.value?.meta;
  if (!m?.width) return "";
  return m.height > m.width ? "竖屏" : m.height === m.width ? "方形" : "横屏";
});

const costText = computed(() => {
  const r = report.value;
  if (!r) return "";
  if (props.quota?.is_member) return "订阅内，不扣积分";
  return `预计消耗 ${r.costPoints} 积分`;
});

const primaryLabel = computed(() => {
  const r = report.value;
  if (!r) return "开始处理";
  if (r.blocked) return "换一个文件";
  if (r.overQuota) return "积分不足，去签到";
  return "开始处理";
});

function openPicker() {
  if (probing.value || uploading.value) return;
  inputRef.value?.click();
}

function onInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  if (file) handleFile(file);
  input.value = ""; // 允许重复选同一个文件
}

function onDrop(e: DragEvent) {
  over.value = false;
  const file = e.dataTransfer?.files?.[0];
  if (file) handleFile(file);
}

function onDragOver(e: DragEvent) {
  e.preventDefault();
  if (!probing.value && !uploading.value) over.value = true;
}

function onPaste(e: ClipboardEvent) {
  const items = Array.from(e.clipboardData?.items ?? []);
  const item = items.find((i) => i.kind === "file");
  const file = item?.getAsFile();
  if (file && file.type.startsWith("video/")) {
    e.preventDefault();
    handleFile(file);
  }
}

async function handleFile(file: File) {
  if (probing.value || uploading.value) return;
  errorMsg.value = "";
  report.value = null;
  releaseVideoUrl();
  probing.value = true;
  try {
    report.value = await preflight(file, props.quota);
    localVideoUrl.value = URL.createObjectURL(file);
  } catch {
    errorMsg.value = "读取视频失败，请换一个文件";
  } finally {
    probing.value = false;
  }
}

function reset() {
  releaseVideoUrl();
  report.value = null;
  errorMsg.value = "";
  uploadRatio.value = 0;
}

async function submit() {
  const r = report.value;
  if (!r || probing.value || uploading.value) return;
  if (r.blocked) {
    reset();
    return;
  }
  if (r.overQuota) {
    router.push({ name: "points" });
    return;
  }

  uploading.value = true;
  uploadRatio.value = 0;
  errorMsg.value = "";
  try {
    stage.value = "获取上传凭证";
    const { data: sign } = await api.signUpload(r.file.size);

    stage.value = "上传中";
    await uploadToOss(sign.upload, r.file, (p) => {
      uploadRatio.value = p;
      if (p >= 1) stage.value = "等待对象存储确认";
    });

    stage.value = "提交处理";
    let firstFrame = "";
    try {
      firstFrame = await grabFrame(r.file, 0.001, 320);
    } catch {
      firstFrame = r.thumb;
    }
    await api.confirmUpload(sign.task_id, {
      duration_ms: r.meta.duration_ms,
      width: r.meta.width,
      height: r.meta.height,
      fps: r.meta.fps,
      first_frame: firstFrame,
    });
    await user.refresh();
    ElMessage.success("已进入处理队列");
    emit("submitted", sign.task_id);
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || e?.message || "上传失败，请重试";
  } finally {
    uploading.value = false;
    stage.value = "";
  }
}

onMounted(() => window.addEventListener("paste", onPaste));
onBeforeUnmount(() => { window.removeEventListener("paste", onPaste); releaseVideoUrl(); });
</script>

<template>
  <input ref="inputRef" type="file" accept="video/*" class="sr-only" @change="onInputChange" />

  <!-- 上传中 -->
  <div v-if="uploading" class="card panel">
    <div class="row-between" style="margin-bottom: 14px">
      <div class="grow">
        <div class="micro muted" style="margin-bottom: 3px">{{ stage }}</div>
        <div class="filename">{{ report?.file.name }}</div>
      </div>
      <span class="num h2">{{ Math.round(uploadRatio * 100) }}%</span>
    </div>
    <div class="bar"><i :style="{ width: `${Math.max(2, uploadRatio * 100)}%` }" /></div>
    <p class="micro muted" style="margin-top: 12px">
      {{ formatSize((report?.file.size ?? 0) * uploadRatio) }} / {{ formatSize(report?.file.size ?? 0) }}
      　直传对象存储，不经过我们的服务器
    </p>
    <video v-if="localVideoUrl" class="upload-preview" :src="localVideoUrl" :poster="report?.thumb || undefined" controls playsinline preload="metadata" aria-label="播放待上传的视频" />
  </div>

  <!-- 预检卡 -->
  <div v-else-if="report" class="card panel">
    <div class="head">
      <div class="thumb">
        <video v-if="showPreviewPlayer && localVideoUrl" :src="localVideoUrl" controls autoplay playsinline aria-label="播放待上传的视频" />
        <button v-else-if="report.thumb" class="thumb-play" type="button" aria-label="播放待上传的视频" @click="showPreviewPlayer = true"><img :src="report.thumb" alt="视频首帧" /><span>▶</span></button>
        <span v-else class="micro muted">无预览</span>
      </div>
      <div class="grow">
        <div class="filename">{{ report.file.name }}</div>
        <div class="micro muted num" style="margin-top: 3px">
          {{ orientation }} {{ report.meta.width }}×{{ report.meta.height }}
          <template v-if="report.meta.duration_ms">
            　·　{{ (report.meta.duration_ms / 1000).toFixed(1) }} 秒
          </template>
          　·　{{ formatSize(report.file.size) }}
        </div>
      </div>
    </div>

    <hr class="divider" />

    <ul class="checks">
      <li v-for="c in report.checks" :key="c.label" class="check" :class="`s-${c.state}`">
        <span class="mark" aria-hidden="true">
          <svg v-if="c.state === 'pass'" viewBox="0 0 12 12">
            <path d="M2.4 6.2l2.3 2.3 4.9-5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <svg v-else-if="c.state === 'fail'" viewBox="0 0 12 12">
            <path d="M3.4 3.4l5.2 5.2M8.6 3.4L3.4 8.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          </svg>
          <svg v-else viewBox="0 0 12 12">
            <circle cx="6" cy="6" r="2.2" fill="currentColor" />
          </svg>
        </span>
        <span class="check-label">{{ c.label }}</span>
        <span class="check-detail">{{ c.detail }}</span>
      </li>
    </ul>

    <div v-if="errorMsg" class="notice notice-bad" style="margin-top: 14px">{{ errorMsg }}</div>

    <div v-if="report.blocked" class="notice notice-bad" style="margin-top: 14px">
      这条视频暂时处理不了。换一条符合要求的，或者先把视频压缩 / 裁剪一下。
    </div>

    <hr class="divider" />

    <div class="actions">
      <span class="micro muted">{{ costText }}</span>
      <div class="row" style="gap: 8px">
        <button class="btn btn-secondary" type="button" @click="reset">重新选择</button>
        <button
          class="btn btn-primary"
          type="button"
          :disabled="report.blocked || report.overQuota"
          @click="submit"
        >
          {{ primaryLabel }}
        </button>
      </div>
    </div>
  </div>

  <!-- 空态：拖拽区 -->
  <div
    v-else
    class="drop"
    :class="{ over, busy: probing }"
    role="button"
    tabindex="0"
    @click="openPicker"
    @keydown.enter.prevent="openPicker"
    @keydown.space.prevent="openPicker"
    @dragover="onDragOver"
    @dragleave.prevent="over = false"
    @drop.prevent="onDrop"
  >
    <div class="drop-icon">
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <path d="M10 14V6M10 6L6.8 9.2M10 6l3.2 3.2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
        <path d="M4 13v2a1.6 1.6 0 0 0 1.6 1.6h8.8A1.6 1.6 0 0 0 16 15v-2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
      </svg>
    </div>

    <template v-if="probing">
      <div class="drop-title">正在读取视频信息…</div>
      <div class="drop-meta">只在本地读取，还没有开始上传</div>
    </template>
    <template v-else>
      <div class="drop-title">{{ over ? "松手就上传" : "拖入视频，或点击选择" }}</div>
      <div class="drop-meta">mp4 · mov · avi · mkv　　单条 ≤ 500MB　·　≤ {{ maxSecondsText }}</div>
      <div class="drop-paste">也支持 Ctrl + V 直接粘贴</div>
    </template>
  </div>

  <div v-if="errorMsg && !report" class="notice notice-bad" style="margin-top: 12px">
    {{ errorMsg }}
  </div>
</template>

<style scoped>
.panel {
  padding: var(--sp-5);
}
.upload-preview { display: block; width: 100%; max-height: 340px; margin-top: 18px; border-radius: 12px; background: #142620; }
.thumb-play { position: relative; display: block; width: 100%; height: 100%; padding: 0; border: 0; cursor: pointer; background: #142620; }
.thumb-play img { width: 100%; height: 100%; object-fit: cover; }
.thumb-play span { position: absolute; inset: 0; display: grid; place-items: center; color: white; font-size: 24px; text-shadow: 0 2px 10px #102a25; }

/* ── 拖拽区 ─────────────────────────────── */
.drop {
  border: 1.5px dashed #c9cfc9;
  border-radius: var(--r-card);
  background: #fdfcfa;
  min-height: 260px;
  padding: var(--sp-6) var(--sp-5);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  cursor: pointer;
  transition: background-color 0.18s, border-color 0.18s;
}

.drop:hover {
  border-color: var(--ink-300);
}

.drop.over {
  border-color: var(--brand);
  border-style: solid;
  background: var(--brand-wash);
}

.drop.busy {
  cursor: progress;
}

.drop-icon {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--brand-wash);
  color: var(--brand);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 14px;
}

.drop-icon svg {
  width: 20px;
  height: 20px;
}

.drop-title {
  font-size: var(--fs-body);
  font-weight: 500;
  color: var(--ink-900);
}

.drop-meta {
  font-size: var(--fs-micro);
  color: var(--ink-400);
  margin-top: 6px;
}

.drop-paste {
  font-size: var(--fs-micro);
  color: var(--ink-300);
  margin-top: 14px;
}

/* ── 预检卡 ─────────────────────────────── */
.head {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
}

.thumb {
  width: 88px;
  height: 56px;
  flex-shrink: 0;
  border-radius: 8px;
  overflow: hidden;
  background: var(--surface-sunken);
  border: 1px solid var(--line-soft);
  display: flex;
  align-items: center;
  justify-content: center;
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.thumb video { width: 100%; height: 100%; object-fit: cover; }

.filename {
  font-size: var(--fs-small);
  font-weight: 500;
  color: var(--ink-900);
  word-break: break-all;
  line-height: 1.45;
}

.checks {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 20px;
}

.check {
  display: flex;
  align-items: baseline;
  gap: 7px;
  font-size: var(--fs-micro);
  line-height: 1.5;
}

.mark {
  width: 12px;
  height: 12px;
  flex-shrink: 0;
  transform: translateY(1.5px);
}

.mark svg {
  width: 12px;
  height: 12px;
  display: block;
}

.s-pass .mark {
  color: var(--ok);
}

.s-fail .mark {
  color: var(--bad);
}

.s-info .mark {
  color: var(--warn);
}

.check-label {
  color: var(--ink-600);
  flex-shrink: 0;
}

.check-detail {
  color: var(--ink-900);
}

.s-fail .check-detail {
  color: var(--bad);
}

.actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  flex-wrap: wrap;
}

@media (max-width: 640px) {
  .drop {
    min-height: 200px;
    padding: var(--sp-5) var(--sp-4);
  }

  .checks {
    grid-template-columns: 1fr;
  }

  .actions {
    flex-direction: column;
    align-items: stretch;
  }

  .actions > .row {
    justify-content: stretch;
  }

  .actions .btn {
    flex: 1;
  }
}
</style>
