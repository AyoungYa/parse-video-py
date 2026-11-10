<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  metrics: Record<string, number | boolean>;
  allGreen: boolean;
}>();

interface MetricDef {
  key: string;
  label: string;
  rule: string;
  pass: (v: number) => boolean;
  verdict: (v: number) => string;
}

const DEFS: MetricDef[] = [
  {
    key: "residual_max",
    label: "残留",
    rule: "及格 < 3.0",
    pass: (v) => v < 3,
    verdict: (v) => (v < 0.5 ? "几乎为零" : v < 3 ? "肉眼不可见" : "可能有残留"),
  },
  {
    key: "gradient_p50_ratio",
    label: "纹理自然度",
    rule: "及格 > 0.85",
    pass: (v) => v > 0.85,
    verdict: (v) => (v < 0.85 ? "略偏平滑" : v > 1.25 ? "纹理偏强" : "与周边一致"),
  },
  {
    key: "flicker_p90",
    label: "帧间闪烁",
    rule: "及格 < 1.5",
    pass: (v) => v < 1.5,
    verdict: (v) => (v < 0.5 ? "无闪烁" : v < 1.5 ? "极轻微" : "有闪烁"),
  },
];

const rows = computed(() =>
  DEFS.map((d) => {
    const raw = props.metrics?.[d.key];
    const value = typeof raw === "number" ? raw : null;
    return {
      ...d,
      value,
      ok: value === null ? null : d.pass(value),
      text: value === null ? "未测" : value.toFixed(2),
      verdictText: value === null ? "本次未采集到" : d.verdict(value),
    };
  })
);

const riskCount = computed(() => rows.value.filter((r) => r.ok === false).length);
</script>

<template>
  <div class="report">
    <div class="row-between" style="margin-bottom: 4px">
      <span class="h2">验收报告</span>
      <span class="micro muted">判据同 verify.py 三件套</span>
    </div>

    <ul class="list">
      <li v-for="r in rows" :key="r.key" class="item">
        <i class="dot" :class="r.ok === null ? 'is-muted' : r.ok ? 'is-ok' : 'is-bad'" aria-hidden="true" />
        <span class="label">{{ r.label }}</span>
        <span class="num value muted">{{ r.text }}　{{ r.rule }}</span>
        <span class="verdict" :class="r.ok === null ? 'is-muted' : r.ok ? 'is-ok' : 'is-bad'">
          {{ r.verdictText }}
        </span>
      </li>
    </ul>

    <div v-if="riskCount > 0" class="notice notice-warn" style="margin-top: 14px">
      有 {{ riskCount }} 项没到及格线。这部分可能有轻微痕迹，建议拖动对比、放大确认一下。
      本次不计费，可在 24 小时内免费重跑一次。
    </div>

    <div class="promise">
      <template v-if="allGreen">
        三项全部通过。如果肉眼仍看出痕迹，把片子发给我们，人工复核后全额退分。
      </template>
      <template v-else>任何一项不达标，本次不计费。</template>
    </div>
  </div>
</template>

<style scoped>
.report {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-card);
  padding: var(--sp-5);
}

.list {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
}

.item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 0;
  border-bottom: 1px solid var(--line-soft);
}

.item:last-child {
  border-bottom: 0;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot.is-ok {
  background: var(--ok);
}

.dot.is-bad {
  background: var(--bad);
}

.dot.is-muted {
  background: var(--ink-300);
}

.label {
  font-size: var(--fs-small);
  color: var(--ink-900);
  width: 84px;
  flex-shrink: 0;
}

.value {
  font-size: var(--fs-micro);
  flex: 1;
  min-width: 0;
}

.verdict {
  font-size: var(--fs-micro);
  flex-shrink: 0;
}

.verdict.is-ok {
  color: var(--ok);
}

.verdict.is-bad {
  color: var(--bad);
}

.verdict.is-muted {
  color: var(--ink-400);
}

.promise {
  margin-top: 14px;
  padding: 10px 12px;
  background: var(--surface-sunken);
  border-radius: 8px;
  font-size: var(--fs-micro);
  line-height: 1.6;
  color: var(--ink-600);
}

@media (max-width: 640px) {
  .item {
    flex-wrap: wrap;
    gap: 6px 10px;
  }

  .label {
    width: auto;
  }

  .value {
    flex: 1 0 auto;
    order: 3;
    width: 100%;
    padding-left: 16px;
  }
}
</style>
