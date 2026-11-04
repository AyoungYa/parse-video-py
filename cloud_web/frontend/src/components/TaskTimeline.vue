<script setup lang="ts">
import { computed } from "vue";
import type { TaskStatus } from "@/api/client";

const props = defineProps<{
  status: TaskStatus;
  progress: number;
  remainingSeconds?: number | null;
  queueAhead?: number | null;
  workerOffline?: boolean;
}>();

const PHASES = ["排队等待", "定位水印", "修复画面", "质量验收"] as const;

/** 把后端的单一 progress 映射到四个阶段：用户需要知道「现在在干什么」，不是「几成」。 */
const activeIndex = computed(() => {
  const st = props.status;
  if (st === "awaiting_upload" || st === "pending" || st === "leased") return 0;
  if (st === "verifying") return 3;
  if (st === "running") {
    const p = props.progress;
    if (p < 0.12) return 1;
    if (p < 0.92) return 2;
    return 3;
  }
  return 4; // 终态：全部完成
});

function stateOf(i: number): "done" | "active" | "todo" {
  if (i < activeIndex.value) return "done";
  if (i === activeIndex.value) return "active";
  return "todo";
}

const detailOf = computed<(i: number) => string>(() => (i: number) => {
  const st = props.status;
  if (i === 0) {
    if (st === "pending") {
      return props.queueAhead && props.queueAhead > 0 ? `前面还有 ${props.queueAhead} 条` : "等待可用算力节点";
    }
    if (st === "leased") return "已分配到算力机";
    return "已进入队列";
  }
  if (i === 1) {
    if (activeIndex.value > 1) return "已确认水印区域";
    if (activeIndex.value === 1) return "正在分析画面，找出恒定的水印区域";
    return "待开始";
  }
  if (i === 2) {
    if (activeIndex.value > 2) return "已完成逐帧修复";
    if (activeIndex.value === 2) {
      const left =
        props.remainingSeconds && props.remainingSeconds > 0
          ? `　·　约剩 ${Math.ceil(props.remainingSeconds)} 秒`
          : "";
      return `${Math.round(props.progress * 100)}%${left}`;
    }
    return "待开始";
  }
  if (activeIndex.value > 3) return "三项指标已生成";
  if (activeIndex.value === 3) return "正在比对残留、纹理、闪烁";
  return "待开始";
});

const showBar = computed(() => activeIndex.value === 2 && props.status === "running");
</script>

<template>
  <div class="timeline">
    <div v-for="(name, i) in PHASES" :key="name" class="phase" :class="stateOf(i)">
      <div class="rail">
        <span class="node">
          <svg v-if="stateOf(i) === 'done'" viewBox="0 0 10 10" aria-hidden="true">
            <path d="M1.9 5.1l2.1 2.1 4.1-4.3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </span>
        <i v-if="i < PHASES.length - 1" class="line" />
      </div>

      <div class="body">
        <div class="row-between">
          <span class="name">{{ name }}</span>
          <span v-if="stateOf(i) === 'active' && showBar" class="num micro pct">
            {{ Math.round(progress * 100) }}%
          </span>
        </div>
        <div class="micro detail">{{ detailOf(i) }}</div>
        <div v-if="stateOf(i) === 'active' && showBar" class="bar" style="margin-top: 9px">
          <i :style="{ width: `${Math.max(3, progress * 100)}%` }" />
        </div>
        <div v-if="i === 0 && workerOffline && status === 'pending'" class="notice notice-warn" style="margin-top: 10px">
          处理节点暂时离线，任务已自动排队。节点上线后自动开始，<strong>不需要重新上传</strong>。
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
}

.phase {
  display: flex;
  gap: 14px;
}

.rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 18px;
  flex-shrink: 0;
}

.node {
  width: 15px;
  height: 15px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1.5px solid var(--line-strong);
  background: var(--surface);
  color: #fff;
  transition: background-color 0.2s, border-color 0.2s;
}

.node svg {
  width: 9px;
  height: 9px;
}

.line {
  width: 1.5px;
  flex: 1;
  min-height: 18px;
  background: var(--line);
  margin: 4px 0;
}

.body {
  flex: 1;
  min-width: 0;
  padding-bottom: 20px;
}

.phase:last-child .body {
  padding-bottom: 0;
}

.name {
  font-size: 13px;
  font-weight: 500;
  color: var(--ink-900);
}

.detail {
  color: var(--ink-400);
  margin-top: 2px;
}

.pct {
  color: var(--brand);
}

/* 已完成 */
.done .node {
  background: var(--brand);
  border-color: var(--brand);
}

.done .line {
  background: #cfe0d9;
}

/* 进行中 */
.active .node {
  width: 17px;
  height: 17px;
  border: 2px solid var(--brand);
  background: var(--brand-wash);
  margin: -1px;
}

.active .name {
  color: var(--brand);
}

.active .detail {
  color: var(--ink-600);
}

.active .line {
  background: var(--line);
}

/* 待开始 */
.todo .name {
  color: var(--ink-400);
  font-weight: 400;
}
</style>
