<script setup lang="ts">
/**
 * 全站页脚。
 *
 * ★ 备案号展示位（工信部硬要求）：ICP 备案通过后，把下方 ICP 号替换成
 *   真实的「京ICP备XXXXXXXX号」，并保留指向 beian.miit.gov.cn 的链接。
 *   备案未下来之前保持占位，**上线前必须替换**（否则会被要求整改）。
 */
const ICP_NUMBER = "京ICP备00000000号";
const HAS_ICP = false; // 备案通过后改成 true
</script>

<template>
  <footer class="foot">
    <div class="foot-inner">
      <span class="copy">无痕匠 · 视频去水印</span>

      <nav class="links" aria-label="页脚导航">
        <router-link to="/about">关于与备案信息</router-link>
        <!-- 备案通过后补上「备案信息」页面的链接 -->
        <a
          v-if="HAS_ICP"
          href="https://beian.miit.gov.cn/"
          target="_blank"
          rel="noopener noreferrer"
          >{{ ICP_NUMBER }}</a
        >
        <span v-else class="pending" title="ICP 备案办理中">ICP 备案办理中</span>
      </nav>
    </div>
  </footer>
</template>

<style scoped>
.foot {
  margin-top: auto;
  border-top: 1px solid var(--line);
  background: var(--paper);
}

.foot-inner {
  max-width: var(--page-w);
  margin: 0 auto;
  padding: 18px 20px 26px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  font-size: 12px;
  color: var(--ink-400);
}

.copy {
  color: var(--ink-600);
}

.links {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
}

.links a {
  color: var(--ink-400);
  text-decoration: none;
}

.links a:hover {
  color: var(--brand);
}

/* 页脚里的 router-link（关于与备案信息） */
.links :deep(a) {
  color: var(--ink-400);
}

.links :deep(a:hover) {
  color: var(--brand);
}

.pending {
  color: var(--ink-400);
  opacity: 0.75;
}

@media (max-width: 640px) {
  .foot-inner {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    padding: 16px 16px 22px;
  }
}
</style>
