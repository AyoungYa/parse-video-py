import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: () => import("@/views/LoginView.vue") },
    { path: "/", name: "upload", component: () => import("@/views/UploadView.vue") },
    { path: "/tasks/:id", name: "result", component: () => import("@/views/ResultView.vue") },
    { path: "/points", name: "points", component: () => import("@/views/PointsView.vue") },
  ],
});

router.beforeEach((to) => {
  const token = localStorage.getItem("token");
  if (!token && to.name !== "login") return { name: "login" };
  if (token && to.name === "login") return { name: "upload" };
  return true;
});

export default router;
