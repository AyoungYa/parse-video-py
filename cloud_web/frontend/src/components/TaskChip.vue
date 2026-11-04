<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";
import TaskStatusPill from "@/components/TaskStatusPill.vue";
import type { TaskListItem } from "@/api/client";

const props = defineProps<{ task: TaskListItem }>();
const router = useRouter();

const title = computed(() => {
  const d = props.task.duration_seconds;
  return d ? `${d.toFixed(1)} 秒视频` : "视频任务";
});

const meta = computed(() => {
  const w = props.task.width;
  const h = props.task.height;
  if (!w || !h) return "";
  return `${h > w ? "竖屏" : "横屏"} ${w}×${h}`;
});

function open() {
  router.push({ name: "result", params: { id: props.task.id } });
}
</script>

<template>
  <button class="chip" type="button" @click="open">
    <div class="chip-top">
      <TaskStatusPill :status="task.status" :progress="task.progress" />
      <span class="micro muted num">{{ meta }}</span>
    </div>
    <div class="chip-title">{{ title }}</div>
    <div v-if="task.status === 'running'" class="bar" style="margin-top: 9px">
      <i :style="{ width: `${Math.max(3, task.progress * 100)}%` }" />
    </div>
    <div v-else class="chip-foot micro muted num">{{ task.finished_at ? "已交付" : "处理中" }}</div>
  </button>
</template>

<style scoped>
.chip {
  display: block;
  width: 100%;
  text-align: left;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-ctl);
  padding: 13px;
  cursor: pointer;
  font-family: inherit;
  transition: border-color 0.16s, box-shadow 0.16s;
}

.chip:hover {
  border-color: var(--line-strong);
  box-shadow: var(--shadow-card);
}

.chip-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 9px;
}

.chip-title {
  font-size: 13px;
  color: var(--ink-900);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chip-foot {
  margin-top: 6px;
}
</style>
