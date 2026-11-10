<script setup lang="ts">
/**
 * 水印框选器（M3.1）。
 *
 * 关键设计：不把视频画到 canvas，而是让 <video> 自己显示 seek 后的当前帧，
 * 上面盖一层 viewBox 与原片像素 1:1 的 SVG 画框。这样：
 *   · 不读 canvas 像素 → OSS 没配 CORS 也能用（drawImage 会污染但显示无碍）
 *   · 坐标不做二次换算 → viewBox 坐标即提交给后端的 bbox
 *   · 触屏也能用（统一 pointer 事件 + setPointerCapture）
 *
 * 坐标一律是【原片像素】：x, y, w, h。
 */
import { computed, onMounted, ref, watch } from "vue";
import type { BBox, LocateCandidate } from "@/api/client";

const props = withDefaults(
  defineProps<{
    modelValue: BBox | null;
    videoUrl?: string;
    imageUrls?: string[];
    width?: number;
    height?: number;
    candidates?: LocateCandidate[];
    durationSeconds?: number;
  }>(),
  { videoUrl: "", imageUrls: () => [], width: 1920, height: 1080, candidates: () => [], durationSeconds: 10 }
);

const emit = defineEmits<{ "update:modelValue": [box: BBox | null] }>();

const stageRef = ref<HTMLDivElement | null>(null);
const videoRef = ref<HTMLVideoElement | null>(null);
const mediaError = ref(false);
const mediaReady = ref(false);
const seek = ref(0);
const selectedFrame = ref(0);
const videoDuration = ref(0);

const W = computed(() => props.width || 1920);
const H = computed(() => props.height || 1080);
const duration = computed(() => Math.max(0.2, props.durationSeconds || 10));
const box = computed(() => props.modelValue);

/** SVG 里的字号/线宽按原片宽度缩放，否则 4K 片上线细得看不见。 */
const unit = computed(() => Math.max(1, W.value / 900));
const strokeW = computed(() => 2 * unit.value);
const fontSize = computed(() => 15 * unit.value);
const handle = computed(() => 11 * unit.value);

const MIN_BOX = 8;

function clamp(v: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, v));
}

function commit(next: BBox | null) {
  emit("update:modelValue", next);
}

/** 把屏幕坐标换成原片像素坐标。 */
function toFrame(e: PointerEvent): { x: number; y: number } {
  const rect = stageRef.value!.getBoundingClientRect();
  return {
    x: clamp(((e.clientX - rect.left) / rect.width) * W.value, 0, W.value),
    y: clamp(((e.clientY - rect.top) / rect.height) * H.value, 0, H.value),
  };
}

type Drag =
  | { kind: "new"; sx: number; sy: number }
  | { kind: "move"; dx: number; dy: number }
  | { kind: "resize"; corner: number };

let drag: Drag | null = null;

function onDown(e: PointerEvent) {
  if (e.button !== 0 && e.pointerType === "mouse") return;
  const el = e.target as Element | null;
  const role = el?.getAttribute("data-role");
  const p = toFrame(e);

  if (role === "candidate") {
    const idx = Number(el?.getAttribute("data-index"));
    const c = props.candidates[idx];
    if (c) commit([c.x, c.y, c.w, c.h]);
    return;
  }
  if (role === "corner") {
    drag = { kind: "resize", corner: Number(el?.getAttribute("data-corner")) };
  } else if (role === "move" && box.value) {
    drag = { kind: "move", dx: p.x - box.value[0], dy: p.y - box.value[1] };
  } else {
    drag = { kind: "new", sx: p.x, sy: p.y };
    commit(null);
  }
  stageRef.value?.setPointerCapture(e.pointerId);
  e.preventDefault();
}

function onMove(e: PointerEvent) {
  if (!drag) return;
  const p = toFrame(e);
  if (drag.kind === "new") {
    const x = Math.min(drag.sx, p.x);
    const y = Math.min(drag.sy, p.y);
    const w = Math.abs(p.x - drag.sx);
    const h = Math.abs(p.y - drag.sy);
    if (w < MIN_BOX || h < MIN_BOX) return;
    commit([Math.round(x), Math.round(y), Math.round(w), Math.round(h)]);
    return;
  }
  const cur = box.value;
  if (!cur) return;
  let [x, y, w, h] = cur;

  if (drag.kind === "move") {
    x = clamp(p.x - drag.dx, 0, W.value - w);
    y = clamp(p.y - drag.dy, 0, H.value - h);
  } else {
    // 四个角：0 左上 / 1 右上 / 2 右下 / 3 左下，固定对角不动
    const c = drag.corner;
    const left = c === 0 || c === 3;
    const top = c === 0 || c === 1;
    const x2 = x + w;
    const y2 = y + h;
    const nx = left ? clamp(p.x, 0, x2 - MIN_BOX) : x;
    const ny = top ? clamp(p.y, 0, y2 - MIN_BOX) : y;
    const nx2 = left ? x2 : clamp(p.x, x + MIN_BOX, W.value);
    const ny2 = top ? y2 : clamp(p.y, y + MIN_BOX, H.value);
    x = nx;
    y = ny;
    w = nx2 - nx;
    h = ny2 - ny;
  }
  commit([Math.round(x), Math.round(y), Math.round(w), Math.round(h)]);
}

function onUp(e: PointerEvent) {
  if (!drag) return;
  drag = null;
  try {
    stageRef.value?.releasePointerCapture(e.pointerId);
  } catch {
    // pointer 已被浏览器回收，忽略
  }
}

function clearBox() {
  commit(null);
}

function applyCandidate(c: LocateCandidate) {
  commit([c.x, c.y, c.w, c.h]);
}

/** 数字输入精确微调。 */
function setField(i: number, raw: string) {
  const v = Math.max(0, Math.round(Number(raw) || 0));
  const cur = box.value ?? [0, 0, 200, 80];
  const next: BBox = [...cur] as BBox;
  next[i] = v;
  if (i === 2) next[2] = clamp(v, MIN_BOX, W.value - next[0]);
  if (i === 3) next[3] = clamp(v, MIN_BOX, H.value - next[1]);
  commit(next);
}

function onMeta() {
  const v = videoRef.value;
  if (!v) return;
  mediaReady.value = true;
  videoDuration.value = Number.isFinite(v.duration) ? v.duration : duration.value;
  // 第 0 帧常是黑场，默认停在 10% 处更容易看见水印
  seek.value = Math.min(videoDuration.value * 0.1, videoDuration.value - 0.05);
  v.currentTime = Math.max(0, seek.value);
}

function onSeek() {
  const v = videoRef.value;
  if (v && mediaReady.value) v.currentTime = seek.value;
}

function retryMedia() {
  mediaError.value = false;
  mediaReady.value = false;
  const v = videoRef.value;
  if (v) {
    v.load();
    if (v.currentTime !== seek.value) v.currentTime = seek.value;
  }
}

const timeText = computed(() => {
  const s = Math.max(0, seek.value);
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
});

// 有当前框时不再画候选框：候选 1 常常与自动套用的框完全重合，
// 两条框线 + 两组标签挤在一起反而看不清。清空当前框后候选重新出现供点选。
const candidateRects = computed(() =>
  box.value ? [] : props.candidates.slice(0, 5)
);
const allCandidates = computed(() => props.candidates.slice(0, 5));

/** 尺寸标签底板的估算宽度（等宽字体，字符宽约 0.62em）。 */
const labelW = computed(() => {
  const b = box.value;
  if (!b) return 0;
  const chars = `${b[2]} × ${b[3]}`.length;
  return chars * fontSize.value * 0.64 + fontSize.value * 0.8;
});
const labelAbove = computed(() => (box.value?.[1] ?? 0) > fontSize.value * 2.2);
const labelY = computed(() => {
  const b = box.value;
  if (!b) return 0;
  return labelAbove.value
    ? b[1] - strokeW.value * 3
    : b[1] + fontSize.value * 1.15 + strokeW.value * 3;
});

watch(
  () => props.videoUrl,
  () => {
    mediaError.value = false;
    mediaReady.value = false;
  }
);

onMounted(() => {
  if (!props.videoUrl) mediaError.value = true;
});

defineExpose({ clearBox, applyCandidate });
</script>

<template>
  <div class="selector">
    <div
      ref="stageRef"
      class="stage"
      :style="{ aspectRatio: `${W} / ${H}` }"
      @pointerdown="onDown"
      @pointermove="onMove"
      @pointerup="onUp"
      @pointercancel="onUp"
    >
      <img v-if="imageUrls.length" class="media" :src="imageUrls[selectedFrame]" alt="视频关键帧" @error="mediaError = true" />
      <video
        v-else
        v-show="!mediaError"
        ref="videoRef"
        class="media"
        :src="videoUrl"
        muted
        playsinline
        preload="metadata"
        @loadedmetadata="onMeta"
        @error="mediaError = true"
      />

      <!-- 预览不可用时的降级画面：仍可拖框、可用候选框与数字输入 -->
      <div v-if="mediaError" class="fallback">
        <svg viewBox="0 0 24 24" aria-hidden="true" class="fb-icon">
          <path d="M4 5.5h16v13H4zM4 15l5-5 3.5 3.5L15.5 10 20 14.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p class="small">预览画面加载不出来</p>
        <p class="micro muted">可以照候选框或凭印象填坐标；也可以刷新页面重取预览链接</p>
        <button class="btn btn-secondary btn-sm" type="button" @pointerdown.stop @click.stop="retryMedia">
          重试预览
        </button>
      </div>

      <svg
        class="overlay"
        :class="{ 'light-bg': mediaError }"
        :viewBox="`0 0 ${W} ${H}`"
        preserveAspectRatio="none"
        aria-label="水印框选区"
      >
        <!-- 框外压暗，突出框内内容；不吃指针事件 -->
        <path
          v-if="box && !mediaError"
          class="dim"
          :d="`M0 0H${W}V${H}H0Z M${box[0]} ${box[1]}h${box[2]}v${box[3]}h${-box[2]}Z`"
          fill-rule="evenodd"
        />

        <!-- 候选框（点击即套用） -->
        <g v-for="(c, i) in candidateRects" :key="`c${i}`">
          <rect
            class="cand"
            :x="c.x"
            :y="c.y"
            :width="c.w"
            :height="c.h"
            :stroke-width="strokeW"
            data-role="candidate"
            :data-index="i"
          />
          <rect
            class="label-bg"
            :x="c.x"
            :y="c.y > fontSize * 2.2 ? c.y - fontSize * 1.5 : c.y + strokeW * 2"
            :width="fontSize * 5.2"
            :height="fontSize * 1.5"
            :rx="fontSize * 0.25"
          />
          <text
            class="cand-label"
            :x="c.x + fontSize * 0.4"
            :y="c.y > fontSize * 2.2 ? c.y - fontSize * 0.4 : c.y + fontSize * 1.15 + strokeW * 2"
            :font-size="fontSize"
          >
            候选 {{ i + 1 }}
          </text>
        </g>

        <!-- 当前框 -->
        <g v-if="box">
          <rect
            class="chosen"
            :x="box[0]"
            :y="box[1]"
            :width="box[2]"
            :height="box[3]"
            :stroke-width="strokeW * 1.6"
            data-role="move"
          />
          <rect
            class="label-bg"
            :x="box[0]"
            :y="labelY - fontSize * 1.15"
            :width="labelW"
            :height="fontSize * 1.5"
            :rx="fontSize * 0.25"
          />
          <text class="box-label" :x="box[0] + fontSize * 0.4" :y="labelY" :font-size="fontSize">
            {{ box[2] }} × {{ box[3] }}
          </text>
          <rect
            v-for="i in 4"
            :key="`h${i}`"
            class="handle"
            :x="(i === 1 || i === 2 ? box[0] + box[2] : box[0]) - handle / 2"
            :y="(i === 2 || i === 3 ? box[1] + box[3] : box[1]) - handle / 2"
            :width="handle"
            :height="handle"
            :data-role="'corner'"
            :data-corner="i - 1"
          />
        </g>
      </svg>

      <div v-if="!box && !mediaError" class="hint micro" aria-hidden="true">在画面上拖一个框，圈住水印</div>
    </div>

    <!-- 时间轴：换一帧看清水印 -->
    <div v-if="imageUrls.length" class="timeline">
      <span class="micro muted">关键帧</span>
      <button v-for="(_, i) in imageUrls" :key="i" type="button" class="btn btn-secondary btn-sm"
        :aria-pressed="selectedFrame === i" @click="selectedFrame = i; mediaError = false">
        {{ i === 0 ? "前段" : i === 1 ? "中段" : "后段" }}
      </button>
    </div>
    <div v-else class="timeline">
      <label class="micro muted" for="seek">画面位置</label>
      <input
        id="seek"
        v-model.number="seek"
        class="range"
        type="range"
        min="0"
        :max="videoDuration || duration"
        step="0.05"
        :disabled="!mediaReady"
        @input="onSeek"
      />
      <span class="micro num">{{ timeText }}</span>
      <span class="micro muted">拖动可换帧，片头黑场时往后拉一点</span>
    </div>

    <div class="toolbar">
      <div class="row" style="gap: 8px; flex-wrap: wrap">
        <button
          v-for="(c, i) in allCandidates"
          :key="`b${i}`"
          class="btn btn-secondary btn-sm"
          type="button"
          :class="{ active: box && box[0] === c.x && box[1] === c.y && box[2] === c.w && box[3] === c.h }"
          @click="applyCandidate(c)"
        >
          候选 {{ i + 1 }}
          <span v-if="c.std_ratio" class="micro muted num">· {{ c.std_ratio }}</span>
        </button>
        <button class="btn btn-ghost btn-sm" type="button" :disabled="!box" @click="clearBox">重新框选</button>
      </div>

      <!-- 精确微调 -->
      <div class="fields">
        <label v-for="(lab, i) in ['X', 'Y', '宽', '高']" :key="lab" class="field-wrap">
          <span class="micro muted">{{ lab }}</span>
          <input
            class="input num"
            type="number"
            min="0"
            :value="box ? box[i] : ''"
            :disabled="!box"
            @change="setField(i, ($event.target as HTMLInputElement).value)"
          />
        </label>
        <span class="micro muted dims">原片 {{ W }} × {{ H }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.selector {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}

.stage {
  position: relative;
  width: 100%;
  max-height: 62vh;
  margin: 0 auto;
  overflow: hidden;
  border-radius: var(--r-ctl);
  background: #101512;
  border: 1px solid var(--line);
  touch-action: none;
  cursor: crosshair;
  user-select: none;
}

.media {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: fill;
  display: block;
  pointer-events: none;
}

/* 覆盖层默认不吃指针：空白拖拽由 stage 统一处理，
   否则降级占位里的「重试预览」按钮会被整层 SVG 挡住点不到。 */
.overlay {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.cand,
.chosen,
.handle {
  pointer-events: auto;
}

.dim {
  fill: rgba(6, 12, 10, 0.5);
  pointer-events: none;
}

.cand {
  fill: rgba(220, 38, 38, 0.06);
  stroke: #ff5555;
  stroke-dasharray: 10 7;
  cursor: pointer;
}

.label-bg {
  fill: rgba(6, 12, 10, 0.84);
  pointer-events: none;
}

.cand-label {
  fill: #d9f6e9;
  pointer-events: none;
  font-family: var(--font-mono);
}

.chosen {
  fill: rgba(220, 38, 38, 0.08);
  stroke: #ff3636;
  cursor: move;
}

.box-label {
  fill: #ffffff;
  pointer-events: none;
  font-family: var(--font-mono);
}

.handle {
  fill: #ffffff;
  stroke: var(--brand);
  stroke-width: 2;
  cursor: nwse-resize;
}

/* 预览不可用时底图是浅色斜纹，白框线会看不见，整套换成深墨绿 */
.light-bg .chosen,
.light-bg .cand {
  stroke: var(--brand);
}

.light-bg .handle {
  fill: var(--brand);
  stroke: #ffffff;
}

.hint {
  position: absolute;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  padding: 6px 12px;
  border-radius: var(--r-pill);
  color: #fff;
  background: rgba(6, 12, 10, 0.72);
  pointer-events: none;
  white-space: nowrap;
}

.fallback {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-align: center;
  padding: 20px;
  color: var(--ink-900);
  background: repeating-linear-gradient(
    45deg,
    var(--surface-sunken),
    var(--surface-sunken) 12px,
    var(--paper) 12px,
    var(--paper) 24px
  );
}

.fb-icon {
  width: 36px;
  height: 36px;
  color: var(--ink-300);
}

.timeline {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.range {
  flex: 1 1 180px;
  min-width: 120px;
  accent-color: var(--brand);
}

.range:disabled {
  opacity: 0.45;
}

.toolbar {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.btn-sm {
  height: 44px;
  padding: 0 14px;
  font-size: var(--fs-micro);
}

.btn-sm.active {
  border-color: var(--brand);
  color: var(--brand);
  background: var(--brand-wash);
}

.fields {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.field-wrap {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field-wrap .input {
  width: 96px;
  height: 50px;
  padding: 0 10px;
}

.dims {
  margin-left: auto;
  padding-bottom: 8px;
}

@media (max-width: 640px) {
  .dims {
    margin-left: 0;
  }
}
</style>
