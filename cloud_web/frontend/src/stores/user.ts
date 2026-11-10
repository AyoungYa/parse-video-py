import { defineStore } from "pinia";
import { api, type Quota } from "@/api/client";

export const useUserStore = defineStore("user", {
  state: () => ({
    token: localStorage.getItem("token") || "",
    nickname: "",
    phone: "",
    points: null as number | null,
    quota: null as Quota | null,
  }),
  getters: {
    isLoggedIn: (state) => Boolean(state.token),
    /** 积分的通俗说法。刻意不写死「1 分 = 1 条 30 秒」这类换算，方便后续调价 */
    pointsHint: (state) => {
      if (state.points === null) return "";
      if (state.quota?.is_member) return "订阅期内不限条数";
      return `约可处理 ${state.points} 条短视频`;
    },
  },
  actions: {
    setToken(token: string) {
      this.token = token;
      localStorage.setItem("token", token);
    },
    async refresh() {
      if (!this.token) return;
      const { data } = await api.me();
      this.nickname = data.nickname || "";
      this.phone = data.phone || "";
      this.points = data.points;
      this.quota = data.quota ?? null;
    },
    logout() {
      this.token = "";
      this.nickname = "";
      this.phone = "";
      this.points = null;
      this.quota = null;
      localStorage.removeItem("token");
    },
  },
});
