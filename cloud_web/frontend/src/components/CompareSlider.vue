<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps<{
  before: string;
  after: string;
  /** 宽 / 高，用来撑住版面，避免加载时跳动 */
  aspect?: number;
}>();

const wrapRef = ref<HTMLElement | null>(null);
const videoA = ref<HTMLVideoElement | null>(null);
const videoB = ref<HTMLVideoElement | null>(null);

const pos = ref(50);
const dragging = ref(false);
const mode = ref<"compare" | "after">("compare");
const expanded = ref(false);
const paused = ref(false);
const ready = ref(false);

const ratio = computed(() => props.aspect && props.aspect > 0 ? props.aspect : 16 / 9);

function update(e: PointerEvent) {
  const el = wrapRef.value;
  if (!el) return;
  const rect = el.getBoundingClientRect();
  const next = ((e.clientX - rect.left) / rect.width) * 100;
  pos.value = Math.min(100, Math.max(0, next));
}

function onDown(e: PointerEvent) {
  if (mode.value !== "compare") return;
  dragging.value = true;
  (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  update(e);
}

function onMove(e: PointerEvent) {
  if (!dragging.value) return;
  update(e);
}

function onUp() {
  dragging.value = false;
}

function nudge(step: number) {
  pos.value = Math.min(100, Math.max(0, pos.value + step));
}

/** 两个播放器各跑各的必然会漂移，用「成片」当主时钟，原片超过 150ms 就纠一次。 */
function syncClock() {
  const a = videoA.value;
  const b = videoB.value;
  if (!a || !b || b.paused) return;
  if (Math.abs(a.currentTime - b.currentTime) > 0.15) {
    a.currentTime = b.currentTime;
  }
}

let rafId = 0;
function loop() {
  syncClock();
  rafId = window.requestAnimationFrame(loop);
}

async function playAll() {
  await nextTick();
  for (const v of [videoA.value, videoB.value]) {
    if (!v) continue;
    v.muted = true;
    try {
      await v.play();
    } catch {
      /* 浏览器可能拦截自动播放，用户点一下就好 */
    }
  }
  ready.value = true;
}

function togglePlay() {
  paused.value = !paused.value;
  for (const v of [videoA.value, videoB.value]) {
    if (!v) return;
    if (paused.value) v.pause();
    else void v.play();
  }
}

function switchMode(next: "compare" | "after") {
  mode.value = next;
  if (next === "compare") void playAll();
}

onMounted(() => {
  void playAll();
  rafId = window.requestAnimationFrame(loop);
});

onBeforeUnmount(() => {
  window.cancelAnimationFrame(rafId);
});

watch(expanded, (open) => {
  document.body.style.overflow = open ? "hidden" : "";
  if (open) void playAll();
});

onBeforeUnmount(() => {
  document.body.style.overflow = "";
});

function onKey(e: KeyboardEvent) {
  if (e.key === "ArrowLeft") {
    e.preventDefault();
    nudge(-2);
  } else if (e.key === "ArrowRight") {
    e.preventDefault();
    nudge(2);
  }
}
</script>

<template>
  <div class="compare">
    <div
      ref="wrapRef"
      class="stage"
      :class="{ dragging, single: mode === 'after' }"
      :style="{ aspectRatio: String(ratio) }"
      @pointerdown="onDown"
      @pointermove="onMove"
      @pointerup="onUp"
      @pointercancel="onUp"
    >
      <!-- 成片（底层） -->
      <video
        ref="videoB"
        class="layer"
        :src="after"
        muted
        loop
        playsinline
        preload="auto"
        :controls="mode === 'after'"
      />

      <!-- 原片（上层，按拖动位置裁切） -->
      <video
        v-if="mode === 'compare'"
        ref="videoA"
        class="layer"
        :src="before"
        muted
        loop
        playsinline
        preload="auto"
        :style="{ clipPath: `inset(0 ${100 - pos}% 0 0)` }"
      />

      <template v-if="mode === 'compare'">
        <div class="vlabel left">原片</div>
        <div class="vlabel right">成片</div>

        <div class="divider-line" :style="{ left: `${pos}%` }" />
        <div
          class="handle"
          :style="{ left: `${pos}%` }"
          role="slider"
          tabindex="0"
          aria-label="拖动对比位置"
          :aria-valuenow="Math.round(pos)"
          aria-valuemin="0"
          aria-valuemax="100"
          @keydown="onKey"
        >
          <svg viewBox="0 0 14 14" aria-hidden="true">
            <path d="M5 3.6L2.6 7 5 10.4M9 3.6L11.4 7 9 10.4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </div>
        <div class="hint-corner">拖动对比</div>
      </template>

      <div v-if="!ready" class="loading">加载预览…</div>
    </div>

    <div class="toolbar">
      <div class="seg">
        <button type="button" :class="{ on: mode === 'compare' }" @click="switchMode('compare')">对比</button>
        <button type="button" :class="{ on: mode === 'after' }" @click="switchMode('after')">只看成片</button>
      </div>

      <div class="row" style="gap: 8px">
        <button v-if="mode === 'compare'" class="tool" type="button" @click="togglePlay">
          <svg v-if="!paused" viewBox="0 0 12 12" aria-hidden="true">
            <rect x="3" y="2.4" width="2.4" height="7.2" rx="0.6" fill="currentColor" />
            <rect x="6.8" y="2.4" width="2.4" height="7.2" rx="0.6" fill="currentColor" />
          </svg>
          <svg v-else viewBox="0 0 12 12" aria-hidden="true">
            <path d="M3.6 2.4l6 3.6-6 3.6z" fill="currentColor" />
          </svg>
          {{ paused ? "播放" : "暂停" }}
        </button>
        <button class="tool" type="button" @click="expanded = true">
          <svg viewBox="0 0 12 12" aria-hidden="true">
            <path d="M4.4 1.8H1.8v2.6M7.6 1.8h2.6v2.6M4.4 10.2H1.8V7.6M7.6 10.2h2.6V7.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          放大
        </button>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="expanded" class="modal" @click.self="expanded = false">
        <div class="modal-body">
          <div class="modal-head">
            <span class="small">拖动中间的白线，左边原片、右边成片</span>
            <button class="modal-close" type="button" @click="expanded = false">关闭</button>
          </div>
          <div
            class="stage"
            :class="{ single: mode === 'after' }"
            :style="{ aspectRatio: String(ratio) }"
            @pointerdown="onDown"
            @pointermove="onMove"
            @pointerup="onUp"
            @pointercancel="onUp"
          >
            <video class="layer" :src="after" muted loop playsinline autoplay />
            <video
              v-if="mode === 'compare'"
              class="layer"
              :src="before"
              muted
              loop
              playsinline
              autoplay
              :style="{ clipPath: `inset(0 ${100 - pos}% 0 0)` }"
            />
            <template v-if="mode === 'compare'">
              <div class="divider-line" :style="{ left: `${pos}%` }" />
              <div class="handle" :style="{ left: `${pos}%` }" aria-hidden="true">
                <svg viewBox="0 0 14 14" aria-hidden="true">
                  <path d="M5 3.6L2.6 7 5 10.4M9 3.6L11.4 7 9 10.4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </div>
            </template>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.stage {
  position: relative;
  width: 100%;
  border-radius: 12px;
  overflow: hidden;
  background: #1c2422;
  cursor: ew-resize;
  touch-action: none;
  user-select: none;
}

.stage.single {
  cursor: default;
}

.layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #1c2422;
  display: block;
}

.vlabel {
  position: absolute;
  bottom: 12px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.9);
  background: rgba(0, 0, 0, 0.42);
  padding: 3px 8px;
  border-radius: 4px;
  pointer-events: none;
}

.vlabel.left {
  left: 12px;
}

.vlabel.right {
  right: 12px;
}

.hint-corner {
  position: absolute;
  top: 12px;
  right: 12px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.72);
  pointer-events: none;
}

.divider-line {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1.5px;
  background: rgba(255, 255, 255, 0.95);
  transform: translateX(-0.75px);
  pointer-events: none;
}

.handle {
  position: absolute;
  top: 50%;
  width: 30px;
  height: 30px;
  margin-left: -15px;
  margin-top: -15px;
  border-radius: 50%;
  background: #fff;
  color: var(--ink-900);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.28);
  cursor: ew-resize;
}

.handle svg {
  width: 14px;
  height: 14px;
}

.handle:focus-visible {
  outline: 2px solid #fff;
  outline-offset: 2px;
}

.loading {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(255, 255, 255, 0.7);
  font-size: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
  gap: var(--sp-3);
}

.seg {
  display: inline-flex;
  padding: 2px;
  background: var(--surface-sunken);
  border-radius: 8px;
  gap: 2px;
}

.seg button {
  height: 26px;
  padding: 0 12px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ink-600);
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}

.seg button.on {
  background: var(--surface);
  color: var(--ink-900);
  box-shadow: 0 1px 2px rgba(18, 26, 23, 0.08);
}

.tool {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 11px;
  border: 1px solid var(--line-strong);
  border-radius: 7px;
  background: var(--surface);
  color: var(--ink-600);
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}

.tool:hover {
  border-color: var(--ink-400);
  color: var(--ink-900);
}

.tool svg {
  width: 11px;
  height: 11px;
}

.modal {
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(10, 14, 13, 0.86);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.modal-body {
  width: min(1100px, 100%);
}

.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  color: rgba(255, 255, 255, 0.82);
}

.modal-close {
  height: 28px;
  padding: 0 12px;
  border-radius: 7px;
  border: 1px solid rgba(255, 255, 255, 0.28);
  background: transparent;
  color: rgba(255, 255, 255, 0.9);
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}
</style>
