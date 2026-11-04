import { createApp } from "vue";
import { createPinia } from "pinia";

// Element Plus 只用来弹 toast，没必要引 300KB 的全量样式。
// 以后真用到它的表单/表格组件时，再补对应的 el-xxx.css。
import "element-plus/theme-chalk/base.css";
import "element-plus/theme-chalk/el-icon.css";
import "element-plus/theme-chalk/el-message.css";

// tokens 必须在 element 之后引入，才能盖掉它的 --el-color-primary
import "./styles/tokens.css";
import "./styles/base.css";

import App from "./App.vue";
import router from "./router";

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.mount("#app");
