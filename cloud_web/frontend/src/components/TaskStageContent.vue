<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import BoxSelector from "@/components/BoxSelector.vue";
import CompareSlider from "@/components/CompareSlider.vue";
import { api, formatDuration, formatSize, type BBox, type TaskDetail } from "@/api/client";

const props = defineProps<{ task: TaskDetail; workerOffline?: boolean; queueAhead?: number | null }>();
const emit = defineEmits<{ updated: [] }>();
const frameIndex = ref(0);
const frameReady = ref(false);
const frameAttempt = ref(0);
const bbox = ref<BBox | null>(null);
const submitting = ref(false);
const video = ref<HTMLVideoElement | null>(null);
const previewUrl = ref("");
const frameUrls = ref<string[]>([]);
let mediaSetAt = 0;
const isConfirm = computed(() => ["awaiting_confirmation", "needs_manual"].includes(props.task.status));
const isQueue = computed(() => ["pending", "leased"].includes(props.task.status) && !props.task.params?.bbox_confirmed);
const isRender = computed(() => ["removing_watermark", "running"].includes(props.task.status) || (["pending", "leased"].includes(props.task.status) && Boolean(props.task.params?.bbox_confirmed)));
const frames = computed(() => frameUrls.value.slice(0, 5));
const currentFrame = computed(() => frames.value[frameIndex.value] || "");
const frameWidth = computed(() => props.task.frame_width || props.task.width || 1920);
const frameHeight = computed(() => props.task.frame_height || props.task.height || 1080);
const boxValid = computed(() => {
  const b = bbox.value;
  return Boolean(b && b[0] >= 0 && b[1] >= 0 && b[2] >= 8 && b[3] >= 8 && b[0] + b[2] <= frameWidth.value && b[1] + b[3] <= frameHeight.value);
});
watch(() => props.task.id, () => { bbox.value = null; frameIndex.value = 0; previewUrl.value = ""; frameUrls.value = []; mediaSetAt = 0; });
watch(() => props.task, (next) => {
  if (!mediaSetAt || Date.now() - mediaSetAt > 8 * 60_000) {
    previewUrl.value = next.preview_url || "";
    frameUrls.value = next.keyframe_urls || [];
    mediaSetAt = Date.now();
  } else {
    if (!previewUrl.value && next.preview_url) previewUrl.value = next.preview_url;
    if (!frameUrls.value.length && next.keyframe_urls?.length) frameUrls.value = next.keyframe_urls;
  }
}, { immediate: true });
watch(() => props.task.locate_candidates, (list) => {
  if (!bbox.value && list?.length) bbox.value = [list[0].x, list[0].y, list[0].w, list[0].h];
}, { immediate: true });
watch(frames, (list) => { if (frameIndex.value >= list.length) frameIndex.value = 0; });
watch(currentFrame, () => { frameReady.value = false; });
let carousel: number | undefined;
let frameRetry: number | undefined;
watch(() => props.task.status, (status) => {
  if (carousel) window.clearInterval(carousel);
  if (frameRetry) window.clearInterval(frameRetry);
  carousel = undefined;
  frameRetry = undefined;
  if (status === "extracting_frames" || status === "locating_watermark") {
    carousel = window.setInterval(() => { if (frames.value.length > 1) frameIndex.value = (frameIndex.value + 1) % frames.value.length; }, 3500);
    frameRetry = window.setInterval(() => { if (!frameReady.value) frameAttempt.value++; }, 5000);
  }
}, { immediate: true });
onBeforeUnmount(() => { if (carousel) window.clearInterval(carousel); if (frameRetry) window.clearInterval(frameRetry); });
async function submit() {
  if (!boxValid.value || submitting.value) return;
  submitting.value = true;
  try {
    await api.submitBbox(props.task.id, bbox.value!);
    ElMessage.success("水印位置已确认，开始排队去水印");
    emit("updated");
  } finally { submitting.value = false; }
}
function play() { void video.value?.play(); }
</script>

<template>
  <section class="stage-content">
    <template v-if="isQueue || task.status === 'awaiting_upload'">
      <div class="stage-copy">
        <span class="eyebrow">{{ isQueue ? '等待算力节点' : '视频信息' }}</span>
        <h2 class="h2">{{ isQueue ? '视频已就绪，正在排队' : '视频已上传' }}</h2>
        <p class="small secondary">{{ formatDuration(task.duration_seconds) }} · {{ task.width }} × {{ task.height }} · {{ formatSize(task.size_bytes) }}</p>
        <div v-if="isQueue" class="queue-signal" role="status"><span class="queue-dots"><i /><i /><i /></span>{{ task.status === 'leased' ? '已分配算力节点' : '正在等待空闲算力' }}<span v-if="queueAhead"> · 前面还有 {{ queueAhead }} 条</span></div>
        <p v-if="workerOffline" class="micro muted">算力节点暂时离线，任务会留在队列中。</p>
      </div>
      <div class="video-preview">
        <video v-if="previewUrl" ref="video" :src="previewUrl" controls playsinline preload="metadata" aria-label="播放原视频" />
        <div v-else class="media-placeholder">原视频预览暂不可用</div>
        <button v-if="previewUrl" class="play-badge" type="button" aria-label="播放原视频" @click="play">▶</button>
      </div>
    </template>

    <template v-else-if="task.status === 'extracting_frames' || task.status === 'locating_watermark'">
      <div class="stage-heading"><div><span class="eyebrow">画面分析</span><h2 class="h2">{{ task.status === 'extracting_frames' ? '正在获取关键帧' : '正在定位水印' }}</h2></div><span class="micro muted">{{ frames.length ? `${frameIndex + 1} / ${frames.length}` : '等待画面生成' }}</span></div>
      <div v-if="currentFrame" class="frame-stage"><img :key="`${frameIndex}-${frameAttempt}`" :src="currentFrame" :alt="`关键帧 ${frameIndex + 1}`" :style="{ visibility: frameReady ? 'visible' : 'hidden' }" @load="frameReady = true" @error="frameReady = false" /><span v-if="!frameReady" class="frame-loading">关键帧正在生成…</span><span class="frame-index num">{{ String(frameIndex + 1).padStart(2, '0') }}</span></div>
      <div v-else class="frame-placeholder"><span class="scan-line" />正在分析视频画面，关键帧生成后会在这里轮播</div>
      <div v-if="frames.length > 1" class="frame-nav"><button v-for="(_, i) in frames" :key="i" type="button" :class="{ on: i === frameIndex }" :aria-label="`查看第 ${i + 1} 张关键帧`" @click="frameIndex = i" /></div>
      <p v-if="task.status === 'locating_watermark'" class="small secondary">定位完成后会显示红框，你可以调整位置再确认。</p>
    </template>

    <template v-else-if="isConfirm">
      <div class="stage-heading"><div><span class="eyebrow">需要你确认</span><h2 class="h2">检查水印位置</h2></div><span class="tag tag-warn">等待确认</span></div>
      <p class="small secondary intro">红框是建议位置。拖动、拉角调整，或在画面上重新框选。当前支持矩形区域。</p>
      <BoxSelector v-model="bbox" :video-url="previewUrl" :image-urls="frames" :width="frameWidth" :height="frameHeight" :candidates="task.locate_candidates || []" :duration-seconds="task.duration_seconds || 10" />
      <div class="confirm-actions"><button class="btn btn-primary" type="button" :disabled="!boxValid || submitting" @click="submit">{{ submitting ? '提交中…' : '确认位置，开始去水印' }}</button><span class="micro muted">确认后才会开始处理，不重复扣积分</span></div>
    </template>

    <template v-else-if="isRender">
      <div class="stage-heading"><div><span class="eyebrow">逐帧修复</span><h2 class="h2">{{ task.status === 'pending' || task.status === 'leased' ? '等待去水印' : '正在去除水印' }}</h2></div><strong v-if="task.status !== 'pending' && task.status !== 'leased'" class="num">{{ Math.round(task.progress * 100) }}%</strong></div>
      <div class="repair-visual" aria-label="去水印处理中"><div class="repair-grain" /><div class="repair-mark">水印区域</div><div class="repair-sweep" /><div class="repair-clean">画面修复中</div></div>
      <div v-if="task.status !== 'pending' && task.status !== 'leased'" class="bar repair-progress"><i :style="{ width: `${Math.max(3, task.progress * 100)}%` }" /></div>
      <p class="micro muted">{{ task.status === 'pending' || task.status === 'leased' ? '位置已确认，等待算力节点继续处理' : '正在按确认的区域逐帧修复画面' }}</p>
    </template>

    <template v-else-if="task.status === 'verifying'">
      <div class="stage-heading"><div><span class="eyebrow">质量验收</span><h2 class="h2">正在检查修复效果</h2></div><span class="tag tag-brand">验收中</span></div>
      <div v-if="previewUrl && task.download_url" class="compare-wrap"><CompareSlider :before="previewUrl" :after="task.download_url" :aspect="task.width / task.height" /></div>
      <div v-else class="verify-panes"><div><span>原片</span><video v-if="previewUrl" :src="previewUrl" muted playsinline preload="metadata" /><span v-else class="pane-wait">原片预览</span></div><div><span>修复后</span><span class="pane-wait">成片生成后展示</span></div></div>
      <p class="small secondary">正在核对水印残留、画面纹理和闪烁。验收完成后可拖动分割线对比原片与成片。</p>
    </template>
  </section>
</template>

<style scoped>
.stage-content { min-height: 260px; padding-top: 24px; border-top: 1px solid var(--line-soft); }
.stage-heading { display: flex; align-items: start; justify-content: space-between; gap: 16px; margin-bottom: 17px; }
.stage-heading .h2, .stage-copy .h2 { margin: 6px 0; }
.stage-copy { margin-bottom: 22px; }
.stage-copy p, .stage-content > p { margin-top: 10px; }
.intro { margin-bottom: 16px; }
.video-preview { position: relative; display: grid; place-items: center; background: #132621; border-radius: 14px; overflow: hidden; max-height: 480px; }
.video-preview video { width: 100%; max-height: 480px; display: block; }
.media-placeholder { padding: 110px 20px; color: #d9e5dc; }
.play-badge { position: absolute; top: 14px; right: 14px; border: 0; border-radius: 50%; width: 42px; height: 42px; background: rgba(255,255,255,.9); color: #163b31; cursor: pointer; }
.queue-signal { display: inline-flex; align-items: center; gap: 10px; margin-top: 17px; padding: 11px 15px; border-radius: 12px; background: var(--brand-wash); color: var(--brand); font-weight: 700; font-size: 13px; }
.queue-dots { display: inline-flex; gap: 3px; }
.queue-dots i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; animation: dot 1.1s infinite alternate; }
.queue-dots i:nth-child(2) { animation-delay: .2s; }.queue-dots i:nth-child(3) { animation-delay: .4s; }
.frame-stage, .frame-placeholder { position: relative; display: grid; place-items: center; min-height: 280px; max-height: 520px; overflow: hidden; border-radius: 14px; background: #142620; color: #c7d9cf; }
.frame-stage img { display: block; width: 100%; max-height: 520px; object-fit: contain; }
.frame-index { position: absolute; bottom: 14px; right: 16px; color: white; }
.frame-loading { position: absolute; color: #c7d9cf; font-size: 13px; }
.frame-placeholder { background: repeating-linear-gradient(0deg,#172d27,#172d27 3px,#1a332b 4px); }
.scan-line { position: absolute; left: 0; right: 0; height: 2px; background: #69b99a; box-shadow: 0 0 24px 5px #69b99a66; animation: scan 3s ease-in-out infinite alternate; }
.frame-nav { display: flex; justify-content: center; gap: 8px; margin: 14px 0; }
.frame-nav button { width: 28px; height: 5px; padding: 0; border: 0; border-radius: 9px; background: var(--line-strong); cursor: pointer; }.frame-nav button.on { background: var(--brand); }
.confirm-actions { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-top: 20px; }
.repair-visual { position: relative; height: 310px; overflow: hidden; border-radius: 14px; background: radial-gradient(circle at 30% 25%,#628073,#203f35 50%,#102b24); }
.repair-grain { position: absolute; inset: 0; opacity: .25; background: repeating-linear-gradient(90deg,transparent 0 18px,#9ccbb944 19px 20px); }
.repair-mark { position: absolute; top: 37%; left: 35%; padding: 17px 28px; border: 2px solid #fb7266; color: #ffd6d1; background: #ad454433; letter-spacing: .18em; }
.repair-sweep { position: absolute; top: 0; bottom: 0; left: -30%; width: 32%; transform: skewX(-16deg); background: linear-gradient(90deg,transparent,#b0ecd750,transparent); animation: sweep 3.4s ease-in-out infinite; }
.repair-clean { position: absolute; right: 20px; bottom: 20px; color: #d4efe3; font-size: 12px; letter-spacing: .15em; }
.repair-progress { margin-top: 17px; }
.verify-panes { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }.verify-panes > div { display: flex; flex-direction: column; gap: 8px; }.verify-panes video, .pane-wait { width: 100%; aspect-ratio: 16/9; object-fit: contain; border-radius: 12px; background: #19342a; color: #d4eadf; }.pane-wait { display: grid; place-items: center; }
@keyframes dot { to { opacity: .18; transform: translateY(-3px); } }@keyframes scan { from { top: 8%; } to { top: 90%; } }@keyframes sweep { to { left: 120%; } }
@media (prefers-reduced-motion: reduce) { .queue-dots i,.scan-line,.repair-sweep { animation: none; } }
@media (max-width: 640px) { .stage-content { padding-top: 18px; }.verify-panes { grid-template-columns: 1fr; }.repair-visual { height: 230px; } }
</style>
