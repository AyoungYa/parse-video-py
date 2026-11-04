<script setup lang="ts">
import AppHeader from "@/components/AppHeader.vue";
</script>

<template>
  <AppHeader />
  <router-view v-slot="{ Component }">
    <transition name="fade" mode="out-in">
      <component :is="Component" />
    </transition>
  </router-view>
</template>
