import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: () => import("@/views/LoginView.vue") },
    { path: "/", name: "upload", component: () => import("@/views/UploadView.vue") },
    { path: "/tasks/:id", name: "result", component: () => import("@/views/ResultView.vue") },
    // 自动定位失败时的人工框选页（M3.1 兜底）
    {
      path: "/tasks/:id/mask",
      name: "mask-confirm",
      component: () => import("@/views/MaskConfirmView.vue"),
    },
    { path: "/points", name: "points", component: () => import("@/views/PointsView.vue") },
    // 备案信息页：无需登录（工信部要求备案号可公开访问）
    { path: "/about", name: "about", component: () => import("@/views/AboutView.vue") },
  ],
});

router.beforeEach((to) => {
  const token = localStorage.getItem("token");
  // about 公开：备案信息必须能被任何人访问
  if (!token && to.name !== "login" && to.name !== "about") return { name: "login" };
  if (token && to.name === "login") return { name: "upload" };
  return true;
});

export default router;
