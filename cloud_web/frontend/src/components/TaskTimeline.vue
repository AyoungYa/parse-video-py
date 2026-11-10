<script setup lang="ts">
import { computed } from "vue";
import type { TaskStatus } from "@/api/client";

const props = defineProps<{
  status: TaskStatus;
  progress: number;
  confirmed?: boolean;
}>();

const phases = ["上传视频", "排队等待", "获取关键帧", "定位水印", "确认水印", "去水印", "质量验收"] as const;
const active = computed(() => {
  const s = props.status;
  if (s === "awaiting_upload") return 0;
  if (s === "pending" || s === "leased") return props.confirmed ? 5 : 1;
  if (s === "extracting_frames") return 2;
  if (s === "locating_watermark") return 3;
  if (s === "awaiting_confirmation" || s === "needs_manual") return 4;
  if (s === "removing_watermark" || s === "running") return 5;
  return 6;
});
const complete = computed(() => props.status === "succeeded");
function state(i: number) {
  if (i < active.value || (complete.value && i === 6)) return "done";
  return i === active.value ? "active" : "todo";
}
</script>

<template>
  <nav class="timeline" aria-label="视频处理进度">
    <ol class="track">
      <li v-for="(name, i) in phases" :key="name" class="phase" :class="state(i)" :aria-current="state(i) === 'active' ? 'step' : undefined">
        <span class="marker"><span v-if="state(i) === 'done'">✓</span><span v-else>{{ String(i + 1).padStart(2, "0") }}</span></span>
        <span class="label">{{ name }}</span>
      </li>
    </ol>
    <div class="phase-caption">
      <span>{{ confirmed && (status === 'pending' || status === 'leased') ? '等待去水印' : phases[active] }}</span>
      <span v-if="active === 5 && status !== 'pending' && status !== 'leased'" class="num">{{ Math.round(progress * 100) }}%</span>
      <span v-else-if="complete">已完成</span>
      <span v-else>第 {{ active + 1 }} / {{ phases.length }} 步</span>
    </div>
  </nav>
</template>

<style scoped>
.timeline { overflow: hidden; }
.track { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); margin: 0; padding: 0 0 22px; list-style: none; }
.phase { position: relative; display: flex; flex-direction: column; align-items: center; gap: 10px; min-width: 0; text-align: center; }
.phase:not(:last-child)::after { content: ""; position: absolute; top: 17px; left: calc(50% + 22px); right: calc(-50% + 22px); height: 2px; background: var(--line); }
.phase.done::after { background: var(--brand); }
.marker { position: relative; z-index: 1; display: grid; place-items: center; width: 36px; height: 36px; border: 1.5px solid var(--line-strong); border-radius: 50%; color: var(--ink-400); background: var(--surface); font-size: 11px; font-weight: 700; }
.done .marker { background: var(--brand); color: white; border-color: var(--brand); font-size: 16px; }
.active .marker { color: var(--brand); background: var(--brand-wash); border: 2px solid var(--brand); box-shadow: 0 0 0 5px color-mix(in srgb, var(--brand) 10%, transparent); }
.label { color: var(--ink-400); font-size: 12px; line-height: 1.35; white-space: nowrap; }
.done .label { color: var(--ink-700); }
.active .label { color: var(--brand); font-weight: 700; }
.phase-caption { display: flex; justify-content: space-between; padding: 13px 4px 0; border-top: 1px solid var(--line-soft); color: var(--ink-600); font-size: 12px; }
.phase-caption span:first-child { color: var(--brand); font-weight: 700; }
@media (max-width: 680px) {
  .timeline { overflow-x: auto; }
  .track { min-width: 630px; }
  .label { font-size: 11px; }
}
</style>
