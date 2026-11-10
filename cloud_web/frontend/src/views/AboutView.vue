<script setup lang="ts">
import { useSiteStore } from "@/stores/site";

const site = useSiteStore();
/**
 * 关于与备案信息页。
 *
 * ★ 工信部要求：网站底部展示 ICP 备案号并链接到 beian.miit.gov.cn。
 *   本页是那个链接的落地页，同时承载「服务内容 / 隐私承诺 / 联系方式」——
 *   这些是 ICP 备案审核与用户信任的必备信息。
 * ★ 备案通过后把 ICP_NUMBER 换成真实备案号、HAS_ICP 改成 true。
 */
const ICP_NUMBER = "京ICP备00000000号";
const HAS_ICP = false;
const POLICY_URL = "https://beian.miit.gov.cn/";
</script>

<template>
  <div class="page">
    <h1 class="title">关于无痕匠</h1>

    <section class="block">
      <h2 class="h2">我们做什么</h2>
      <p class="p">
        无痕匠是一站式视频处理工具，提供视频去水印、水印清除等影像编辑能力。
        你上传自己的素材，我们在服务器上完成处理，交付成片与一份可读的质量验收报告。
      </p>
    </section>

    <section class="block">
      <h2 class="h2">素材处理承诺</h2>
      <ul class="list">
        <li>只处理<strong>你主动上传</strong>的素材，不解析、不下载任何第三方平台链接。</li>
        <li>原片与成片在 <strong>处理完成后即删除</strong>，默认保留不超过 {{ site.retentionText }}。</li>
        <li>算力节点只持有<strong>单文件临时访问凭证</strong>，不持有账号密钥。</li>
        <li>验收不达标<strong>不计费</strong>，失败自动退还积分。</li>
      </ul>
    </section>

    <section class="block">
      <h2 class="h2">联系我们</h2>
      <p class="p">
        使用中遇到问题，或需要批量处理 / 授权素材处理，请通过
        <a class="link" href="mailto:support@wuhenjiang.com">support@wuhenjiang.com</a>
        联系我们，工作日 24 小时内回复。
      </p>
    </section>

    <section class="block">
      <h2 class="h2">备案信息</h2>
      <p class="p">
        <template v-if="HAS_ICP">
          <a class="link" :href="POLICY_URL" target="_blank" rel="noopener noreferrer">{{ ICP_NUMBER }}</a>
        </template>
        <template v-else>
          <span class="pending">ICP 备案办理中</span>
        </template>
      </p>
      <p class="p muted">
        本站为工具类网站，不提供用户内容发布、交易或社交功能。
      </p>
    </section>
  </div>
</template>

<style scoped>
.page {
  max-width: var(--page-w);
  margin: 0 auto;
  padding: var(--sp-7) 20px var(--sp-8);
  /* 内容不足一屏时，不要在正文和页脚之间留大片空洞 */
  width: 100%;
}

.title {
  font-size: var(--fs-title);
  font-weight: 600;
  color: var(--ink-900);
  margin-bottom: var(--sp-6);
  letter-spacing: 0.01em;
}

.block {
  margin-bottom: var(--sp-6);
}

.h2 {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--ink-700);
  margin-bottom: var(--sp-3);
}

.p {
  font-size: var(--fs-body);
  line-height: 1.75;
  color: var(--ink-600);
}

.muted {
  color: var(--ink-400);
  font-size: var(--fs-small);
}

.list {
  margin: 0;
  padding-left: 20px;
  font-size: var(--fs-body);
  line-height: 1.9;
  color: var(--ink-600);
}

.list strong {
  color: var(--ink-700);
  font-weight: 600;
}

.link {
  color: var(--brand);
  text-decoration: none;
}

.link:hover {
  text-decoration: underline;
}

.pending {
  color: var(--ink-400);
}
</style>
