import { defineStore } from "pinia";
import { api } from "@/api/client";

/**
 * 站点公开配置。素材保留天数等运营参数跟着后端 OSS_RETENTION_DAYS 走，
 * 改配置不用重发前端。默认值与后端默认一致（3 天），接口失败时静默用默认。
 */
export const useSiteStore = defineStore("site", {
  state: () => ({
    retentionDays: 3,
  }),
  getters: {
    /** 文案用：如 "3 天"。刻意不换算成小时，保持与配置单位一致。 */
    retentionText: (state) => `${state.retentionDays} 天`,
  },
  actions: {
    async refresh() {
      try {
        const { data } = await api.siteConfig();
        if (data?.retention_days && data.retention_days > 0) {
          this.retentionDays = data.retention_days;
        }
      } catch {
        // 拉不到就用默认值，不打扰用户
      }
    },
  },
});
