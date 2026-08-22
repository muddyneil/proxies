# Proxies

来自 https://github.com/sunmiao4458/free-proxy-airport

## 能力

GitHub Actions 每 30 分钟自动聚合公开免费节点源，执行真实延迟测试（多 URL、多轮采样），剔除超时和无效节点，并按健康评分生成自动分组。

- 真实测速和 timeout 自动剔除
- 节点健康评分排序
- HK / JP / US / AI 自动分组
- FALLBACK 自动降级
- OpenAI / ChatGPT / Claude / Anthropic 智能分流

## 自动更新

工作流名称：`AI Self-Healing Proxy v7`

节点
- Shadowrocket: https://muddyneil.github.io/proxies/rocket.txt
- Clash: https://muddyneil.github.io/proxies/clash.yaml
- V2Ray: https://muddyneil.github.io/proxies/v2ray.txt
  

输出文件：

```text
output/clash.yaml
output/rocket.txt
output/v2ray.txt
docs/clash.yaml
docs/rocket.txt
docs/v2ray.txt
```

## 本地校验与环境变量

脚本主要面向 CI 使用，但支持离线校验已生成的配置：

```bash
uv sync
uv run python generator.py --validate-only --config docs/clash.yaml
```

可用环境变量（均可选）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `FREE_PROXY_AIRPORT_MAX_WORKERS` | 24 | 并发测速线程数 |
| `FREE_PROXY_AIRPORT_MAX_CANDIDATES` | 500 | 最大候选节点数（0=不限） |
| `FREE_PROXY_AIRPORT_TOP_N` | 20 | 快节点订阅数量（0=全部） |
| `FREE_PROXY_AIRPORT_MAX_LATENCY_MS` | 2000 | 存活延迟上限（0=用 5000ms 超时） |
| `FREE_PROXY_AIRPORT_PROBE_TIMES` / `_PROBE_PASS_MIN` | 3 / 2 | 多轮探测轮数与通过下限（0 按 1 处理） |
| `FREE_PROXY_AIRPORT_AUTO_FAST_MAX` | 50 | AUTO-FAST 池上限（0=全部） |
| `FREE_PROXY_AIRPORT_REGION_POOL_MAX` | 20 | 区域池上限（0=全部） |
| `FREE_PROXY_AIRPORT_SKIP_CERT_VERIFY` | 0 | 对 TLS 类节点注入 skip-cert-verify（安全敏感，默认关闭） |
| `FREE_PROXY_AIRPORT_ALLOW_LAN` | 0 | 生成配置的 allow-lan 开关（设 1 才允许局域网共享） |
| `FREE_PROXY_AIRPORT_GENERATED_AT` | 当前时间 | 覆盖 generated-at 时间戳 |
## 部署步骤

### 1. 推送代码到 GitHub

```bash
git push origin main
```

### 2. 运行 GitHub Actions

- 打开仓库 **Actions** 标签页，选择 `AI Self-Healing Proxy v7` 工作流
- 首次部署可点击 **Run workflow** 手动触发一次；之后每 30 分钟自动运行
- 运行成功后仓库会出现新的 `Update subscriptions` 提交（`docs/clash.yaml` 被刷新）

### 3. 开启 GitHub Pages（GitHub Actions 模式）

1. 仓库 **Settings** → **Pages**
2. **Build and deployment** → Source 选择 **"GitHub Actions"**
3. 仓库内已有 `.github/workflows/pages.yml`：每次推送 `main`（含 Actions 自动生成的 `Update subscriptions` 提交）会自动把 `docs/` 发布到 Pages
4. 也可以手动在 **Actions** 标签页运行 `Deploy GitHub Pages` 工作流，等待 1~2 分钟部署完成

> ⚠️ 必须使用 "GitHub Actions" 模式而非 "Deploy from a branch"：后者的隐式 Pages 工作流内部使用 Node.js 20 的 `actions/checkout@v4` / `actions/upload-artifact@v4`，会持续产生 Node 20 弃用警告；`pages.yml` 已全部使用 Node 24 的 action（`checkout@v6` / `configure-pages@v6` / `upload-pages-artifact@v5` / `deploy-pages@v5`）。

### 4. 验证订阅地址

浏览器访问，应直接显示/下载 YAML 内容。

### 5. Clash Verge 导入订阅

> 端口说明：`mixed-port` 是本地代理监听端口。Clash Verge 使用自身设置中的端口（默认 7897）并覆盖订阅值，因此无需修改订阅中的 `mixed-port`。

## 常见问题

| 问题 | 原因/解决 |
|------|-----------|
| Actions 运行失败 | 查看日志红色步骤；上游节点源 404 属正常现象，脚本内置降级机制兜底 |
| Pages 站点 404 | 确认 Settings → Pages 的 Source 已切换为 "GitHub Actions"，且 `Deploy GitHub Pages` 工作流运行成功 |
| 订阅更新慢 | Actions 每 30 分钟运行；可手动在 Actions 里 Run workflow |
| Node.js 20 弃用警告 | 来自隐式 Pages 工作流（`actions/checkout@v4` / `actions/upload-artifact@v4`）。已新增 `.github/workflows/pages.yml`（Node 24 action）替代，只需在 Settings → Pages 把 Source 切为 "GitHub Actions" 后警告即消失 |

请仅在遵守当地法律法规和相关服务条款的前提下使用。
