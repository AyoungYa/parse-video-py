<script lang="ts">
// 模块级计数器：保证每个实例的 mask id 唯一（同页多个 logo 不会互相串）
let seq = 0;
</script>

<script setup lang="ts">
/**
 * 品牌「帧 · 擦」标。
 *
 * 设计：圆角方块（延续印章 DNA，源自视频画幅）+ 反白播放键（视频处理）
 *   + 右上角弧形擦口（水印最常见的角落被干干净净擦掉）
 *   + 四角星芒（擦净焕新）。
 * 三个内形全部用 mask 挖空而非第二色，任何背景色都成立：
 * 浅底传默认（块底 var(--brand)），深底传 inverse（块底纸白）。
 */
withDefaults(defineProps<{ size?: number; inverse?: boolean }>(), {
  size: 22,
  inverse: false,
});

const uid = `wj-mark-${++seq}`;
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 64 64" aria-hidden="true" :class="{ inverse }">
    <defs>
      <mask :id="uid">
        <rect x="4" y="4" width="56" height="56" rx="13" fill="#fff" />
        <!-- 播放键（圆角三角） -->
        <path
          d="M21.5 23 L37.5 32 L21.5 41 Z"
          fill="#000"
          stroke="#000"
          stroke-width="3"
          stroke-linejoin="round"
        />
        <!-- 右上角弧形擦口：水印角被擦掉 -->
        <path d="M60 4 L46 4 A14 14 0 0 0 60 18 Z" fill="#000" />
        <!-- 四角星芒：擦净焕新 -->
        <path
          d="M49 18.5 L50.7 24.3 L56.5 26 L50.7 27.7 L49 33.5 L47.3 27.7 L41.5 26 L47.3 24.3 Z"
          fill="#000"
        />
      </mask>
    </defs>
    <rect x="4" y="4" width="56" height="56" rx="13" class="seal" :mask="`url(#${uid})`" />
  </svg>
</template>

<style scoped>
svg {
  display: block;
  flex-shrink: 0;
}

.seal {
  fill: var(--brand);
}

svg.inverse .seal {
  fill: #faf9f5;
}
</style>
