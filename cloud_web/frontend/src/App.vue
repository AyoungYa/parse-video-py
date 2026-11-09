<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import AppFooter from "@/components/AppFooter.vue";
import AppHeader from "@/components/AppHeader.vue";

const route = useRoute();
// 登录页是左右分栏布局，通栏页脚会把分栏割裂 —— 改由页内自曝备案号
const showFooter = computed(() => route.name !== "login");
</script>

<template>
  <div class="app-shell">
    <AppHeader />
    <main class="app-main">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>
    <!-- 页脚含 ICP 备案号展示位（工信部要求，上线前必须替换成真实备案号） -->
    <AppFooter v-if="showFooter" />
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.app-main {
  flex: 1;
}
</style>
