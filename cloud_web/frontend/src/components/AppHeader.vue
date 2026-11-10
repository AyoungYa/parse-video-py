<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useUserStore } from "@/stores/user";
import BrandLogo from "@/components/BrandLogo.vue";

const route = useRoute();
const router = useRouter();
const user = useUserStore();

// 登录页是完整的左右分栏，不需要顶栏抢空间
const show = computed(() => route.name !== "login");

function logout() {
  user.logout();
  router.push({ name: "login" });
}
</script>

<template>
  <header v-if="show" class="topbar">
    <div class="topbar-inner">
      <router-link to="/" class="brand" aria-label="无痕匠首页">
        <BrandLogo :size="28" />
        <span class="brand-name">无痕匠</span>
      </router-link>

      <div class="topbar-right">
        <router-link to="/points" class="points-pill num" :title="`积分 ${user.points ?? 0}`">
          <svg viewBox="0 0 12 12" class="coin" aria-hidden="true">
            <circle cx="6" cy="6" r="4.3" fill="none" stroke="currentColor" stroke-width="1.2" />
            <circle cx="6" cy="6" r="1.3" fill="currentColor" />
          </svg>
          积分 {{ user.points ?? "—" }}
        </router-link>
        <router-link to="/points" class="who">
          {{ user.nickname || "我的" }}
          <span
            v-if="user.quota?.is_member"
            class="member-badge"
            title="会员生效中 · 订阅期内不限条数"
          >
            <svg viewBox="0 0 14 14" class="crown" aria-hidden="true">
              <path
                d="M2.1 4.4 4.5 6.5 7 2.9l2.5 3.6 2.4-2.1-1 5.8c-.1.5-.5.8-1 .8H3.1c-.5 0-.9-.3-1-.8z"
                fill="currentColor"
              />
            </svg>
            会员
          </span>
        </router-link>
        <button class="link-btn" type="button" @click="logout">退出</button>
      </div>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  background: rgba(255, 255, 255, 0.9);
  backdrop-filter: saturate(180%) blur(8px);
  border-bottom: 1px solid var(--line);
}

.topbar-inner {
  max-width: var(--page-w);
  margin: 0 auto;
  height: 72px;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--ink-900);
}

.brand:hover {
  color: var(--ink-900);
}

.brand-name {
  font-size: var(--fs-md);
  font-weight: 500;
  letter-spacing: 0.04em;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  font-size: var(--fs-small);
}

.points-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 38px;
  padding: 0 12px;
  border-radius: var(--r-pill);
  background: var(--brand-wash);
  color: var(--brand);
  font-size: var(--fs-micro);
}

.points-pill:hover {
  background: #dfeee9;
  color: var(--brand);
}

.coin {
  width: 14px;
  height: 14px;
}

.who {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--ink-600);
  font-size: var(--fs-small);
}

.who:hover {
  color: var(--ink-900);
}

/* 会员徽章：古铜金渐变 + 顶部高光，像一枚小铜牌 */
.member-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 28px;
  padding: 0 10px 0 8px;
  border-radius: var(--r-pill);
  background: linear-gradient(160deg, var(--gold-lite), var(--gold) 52%, var(--gold-deep));
  box-shadow: inset 0 1px 0 var(--gold-sheen), 0 1px 3px var(--gold-ring);
  color: var(--surface);
  font-size: var(--fs-caption);
  font-weight: 600;
  letter-spacing: 0.08em;
  line-height: 1;
  transition: filter 0.15s ease;
}

.member-badge:hover {
  filter: brightness(1.08);
}

.crown {
  width: 13px;
  height: 13px;
  flex: none;
}

.link-btn {
  color: var(--ink-400);
  font-size: var(--fs-small);
}

.link-btn:hover {
  color: var(--ink-600);
  text-decoration: none;
}

@media (max-width: 640px) {
  .who {
    display: none;
  }

  .topbar-inner {
    padding: 0 16px;
  }
}
</style>
