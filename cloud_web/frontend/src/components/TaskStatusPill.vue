<script setup lang="ts">
import { computed } from "vue";
import type { TaskStatus } from "@/api/client";

const props = defineProps<{ status: TaskStatus | string; progress?: number }>();

const MAP: Record<string, { text: string; tone: string; live?: boolean }> = {
  awaiting_upload: { text: "等待上传", tone: "muted" },
  pending: { text: "排队中", tone: "muted" },
  leased: { text: "已分配算力", tone: "brand", live: true },
  running: { text: "处理中", tone: "brand", live: true },
  verifying: { text: "验收中", tone: "brand", live: true },
  needs_manual: { text: "待你框选", tone: "warn" },
  succeeded: { text: "已完成", tone: "ok" },
  failed: { text: "处理失败", tone: "bad" },
  cancelled: { text: "已取消", tone: "muted" },
  expired: { text: "已过期", tone: "muted" },
};

const info = computed(() => MAP[props.status] ?? { text: props.status, tone: "muted" });

const label = computed(() => {
  const base = info.value.text;
  // 处理中把百分比并进徽标，省一个视觉元素
  if (props.status === "running" && typeof props.progress === "number") {
    return `${base} ${Math.round(props.progress * 100)}%`;
  }
  return base;
});
</script>

<template>
  <span class="pill" :class="[`t-${info.tone}`]">
    <i v-if="info.live" class="dot" aria-hidden="true" />
    {{ label }}
  </span>
</template>

<style scoped>
.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 22px;
  padding: 0 8px;
  border-radius: var(--r-tag);
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
}

.t-muted {
  background: var(--surface-sunken);
  color: var(--ink-600);
}

.t-brand {
  background: var(--brand-wash);
  color: var(--brand);
}

.t-ok {
  background: var(--ok-wash);
  color: var(--ok);
}

.t-bad {
  background: var(--bad-wash);
  color: var(--bad);
}

.t-warn {
  background: var(--warn-wash);
  color: var(--warn);
}

.dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  animation: breathe 1.8s ease-in-out infinite;
}

@keyframes breathe {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.25;
  }
}
</style>
