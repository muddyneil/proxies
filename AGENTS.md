# AGENTS.md — Free Proxy Airport v7

面向 AI 编码助手的项目指南。本文件基于对 `generator.py` 当前源码的分析编写，描述架构、关键约束、命令与修改规则。

## 项目概述

机场级体验的免费 Clash / Mihomo 节点订阅生成器。核心工作流（由 GitHub Actions 每 30 分钟触发）：
聚合公开免费节点源 → 去重清洗 → 通过真实 Mihomo 实例多 URL / 多轮测速反复活节点（仅保留稳定且低延迟节点）→ 按健康评分排序并分组截断 → 生成 `output/clash.yaml` → 复制到 `docs/clash.yaml` 供 GitHub Pages 订阅分发；同时按健康评分 Top N 生成 Shadowrocket / V2Ray 格式订阅（base64）。

版本：`v7`（见 `generator.py` 顶部 `VERSION` 常量）。

## 目录结构

- `generator.py` — 唯一的主要脚本，完成全部分析、测速与生成逻辑
- `pyproject.toml` / `uv.lock` — 项目元数据与依赖锁定（`requests>=2.34.2`、`pyyaml>=6.0.3`，最少 `3.12`）
- `output/clash.yaml` — 生成的 Clash 配置（最终产物）
- `output/rocket.txt` / `output/v2ray.txt` — Shadowrocket / V2Ray 格式的 Top N 快节点订阅（base64，两者内容相同）
- `docs/clash.yaml` / `docs/rocket.txt` / `docs/v2ray.txt` — GitHub Pages 分发的副本（与 output 保持字节一致）
- `docs/.nojekyll` — 允许 Pages 服务 YAML / txt
- `tests/test_generator.py` — 针对纯函数（URI 转换 / 区域识别 / 评分 / 配置校验等）的单元测试（`python -m unittest discover -s tests`）
- `.github/workflows/update.yml` — CI/CD 工作流（生成 + 校验 + 发布）
- `README.md` — 面向最终用户的使用说明
- `AGENTS.md` — 本文件
- `.venv/` — uv 创建的本地虚拟环境（git 已忽略）

## 入口与执行

```bash
# 本地运行（Python 3.12+，依赖 requests / pyyaml）；`generator.py --validate-only [--config PATH]` 只校验配置不联网
python generator.py

# 使用 uv 搭建虚拟环境并运行
uv run --with requests --with pyyaml python generator.py
# 或基于 pyproject.toml/uv.lock 同步环境后运行
uv sync && uv run python generator.py
```

当脚本作为主模块运行时：捕获 `main()` 异常打印 `[ERROR]` 到 stderr 并 `raise`。脚本入口会把 stdout/stderr 强制配置为 UTF-8 以兼容中文输出。

`main()` 流程：
1. `collect_proxies()` 从各源抓取并清洗候选节点
2. `benchmark_proxies(candidates)` 用真实 Mihomo 测速（若失败则降级）
3. 若无存活节点 → 复用上一次输出（`load_existing_metrics`，降级）
4. 仍无节点 → 使用 DIRECT-FALLBACK 降级配置
5. `build_config` → `validate_config` → `write_config` → `print_summary`
6. 生成 Shadowrocket / V2Ray 订阅（Top N）并输出节点地区分布摘要

### 输出文件与降级保证

- 最终产物固定路径 `output/clash.yaml`（模块常量 `OUTPUT_PATH`），另生成 `output/rocket.txt` 与 `output/v2ray.txt`（Shadowrocket/V2Ray base64 订阅，取健康评分 Top `FREE_PROXY_AIRPORT_TOP_N`，默认 20）。
- **脚本绝不因无可用节点而崩溃退出**：内置三级降级（实时测速 → 复用上一次输出 → DIRECT-FALLBACK）。
- **输出文件不可留空或残缺**：`validate_config()` 保证至少有 proxy、缺失任一必需分组会抛错、缺失任一 AI 分流 / 关键规则会抛错，从而阻止提交无效订阅。
- 极端降级（无存活节点且无历史输出）时 `rocket.txt` / `v2ray.txt` 写入 base64 编码的占位说明（`DEGRADED_SUBSCRIPTION_NOTICE`），保证订阅文件始终非空且为合法 base64。

## 节点源（SOURCE_GROUPS）

当前共 7 个源，每个源：`name` + `primary` URL + `fallbacks`（替代 URL 列表，按顺序尝试，命中即 break）。

| 源 | primary |
| --- | --- |
| openRunner clash-freenode | `.../openRunner/clash-freenode/main/sub.yaml`（备选 clash.yaml） |
| snakem982 proxypool | `.../snakem982/proxypool/main/clash.yaml`（备选 source/clash-meta*.yaml） |
| Flikify Free-Node | `.../Flikify/Free-Node/main/clash.yaml`（备选 getNode） |
| free-clash-v2ray GitHub Pages | `https://free-clash-v2ray.github.io/uploads/latest.yaml`（备选 `discover:`） |
| PuddinCat BestClash | `.../PuddinCat/BestClash/refs/heads/main/proxies.yaml` |
| dongchengjie airport | `.../dongchengjie/airport/refs/heads/main/subs/merged/tested_within.yaml` |
| zhuhaiuk free-nodes | `.../zhuhaiuk/free-nodes/main/clash_config.yaml` |

特殊 fallback 标记 `discover:free-clash-v2ray` 会动态解析 GitHub Pages 的 README，正则提取 `https://free-clash-v2ray\.github\.io/uploads/\d{4}/\d{2}/[0-9]-\d{8}\.yaml` 并取前 8 个。

支持的代理类型 `SUPPORTED_PROXY_TYPES`：`ss, ssr, vmess, vless, trojan, hysteria, hysteria2/hy2, tuic, socks5, http`。不在列表内的类型直接丢弃。

## 数据流

### 抓取（fetch_text）
- 带自定义 `User-Agent`（`free-proxy-airport/v7 (+https://github.com/)`）与 Accept 头
- 超时 `SOURCE_TIMEOUT=25` 秒，最多 `MAX_RETRIES=3` 次，退避 `2*attempt` 秒

### 解析（extract_proxies 系列）
- 内容可能被 base64 包裹：`maybe_base64_decode` 在严格条件下解码（紧凑无空白、长度 4 整除、纯 base64 字符集、解码后包含 `proxies:` 或 `://`）。
- 优先用 `yaml.safe_load` 解析整个文档；失败或没有 proxies 时回退到 `extract_proxy_block` 手工提取 `proxies:` 代码块。

### 清洗去重（sanitize_and_deduplicate）
1. `normalize_proxy`：过滤 None 值、类型归一（`hy2`→`hysteria2`）、校验 server 与端口(1–65535)；无效直接丢弃。
2. `proxy_fingerprint`：对 type/server/port/uuid/password/cipher/network 等关键字段做 SHA-256 指纹，用于去重。
3. 重名节点通过加 `-N` 后缀保证唯一。

## 测速与评分（核心机制）

- **真实测速**：`benchmark_proxies` 自动下载 / 安装 `mihomo`（优先系统内已有 `mihomo`/`clash-meta`/`clash`，否则从 GitHub releases 选择匹配 OS+架构的资产下载缓存到临时目录）。生成临时 benchmark 配置，通过外部控制器 REST API 并发测速。
- **存活策略（多 URL × 多轮）**：`test_single_proxy` 对每个节点按 `TEST_URLS`（gstatic + cloudflare + android 连通性检查）逐一测速；一轮内需**所有 URL** 都返回有效延迟才算通过。每个节点进行 `PROBE_TIMES` 轮探测，通过轮数 ≥ `PROBE_PASS_MIN` 才判活（过滤抖动节点），以通过轮延迟的中位数作为代表值。单轮延迟 > `MAX_LATENCY_PASS_MS`（默认 2000）即判失败。
- **mihomo 资产选择**：首选 GitHub API；**API 限流/失败时自动回退**到 `releases/latest` 302 重定向取 tag + `expanded_assets` 页面抓取资产名（HTML 不受 API 限流）。选择资产时会优先 `compatible` 变体并按分数排序；选定 URL 先用 HEAD 预检，被拒后再用 Range 探测（200/206 即为可达）。`download_file` 直连失败时依次尝试镜像（ghfast.top / ghproxy.net / gh-proxy.com）。
- **二进制校验**：缓存与解压产物均检查魔数（`looks_like_binary`：MZ / ELF / Mach-O），损坏缓存自动清除并重新下载。
- 临时目录、控制器 URL 与混合端口均使用随机空闲端口（`find_free_port`），避免冲突。
- 通过 `ProxyMetric`（proxy/延迟/区域/健康分）记录结果。

### 健康评分（health_score）
```
0.6 * (1000.0 / latency) + 0.3 * region_bonus + 0.1 * stability
```
- 延迟以毫秒计：先归一化（1000/latency）再加权，避免 0.6/latency 被区域加成与随机稳定性项淹没。
- `region_bonus`：HK/SG/JP 为 3，US 为 2，其他 1。
- `stability`：由节点名 SHA-256 作为种子的确定性 0–1 随机值，保证同节点多次运行评分稳定但彼此有差异。
- 延迟代表值为通过轮的**中位数**；存活由 `MAX_LATENCY_PASS_MS`（默认 2000ms）+ 多 URL + 多轮共同判定（`test_single_proxy` / `_probe_*`）。`LATENCY_TIMEOUT_MS=5000` 仅作为降级配置与 `load_existing_metrics` 的占位延迟。

### 区域识别（detect_region）
基于节点名匹配英文关键词（`\bhk\b`、`japan`、`united states` 等）/ 中文（香港、日本、美国/美國、新加坡）/ 国旗 emoji，识别 HK、JP、US、SG，否则 `OTHER`。

### 分组逻辑（build_config）
- 所有存活节点先按健康分降序排序；`proxies` 列表保留全部存活节点（可选注入 `skip-cert-verify`）。
- `AUTO-FAST`：按健康分取 Top `AUTO_FAST_MAX`（默认 50）的精选池（url-test，`lazy`）；节点多时保持客户端低负载
- `ALL`：**全部存活节点**（select，不周期探测）；任何节点都可手动选择，AUTO-FAST 是其子集
- `HK-POOL` / `JP-POOL` / `US-POOL`：对应区域节点截断到 `REGION_POOL_MAX`（默认 20）；区域无节点时回退到前 5 个健康节点（`names_for_region`）
- `AI-POOL`：`low_latency_pool` 按 (latency, -health_score) 取延迟最低的一批（`min(max(3, len//5), 30)`）
- `FALLBACK`（fallback 类型）：串联 AUTO-FAST → HK-POOL → JP-POOL → US-POOL
- `PROXY`（select）：AUTO-FAST → FALLBACK
- 规则：AI 域名进 AI-POOL，`GEOIP,CN,DIRECT`，兜底 `MATCH,PROXY`
- 顶层字段：mixed-port 7890、allow-lan、ipv6、unified-delay、tcp-concurrent、global-client-fingerprint=chrome、generated-by/generated-at。

## 运行时配置（环境变量）

- `FREE_PROXY_AIRPORT_MAX_WORKERS`（默认 24）— 并发测速线程数
- `FREE_PROXY_AIRPORT_MAX_CANDIDATES`（默认 500）— 最大候选节点数限制（0=不限制）
- `FREE_PROXY_AIRPORT_TOP_N`（默认 20）— 进入 rocket.txt / v2ray.txt 的快节点数量（0=全部）
- `FREE_PROXY_AIRPORT_MAX_LATENCY_MS`（默认 2000）— 节点存活延迟上限（0=用 `LATENCY_TIMEOUT_MS`）
- `FREE_PROXY_AIRPORT_PROBE_TIMES`（默认 3）/ `FREE_PROXY_AIRPORT_PROBE_PASS_MIN`（默认 2）— 多轮探测轮数与通过下限（0 按 1 处理，避免静默全灭）
- `FREE_PROXY_AIRPORT_AUTO_FAST_MAX`（默认 50）— AUTO-FAST 精选池最大节点数（0=全部存活节点）
- `FREE_PROXY_AIRPORT_REGION_POOL_MAX`（默认 20）— 各区域池最大节点数（0=全部）
- `FREE_PROXY_AIRPORT_SKIP_CERT_VERIFY`（默认 0）— 对 vmess/vless/trojan/hysteria/hy2/tuic 全局注入 `skip-cert-verify`。**安全敏感、默认关闭**，见"修改 guideline"。
- `FREE_PROXY_AIRPORT_ALLOW_LAN`（默认 0）— 生成配置的 `allow-lan` 开关；设为 `1` 才允许局域网共享代理。
- `FREE_PROXY_AIRPORT_GENERATED_AT` — 覆盖输出中的 `generated-at` 时间戳，CI 可用于减少无意义 diff。
- 负整数环境变量视为非法并回退默认值；Mihomo 二进制下载仅做魔数与最小体积校验（CI-only 场景，不做 checksum pinning），直连失败时依次尝试镜像。
- 非法整数值会打印 `[WARN]` 并回退默认值，避免脚本崩溃。

## 必需输出约束（改动时必须保持）

`REQUIRED_GROUPS` / `REQUIRED_GROUP_TYPES` 与 `validate_config()` 一起强制以下分组存在、类型正确（AUTO-FAST/HK-POOL/JP-POOL/US-POOL/AI-POOL 为 `url-test`，ALL/PROXY 为 `select`，FALLBACK 为 `fallback`）且 `rules` 中必须含 AI 分流与兜底规则，否则抛错：
- `AUTO-FAST`, `HK-POOL`, `JP-POOL`, `US-POOL`, `AI-POOL`（url-test）
- `ALL`（select，全部存活节点）
- `FALLBACK`（fallback，串联 AUTO-FAST + 区域池）
- `PROXY`（select，AUTO-FAST → FALLBACK）
- 规则：4 条 AI 域名 + `GEOIP,CN,DIRECT` + `MATCH,PROXY`

CI 校验（update.yml）通过 `generator.py --validate-only --config docs/clash.yaml` 复用同一套校验逻辑（不再内嵌重复 Python 校验），并独立检查 这些分组的类型、非空 proxies、必需规则，并对 `docs/clash.yaml`、`docs/rocket.txt`、`docs/v2ray.txt` 做字节/内容校验（`cmp`、非空、base64 可解码），最后启动 `http.server` 验证 `docs/clash.yaml` 返回 HTTP 200。

## 修改 guideline

- 尽可能只改 `generator.py`：脚本是自包含的，CI 已处理抓取、校验与发布。
- 新增或修改 `SOURCE_GROUPS` / 分组 / 规则时，务必同时更新 `REQUIRED_GROUPS`、`REQUIRED_GROUP_TYPES`、`validate_config()` 以及 `update.yml` 中的校验逻辑（分组类型、必需规则、字节一致性），保证三者一致。
- 若调整测速逻辑，保持 "无节点不崩溃、输出不大写为空" 的降级保障。
- 修改输出路径需谨慎：`OUTPUT_PATH`、`ROCKET_OUTPUT`、`V2RAY_OUTPUT`、`docs/` 副本、CI 校验、README 引用都要同步。
- **代码风格**：源码注释与文档字符串保持英文；遵循 PEP 8（缩进 4 空格、行宽 ≤99、隐式续行对齐等）。添加 / 调整依赖时同步更新 `pyproject.toml` 并在本地用 `uv sync` 验证。
- 遵守本地法律法规与相关服务条款（项目为教育 / 自用聚合实验）。