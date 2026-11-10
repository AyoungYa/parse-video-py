<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import UploadPanel from "@/components/UploadPanel.vue";
import TaskChip from "@/components/TaskChip.vue";
import { api, type TaskListItem } from "@/api/client";
import { useUserStore } from "@/stores/user";
import { useSiteStore } from "@/stores/site";

const router = useRouter();
const user = useUserStore();
const site = useSiteStore();
const recent = ref<TaskListItem[]>([]);

const TIPS = [
  "水印压在边角、背景相对平稳的片子，修复后几乎看不出接缝。",
  "半透明柔边比纯白字难处理，但我们会把柔边一起擦掉，不只擦最白的那层。",
  "水印大面积盖在人脸或剧烈运动画面上时，系统会提示转人工，而不是硬交付一个花的成片。",
];

function onSubmitted(taskId: string) {
  router.push({ name: "result", params: { id: taskId } });
}

onMounted(async () => {
  await user.refresh().catch(() => undefined);
  try {
    const { data } = await api.taskList();
    recent.value = data.filter((t) => t.status !== "awaiting_upload").slice(0, 4);
  } catch {
    recent.value = [];
  }
});
</script>

<template>
  <div class="page">
    <section class="hero">
      <h1 class="display">把水印擦干净，<br />然后给你一份验收单</h1>
      <p class="secondary hero-sub">
        上传你自己的视频素材，处理完附残留、纹理、闪烁三项检测。任何一项不达标，本次不计费。
      </p>
    </section>

    <UploadPanel :quota="user.quota" @submitted="onSubmitted" />

    <section v-if="recent.length" class="block">
      <div class="row-between" style="margin-bottom: 12px">
        <span class="eyebrow">最近任务</span>
        <router-link to="/points" class="micro">积分与记录</router-link>
      </div>
      <div class="grid">
        <TaskChip v-for="t in recent" :key="t.id" :task="t" />
      </div>
    </section>

    <section v-else class="block tips">
      <span class="eyebrow">什么样的片子效果最好</span>
      <ul class="tip-list">
        <li v-for="(tip, i) in TIPS" :key="i">
          <span class="idx num">{{ String(i + 1).padStart(2, "0") }}</span>
          <span class="small secondary">{{ tip }}</span>
        </li>
      </ul>
    </section>

    <p class="micro muted legal">
      本站仅处理你拥有版权或已获授权的素材，不提供任何平台视频解析或下载功能。素材在任务结束
      {{ site.retentionText }}后自动删除。
    </p>
  </div>
</template>

<style scoped>
.hero {
  padding: var(--sp-5) 0 var(--sp-6);
}

.hero-sub {
  margin-top: 12px;
  max-width: 520px;
  font-size: var(--fs-body);
  line-height: 1.7;
}

.block {
  margin-top: var(--sp-6);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}

.tips {
  padding-top: var(--sp-2);
}

.tip-list {
  list-style: none;
  margin: 14px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.tip-list li {
  display: flex;
  gap: 12px;
  align-items: baseline;
}

.idx {
  font-size: var(--fs-caption);
  color: var(--ink-300);
  flex-shrink: 0;
  letter-spacing: 0.04em;
}

.legal {
  margin-top: var(--sp-7);
  line-height: 1.7;
  max-width: 640px;
}

@media (max-width: 640px) {
  .hero {
    padding: var(--sp-4) 0 var(--sp-5);
  }

  .grid {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
