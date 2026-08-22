# 20260823 Claude Review Fixes (Round 3)

- 修复时间：2026-08-23
- 依据：第三轮回审（中等 3 项 + 低 8 项）
- 验证：uv sync + 80/80 单元测试、`ruff check` 全绿、`--validate-only` 通过

## 修复清单

| 编号 | 级别 | 问题 | 修复 |
| --- | --- | --- | --- |
| R3-M1 | 中 | `_vless_to_uri` 只读顶层 `path`/`host`，Clash 的 `ws-opts.path` / `ws-opts.headers.Host` 被忽略 → vless+ws 订阅链接损坏（path 恒为 `/`） | 新增 `_ws_transport()` 共享助手，vmess/vless 统一读取嵌套字段；顶层字段仍优先 |
| R3-M2 | 中 | SS 节点 `plugin`/`plugin-opts` 被静默丢弃，带 obfs / v2ray-plugin 的链接不可用 | 新增 `_ss_plugin_param()` 按 SIP002 输出 `?plugin=`；无法表达的其他插件直接跳过该节点（返回空 URI） |
| R3-M3 | 中 | hysteria/hysteria2 URI 只认 `insecure` 字段，忽略 Clash 的 `skip-cert-verify` 与全局 `SKIP_CERT_VERIFY` 开关，与 trojan/tuic 行为不一致 | 三条件任一命中即输出 `insecure=1` |
| R3-L1 | 低 | vmess JSON 的 alpn 列表被 `str()` 序列化成 Python repr（`"['h2', 'http/1.1']"`） | 复用已有 `alpn_value()` 归一化 |
| R3-L2 | 低 | release 页资产抓取正则不含大写字母，资产名含大写时会整体漏配 | 正则加 `re.IGNORECASE` |
| R3-L3 | 低 | `.tar.gz` 资产会在解压阶段失败且不尝试下一个候选资产 | `filter_mihomo_assets` 直接排除 tar 包（解压器只支持单文件 .gz/.zip） |
| R3-L4 | 低 | GitHub API 返回有效 assets 但 tag 缺失/非法时直接最终失败，不落回 release 页回退分支 | API 分支先校验 tag 合法性（与页抓取同一正则），不合法则走回退 |
| R3-L5 | 低 | 引擎下载与 gzip/zip 解压无大小上限，恶意镜像可填满磁盘 / 解压炸弹 | 新增 `MAX_DOWNLOAD_BYTES`（256MB）：下载流式计数、gzip 解压计数、zip 展开前按 `file_size` 总量校验 |
| R3-L6 | 低 | trojan over ws/grpc 转订阅时丢失传输层信息 | 非 tcp 网络输出 `type=` + `path/host/serviceName`（纯 tcp 链接保持字节不变） |
| R3-L7 | 低 | `PROBE_PASS_MIN > PROBE_TIMES` 组合静默全灭（仅有降级兑底无提示） | 测速开始前打印 `[WARN]` 提示必然 0 存活 |
| R3-L8 | 低 | `fetch_text` 流式响应在异常路径未显式关闭 | 改为上下文管理器保证关闭 |

## 测试

- 新增 19 个回归测试（URI 传输字段 ×9、hy2 证书策略 ×4、资产筛选与回退 ×4、下载大小上限、探测配置告警、响应关闭）。
- 最终状态：**80/80 通过**，`ruff check generator.py tests/test_generator.py` 无告警。

# 20260822 Codex Review Fixes (Round 2)

- 修复时间：2026-08-22
- 依据：第二轮回审（修复 `collect_proxies` NameError 后的复查，新增 8 项发现）
- 验证：uv sync + 61/61 单元测试、`ruff check` 全绿、`--validate-only` 通过

## 修复清单

| 编号 | 问题 | 修复 |
| --- | --- | --- |
| R2-A | `collect_proxies` / `main()` 主流程零测试覆盖（NameError 漏检） | 新增 `MainFlowTest`：并行抓取路径、`main()` 冒烟（stub 抓取/测速/写盘）、测速分批容错 |
| R2-B | `clean_sni` 保留 `:port` 与空白 → 订阅中出现非法 SNI | 剥离 `host:port`（仅单冒号形式，IPv6 字面量不受影响）与全部空白 |
| R2-C | trojan / tuic 订阅硬编码 `allowInsecure=1`，与默认保留证书校验的策略矛盾 | 仅当节点 `skip-cert-verify` 或全局 `SKIP_CERT_VERIFY` 开启时输出；默认不再降低 TLS 安全性 |
| R2-D | IPv6 SSR 生成非标准订阅（`[addr]:port:...`） | `normalize_proxy` 直接丢弃 IPv6 SSR 节点（SSR 协议未定义 IPv6 host 形式） |
| R2-E | vmess / vless 的 tls 归一化不对称 | 补充注释说明 vmess 折叠未知 token 是有意为之（vmess 无 reality 实际部署），行为不变 |
| R2-F | 单节点毒化整个 500 节点测速配置 → 全部降级为旧输出 | 测速分批（`BENCHMARK_BATCH_SIZE=100`），单批启动失败只丢该批 |
| R2-G | lint 噪音 24 处 | 修复 4 处（import 排序、`Callable` 移入 `collections.abc`、`datetime.UTC`、`endswith(tuple)`）；`BLE001`/`S110` 在 pyproject 新增 ruff 配置显式忽略 |
| R2-H | `PROBE_TIMES=0` 静默全灭（0 轮探测 → 全部节点被淘汰） | 新增 `_probe_times()` / `_probe_pass_min()`，0 与负数钳制为 1，README / AGENTS 同步注明 |

## 测试

- 新增 8 个回归测试（clean_sni 端口/空白、SSR IPv6 丢弃、trojan/tuic allowInsecure 策略、probe 钳制、collect_proxies、main 冒烟、测速分批容错）。
- 最终状态：**61/61 通过**，`ruff check generator.py tests/test_generator.py` 无告警。

# 20260822 Codex Review Fixes

- 修复时间：2026-08-22
- 依据：`D:\Work\workspace\Code Review.md`（2026-08-21 全量复审）
- 验证：uv sync + 53/53 单元测试通过、compileall 通过、行宽合规、workflow YAML 解析通过

## 修复清单

### 高优先级

| 编号 | 问题 | 修复 |
| --- | --- | --- |
| F-H2 | URI 构建器对 IPv6 server 不加方括号，IPv6 节点测速通过但订阅链接非法 | 新增 `_uri_host()`，全部协议构建器统一处理 |
| F-H1 | Mihomo 二进制经第三方镜像下载无 checksum 校验 | **按项目决策不修**：仅 CI 使用，保留魔数+最小体积校验即可 |

### 中优先级

| 编号 | 问题 | 修复 |
| --- | --- | --- |
| F-M1 | 无 argparse，任何参数都会触发完整副作用流程 | 加入 argparse；新增 `--validate-only [--config PATH]` 离线校验入口 |
| F-M2 | `_probe_latency` JSON 解析异常违反"失败返回 None"契约 | 解析纳入 try/except，返回 None 并静默降级 |
| F-M3 | `find_free_port()` TOCTOU 竞争导致整轮测速降级 | 增加重试（默认 3 次），耗尽后报明确错误 |
| F-M4 | 输出文件非原子写入，中途崩溃破坏降级兜底依据 | 新增 `_atomic_write_text()`（临时文件 + `os.replace()`），三个输出文件统一使用 |
| F-M5 | `generated-at` 时间戳导致每次运行必然产生 git diff | 支持 `FREE_PROXY_AIRPORT_GENERATED_AT` 覆盖（CI 可固定时间戳消除提交噪音） |
| F-M6 | vless 非 reality 的 flow、vmess grpc serviceName 转换丢失 | flow 存在即输出；vmess 增加 grpc-service-name 映射并补测试 |

### 低优先级（F-L1 至 F-L9）

| 编号 | 问题 | 修复 |
| --- | --- | --- |
| L1 | `allow-lan: true` 默认值偏激进 | 改为默认 false，需 `FREE_PROXY_AIRPORT_ALLOW_LAN=1` 显式开启 |
| L2 | 负整数环境变量产生错误行为 | `_env_int` 对负值告警并回退默认值 |
| L3 | `health_score` 注释描述旧公式 | 更新注释与实现一致 |
| L4 | 复用历史输出残留 skip-cert-verify 与开关不一致 | 复用前剥离该字段，再统一走注入逻辑 |
| L5 | CI Python 版本 `"3.x"` 可能漂移 | 固定为 `"3.12"` |
| L6 | 源响应无大小上限 | 流式读取 + 8MB 上限（`MAX_SOURCE_BYTES`） |
| L7 | bool 端口通过校验产生荒谬节点 | `normalize_proxy` 显式拒绝 bool 端口 |
| L8 | 7 个源串行抓取耗时过长 | 改为线程池并行抓取（最多 8 并发），清洗去重仍在主线程 |
| L9 | Python/CI YAML 双份校验逻辑易漂移 | 抽取 `REQUIRED_RULES` 常量；CI 改为直接调用 `generator.py --validate-only --config docs/clash.yaml` |

## 文档同步

- `AGENTS.md`：补充新环境变量（ALLOW_LAN / GENERATED_AT）、负整数回退语义、Mihomo 下载校验策略说明、`--validate-only` 用法。
- `README.md`：新增「本地校验与环境变量」章节（离线校验命令 + 完整环境变量表）。

## 测试

- 新增 10 个回归测试覆盖上述修复（IPv6 URI、vless flow、vmess grpc、坏 JSON 探测、负数 env、bool 端口、原子写入等）。
- 最终状态：**53/53 通过**。

# 20260820 Codex Code Review

- 审查时间：2026-08-20
- 审查方式：双轴（Standards / Spec）并行子代理 + 本地复现验证

## 1. 审查范围

该仓库只有一个代码提交 `dfacd4a`（"Init repository."），其后 50 个提交均为 GitHub Actions 自动生成的 "Update subscriptions"（只修改 `docs/`、`output/` 产物）。因此本次审查覆盖 `dfacd4a` 引入的整个代码库（与 HEAD 的非产物文件完全一致）：

| 文件 | 说明 |
| --- | --- |
| `generator.py` | 唯一主脚本（v7，约 1500 行） |
| `tests/test_generator.py` | 单元测试（40 个） |
| `pyproject.toml` / `uv.lock` | 项目元数据与锁定依赖 |
| `.github/workflows/update.yml` | CI/CD（生成 + 校验 + 发布） |
| `.github/workflows/pages.yml` | GitHub Pages 部署 |
| `AGENTS.md` / `README.md` | 项目指南 / 用户文档 |

- Spec 基线：仓库无 issue tracker，以 `AGENTS.md` + `README.md` + `update.yml` 校验逻辑作为需求基线。
- Standards 基线：`AGENTS.md`（修改 guideline、代码风格）+ Fowler 代码味道基线（均为判断项；文档化规范优先）。

## 2. 运行环境验证（uv）

```text
uv sync                                             # OK（Python 3.14.7，7 个依赖）
uv run python -m unittest discover -s tests         # 40/40 通过
uv run python -m py_compile generator.py tests/test_generator.py  # OK
```

关键结论已本地复现：`generate_shadowrocket_sub([{"type": "direct"}]) == ""`（见 S1 / P2）。

## 3. Standards 轴发现

### S1（硬违规）DIRECT-FALLBACK 降级路径产出空订阅文件

`main()`（原 generator.py L1486-1494）在无存活节点且无历史输出时走 `DIRECT-FALLBACK`，此时 `proxy_to_uri()` 对 `type="direct"` 返回 `""`，`generate_shadowrocket_sub()`（L1441-1449）对空列表 `base64.b64encode(b"")` 产出空串。于是 `output/rocket.txt`、`output/v2ray.txt` 被写成**空文件**，违反「输出文件不可留空或残缺」「无节点不崩溃」；且 update.yml 的 "Validate Shadowrocket/V2Ray subscriptions"（`st_size==0` 即退出）在该兜底场景必然失败——降级约定在 CI 层失效。

### S2（判断项）Mysterious Name：`all_names` / `all_nodes` 命名颠倒

L1021 `all_names = _top_names(metrics, AUTO_FAST_MAX)` 实为 AUTO-FAST 精选子集，L1022 `all_nodes` 才是全量——命名与含义相反。

### S3（判断项）Duplicated Code：vmess / vless 的 tls 归一化几乎同构

`_vmess_to_uri`（L1280-1287）与 `_vless_to_uri`（L1330-1341）的 tls bool/字符串归一化逻辑重复（仅空值 token 与未知串处理不同），应抽共用 helper。

### S4（判断项）Duplicated Code：SNI 参数拼接重复

`sni = clean_sni(...); if sni: params.append(f"sni={quote(...)}")` 在 trojan（L1379-1381）、hysteria（L1404-1406）、tuic（L1430-1432）以及 vless 重复 4 次。

### S5（判断项）Repeated Switches / Shotgun Surgery：代理类型分类散落 4 处

代理类型分类散落于 `SUPPORTED_PROXY_TYPES`（L125）、`REQUIRED_FIELDS`（L142）、`proxy_to_uri` 的 if/elif（L1216-1234）、`_maybe_inject_skip_cert_verify` 的 TLS 类型集（L757）。新增类型需改 4+ 处；TLS 集合字面量同时在测试（L296）重复。

### S6（判断项）Data Clumps：每个 `_*_to_uri` 开头重复抽取 (server, port, name)

各转换函数开头重复解构同一三元组。

### S7（判断项）五个 url-test 分组字典同构

L1041-1080 五个 url-test 分组字典（AUTO-FAST / HK / JP / US / AI）结构完全一致，可数据驱动。

### S8（判断项）测试笔误 `casm`

tests/test_generator.py L110 变量名 `casm` 应为 `cases`。

### S9（判断项）测试与源码重复 TLS 类型字面量

tests/test_generator.py L296 与 generator.py L757 重复同一 TLS 类型集合，属同源重复。

### S10（判断项）CI 安装依赖用 pip 而非锁文件

update.yml L28 用 `pip install --upgrade requests pyyaml`，与 AGENTS.md / README 推荐的 `uv sync`+uv.lock 不一致，CI 依赖可能漂移。

### S11（判断项）孤儿空包 `src/free_proxy_airport`

`src/free_proxy_airport/__init__.py` 为空包，无任何引用，README / AGENTS 目录结构均未提及，属脚手架残留。

## 4. Spec 轴发现

### P1（严重）CI 校验在 commit+push 之后执行，无法「阻止提交无效订阅」

- Spec：AGENTS.md L52「…从而阻止提交无效订阅」；L140「CI 校验（update.yml）会再次独立检查…」体现准入语义。
- 代码：update.yml 中「Generate Clash subscription」（L30-49）已含 `git commit`/`git push`（L48），而「Validate Clash Verge subscription」（L51）、「Validate Shadowrocket/V2Ray subscriptions」（L119）在其后。类型 / base64 / HTTP 200 任一失败时坏提交已推送，且 pages.yml 会将其发布到 Pages。**校验应移到 commit 之前。**

### P2（严重）DIRECT-FALLBACK 降级路径写出空 base64 订阅

- Spec：AGENTS.md L52「输出文件不可留空或残缺」及三级降级保证。
- 代码：generator.py `main()` L1487-1490——降级时仅含 DIRECT-FALLBACK，`proxy_to_uri` 不支持 `direct`，`generate_shadowrocket_sub` 产出空串；`validate_config()`（L1118）只校验 Clash 配置，update.yml 的非空/base64 检查又在 push 后。最坏降级下 rocket.txt / v2ray.txt 必为空且被推送。

### P3（中）`validate_config()` 不校验分组类型

- Spec：AGENTS.md L145「务必同时更新 REQUIRED_GROUPS、validate_config() 以及 update.yml 中的校验逻辑（分组类型、必需规则、字节一致性），保证三者一致」。
- 代码：generator.py L1118 `validate_config` 仅查分组存在、非空、规则；类型（url-test / select / fallback）只在 update.yml（L57-69）且提交后检查。

### P4（中）`FREE_PROXY_AIRPORT_MAX_LATENCY_MS=0` 语义未实现

- Spec：AGENTS.md L124「（0=用 `LATENCY_TIMEOUT_MS`）」。
- 代码：L60 直接 `_env_int(..., 2000)`，无 0 处理；L873 `latency > MAX_LATENCY_PASS_MS` 在 0 时恒真→所有节点探测失败→静默降级，而非改用 5000ms。

### P5（轻）「中位数」实现偏差

- Spec：AGENTS.md L90/L103「以通过轮延迟的中位数作为代表值」。
- 代码：L908 `rounds[len(rounds) // 2]` 在恰好通过 2 轮时取两值中较大者（上中位），非标准中位数（偶数时应取两中间值的平均）。

### P6（轻）cmp 字节校验范围与文档不符

- Spec：AGENTS.md L140 称对三个 docs 文件做 `cmp`。
- 代码：update.yml 仅对 clash.yaml 执行 `cmp`（L53）；rocket/v2ray 只查存在/非空/base64。（生成后立即 cp，字节一致天然成立，影响小。）

### Spec（a）缺失项 /（b）越界行为

- 缺失项：未发现——7 源 + discover、测速/评分/分组/环境变量默认值、REQUIRED_GROUPS、三条输出路径等均有实现。
- 越界行为：无显著越界；mihomo 镜像回退、魔数校验、zip-slip 防护等均已在 AGENTS.md 文档化。

## 5. 合规项

注释/文档字符串均为英文、无 >99 字符行、无 tab，PEP 8 缩进/续行良好；`validate_config`/REQUIRED_GROUPS/update.yml 三方同步（类型校验缺失除外，见 P3）。

## 6. 修复状态

| 编号 | 问题 | 状态 | 修复方式 |
| --- | --- | --- | --- |
| S1 / P2 | DIRECT-FALLBACK 空订阅 | 已修复 | 新增 `shadowrocket_subscription_content()`：无 URI 可表达节点时写入 base64 编码的 `DEGRADED_SUBSCRIPTION_NOTICE`，订阅文件永不为空且为合法 base64；`main()` 使用该函数并区分 WARN/OK 日志 |
| P1 | CI 校验时序 | 已修复 | `update.yml` 将两个 Validate 步骤移到 `Commit output` 之前，校验失败即不提交不推送 |
| P3 | validate_config 类型校验 | 已修复 | 新增 `REQUIRED_GROUP_TYPES`，`validate_config()` 校验每个必需分组的 type，与 update.yml 校验口径一致 |
| P4 | MAX_LATENCY_MS=0 | 已修复 | 新增 `_max_latency_pass_ms()`：env=0 时回退 `LATENCY_TIMEOUT_MS`（5000ms），与 AGENTS.md 文档一致 |
| P5 | 中位数 | 已修复 | `test_single_proxy` 偶数轮取两中间值的整数平均（如 [100,200] → 150） |
| P6 | cmp 范围 | 已修复 | `update.yml` 对 rocket.txt / v2ray.txt 也做 `cmp --silent output/... docs/...` |
| S2 | all_names 命名 | 已修复 | 重命名为 `auto_fast_names`，与内容（AUTO-FAST 精选子集）一致 |
| S3 | tls 归一化重复 | 已修复 | 抽取 `_normalize_tls(value, default, pass_through)`，vmess/vless 共用 |
| S4 | sni 拼接重复 | 已修复 | 抽取 `_append_sni_param()`，vless/trojan/hysteria/tuic 4 处共用 |
| S5 | 类型分类散落 | 已修复（部分） | TLS 集合抽为 `TLS_PROXY_TYPES`（测试同步引用）；`proxy_to_uri` if/elif 改为 `_URI_BUILDERS` 分发表；新增类型仍需维护 3 处数据（SUPPORTED_PROXY_TYPES / REQUIRED_FIELDS / _URI_BUILDERS）但不再改逻辑 |
| S6 | Data Clumps | 保留（判断项） | `_*_to_uri` 各函数的 (server, port, name) 抽取已由 `ProxyMetric`/proxy dict 汇聚，且各构建器其余字段（uuid/password/cipher…）各不相同，抽出统一类型反而增加间接层；维持现状 |
| S7 | url-test 分组同构 | 已修复 | `build_config` 用 `url_test_groups` 数据驱动生成 5 个 url-test 分组 |
| S8 | casm 笔误 | 已修复 | 改名 `cases` |
| S9 | TLS 字面量重复 | 已修复 | 测试改用 `gen.TLS_PROXY_TYPES` |
| S10 | CI pip→uv | 已修复 | `update.yml`：`pip install uv` + `uv sync --frozen`，Python 调用统一 `uv run python`，与 AGENTS.md/uv.lock 一致 |
| S11 | 孤儿空包 | 已修复 | 删除 `src/free_proxy_airport/__init__.py`（pyproject 使用 `py-modules=["generator"]`，无任何引用） |

## 7. 修复验证（差异基线与改动后）

| 验证项 | 修复前 | 修复后 |
| --- | --- | --- |
| 单元测试 | 40/40 | **45/45**（新增 5 个：中位数偶数、分组类型校验、降级订阅非空、订阅保真、MAX_LATENCY=0） |
| `py_compile` | OK | OK |
| workflow YAML 解析 | — | OK（步骤顺序：Generate → Validate Clash → Validate Rocket/V2Ray → Commit） |
| 降级订阅内容 | `""`（空文件） | 132 字符合法 base64，解码为 `# DIRECT-FALLBACK: no live nodes available; ...` |
| `validate_config` 类型错误 | 不拦截 | 抛 `RuntimeError: ... has type ...; expected ...` |
| 行宽 ≤99 | OK | OK（generator.py / tests 无超长行） |
