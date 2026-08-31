# 20260829 Pipeline Quality and Mihomo Compatibility Fixes (Round 21)

- Fixed on: 2026-08-29
- Scope: dedicated code review of aggregation, sanitization, Mihomo installation and benchmarking, published-config validation and the Clash-only code boundary
- Basis: minimal reproducible samples confirmed that untrusted YAML with mixed-type keys could abort an entire collection round, that the cache refresh deleted the working engine before a new version was available, that scalar groups and an early MATCH could bypass project validation, that port races had no retry, and that the probe policy was duplicated; Mihomo v1.19.30 confirmed that the top-level `global-client-fingerprint` has been removed
- Verification: `uv run python -m unittest discover -s tests` **111/111 passed**, Ruff check, format check, `compileall`, `git diff --check` and offline validation of both baseline configs all passed; real validation with Mihomo Meta v1.19.30 against `docs/clash.yaml`, `output/clash.yaml` and the project's `--validate-with-mihomo` entry point all succeeded with no further compatibility errors

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R21-H1 | High | Upstream YAML nested mappings could mix string and integer keys; `proxy_fingerprint(sort_keys=True)` raised `TypeError` when comparing heterogeneous keys, so a single malicious node could abort the entire collection round | Recursively reject non-string mapping keys during sanitization, add per-node exception isolation in fingerprinting, and add regression tests |
| R21-H2 | High | The Mihomo cache refresh deleted the last verified binary before a new version was available; a failed download would also break subsequent offline runs | The new asset is downloaded, digest-verified and extracted in a unique temp directory inside the cache directory, then atomically replaced via `os.replace()`; a failed refresh keeps the old engine, and marker files are also written atomically |
| R21-M1 | Medium | `validate_config()` accepted string-form group `proxies`; an illegal structure could pass when the characters happened to resolve to a node name | Force `proxy-groups` to be a list of mappings with each group's `proxies` a non-empty string list, then run reference resolution |
| R21-M2 | Medium | Validation only required a `MATCH,PROXY` to exist, not to be last; an early MATCH made the AI/CN rules unreachable | Enforce that the final rule is `MATCH,PROXY` and reject any other MATCH rules before it |
| R21-M3 | Medium | The controller and mixed ports could be identical or occupied before Mihomo started; an address conflict aborted the whole benchmark round | Force the two listener ports to differ; explicit address-in-use errors from config validation or controller startup retry up to 3 times with fresh ports |
| R21-M4 | Medium | Partial-pass results allowed by `PROBE_PASS_MIN` were still dropped by the final all-rounds gate, and configurations that could never be published still started the engine | Remove the duplicated `PROBE_PASS_MIN`; nodes must pass all rounds and stop at the first failed round; `PROBE_TIMES` is bounded to 1–10 and benchmarking is skipped below the publication minimum of 3 rounds |
| R21-L1 | Low | A source exceeding the size limit was a deterministic failure, yet it was re-downloaded and backed off like a network error | Introduce a non-retryable source-policy exception; the cancel event stops other URLs from continuing to retry after a source URL is chosen |
| R21-L2 | Low | The Clash-only publishing pipeline still maintained hundreds of lines of unused Shadowrocket URI converters and many dedicated tests | Remove the unused URI subsystem and its tests; keep the Clash generation, validation and real benchmarking mainline |
| R21-L3 | Low | Docs still claimed 7 enabled sources and described an obsolete probe configuration | Sync README / AGENTS to the current 3 enabled sources, all-rounds probing and the new cache/port behavior |
| R21-L4 | Low | Mihomo v1.19.30 removed the top-level `global-client-fingerprint`; the config still loaded but kept printing errors and the field no longer took effect | Remove the dead top-level field from the benchmark and final configs, keep each node's own valid `client-fingerprint`, and update both baseline configs, keeping them byte-identical |

## Real-engine verification

- `D:\Temp\mihomo-windows-amd64-compatible-v1.19.30\mihomo.exe -t -f docs/clash.yaml` succeeded.
- `D:\Temp\mihomo-windows-amd64-compatible-v1.19.30\mihomo.exe -t -f output/clash.yaml` succeeded.
- After adding that directory to PATH, `uv run python generator.py --validate-with-mihomo --config docs/clash.yaml` succeeded and confirmed use of the specified binary.
- After removing the dead top-level field, none of the three validations printed the `global-client-fingerprint configuration is removed` log.

## Publishing boundaries

- The publication quality gate is unchanged: latency ≤ 800ms, at least 3 rounds, all rounds passed and jitter ≤ 300ms.
- No new dependencies; Pages permissions, deployment path and schedule unchanged.
- `output/clash.yaml` and `docs/clash.yaml` stay byte-identical.

---

# 20260829 Clash Protocol and Controller Reliability Fixes (Round 20)

- Fixed on: 2026-08-29
- Scope: dedicated code review of the proxy-protocol fields used by generated subscriptions, the Mihomo/Clash external controller API and client health checks
- Basis: reproduction confirmed that `dialer-proxy` could create unvalidated dependencies across benchmark batches; any early controller-process exit was misclassified as a node config error and triggered recursive bisection; child processes were not reaped after a force kill; the client's continuous health check still used an HTTP URL
- Verification: `uv run python -m unittest discover -s tests` **169/169 passed**, Ruff, format check, `py_compile` and `git diff --check` all passed; real `-t` against the final config and the benchmark configs for all 6 existing protocols with Mihomo Meta v1.19.30, plus a live controller `/delay` smoke test

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R20-H1 | High | Upstream's untrusted `dialer-proxy` was preserved verbatim without target-existence, cycle, batch or final-publish closure checks, potentially distorting chained-proxy benchmarks, failing configurations across batches, or leaving dangling references in the final subscription | Remove `dialer-proxy` from the proxy-field whitelist and strip cross-node dialer dependencies at the sanitization entry point |
| R20-M1 | Medium | Any Mihomo process exit before the controller was ready raised `BenchmarkConfigError`, so systemic faults such as port conflicts, permission problems or engine crashes also triggered up to ~2N recursive bisections | Run a real `mihomo -t` before starting the benchmark controller; only an explicit config-validation failure raises `BenchmarkConfigError`; later process exits keep stderr and are raised upward as system errors |
| R20-M2 | Medium | After `terminate()` timed out, `kill()` was called without another `wait()`, potentially leaving file locks that broke temp-dir cleanup on Windows and unreaped child processes on POSIX | After `kill()`, wait again with a bounded timeout so benchmark child processes are fully reaped |
| R20-L1 | Low | Pre-publication probing required all HTTP/HTTPS URLs to pass, but the final `url-test` / `fallback` groups used only an HTTP probe URL, so the client's continuous selection could favor nodes that only respond to whitelisted HTTP targets | Switch the primary `TEST_URL` to the Cloudflare HTTPS 204 address; the three-URL pre-publication filter keeps gstatic, Cloudflare and Android connectivity targets |

## Real-engine verification

- `docs/clash.yaml` passes `mihomo.exe -t -f docs/clash.yaml`.
- Sample nodes of the six protocols `http`, `hysteria2`, `socks5`, `ss`, `trojan`, `vless` from the current subscription were validated; the new benchmark `-t -d ... -f ...` argument combination passed.
- The external controller was started for real and `/proxies/{name}/delay` was called; an unreachable local smoke node was not kept as expected, and the process exited cleanly.
- Mihomo v1.19.30 also reported that the top-level `global-client-fingerprint` is removed but still accepted the current config; that compatibility migration is handled as a later standalone change to keep this round focused.

---

# 20260827 Source Selection Quality Reduction (Round 19)

- Fixed on: 2026-08-27
- Scope: the source quality snapshot showed that only 3 of 7 sources consistently produced valuable publishable nodes; the rest had too low a yield or were currently unavailable
- Basis: in this round's real Mihomo benchmarking, `snakem982`, `PuddinCat` and `zhuhaiuk` produced the final valid nodes; `openRunner`, `Flikify`, `free-clash-v2ray` and `dongchengjie` were temporarily disabled
- Verification: `uv run python -m unittest discover -s tests` **164/164 passed**, Ruff, format check and `git diff --check` all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R19-M1 | Medium | Low-quality sources produced many format-legal nodes that never passed live benchmarking, consuming candidate-pool and benchmark resources | `SOURCE_GROUPS` keeps only `snakem982 proxypool`, `PuddinCat BestClash` and `zhuhaiuk free-nodes` |
| R19-L1 | Low | If the paused sources' URLs and disabling reasons were deleted outright, re-enabling would require re-researching them | Keep the `openRunner`, `Flikify`, `free-clash-v2ray` and `dongchengjie` information in the `collect_proxies()` docstring |

## Publishing boundaries

- The node scoring formula, benchmark thresholds and grouping logic are unchanged.
- Paused sources are only removed from the current fetch flow; they can be restored later based on a new quality snapshot.

---

# 20260827 Benchmark and Client Configuration Consistency Fix (Round 18)

- Fixed on: 2026-08-27
- Scope: a node-focused code review found that the benchmark config and the final client config differed in network behavior, which could make nodes that passed benchmarking show `Timeout` in the client
- Basis: the benchmark did not explicitly mirror the final config's IPv6, TCP-concurrency, unified-delay and client-fingerprint settings; the client's `url-test` already used the primary URL from the initial filter list, so this round adds no probe sites and does not change the scoring formula
- Verification: `uv run python -m unittest discover -s tests` **164/164 passed**, Ruff, format check and `git diff --check` all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R18-H1 | High | The benchmark config did not mirror the final Clash config's IPv6, TCP-concurrency, unified-delay and client-fingerprint settings; domain-based nodes could resolve/dial differently during benchmarking than in the real client, causing client `Timeout` | The benchmark config now mirrors `ipv6: true`, `unified-delay: true`, `tcp-concurrent: true` and `global-client-fingerprint: chrome` |
| R18-L1 | Low | No regression test prevented the benchmark and final configs' network behavior from drifting apart again | Add a test comparing the network-related switches of both configs and confirming `AUTO-FAST` uses the primary `TEST_URL` probe URL |

## Publishing boundaries

- The existing three-URL, multi-round initial filtering and the publication quality gate are kept.
- `health_score` is not rewritten; no additional complex probe sites are added.
- `url-test` continues to use Mihomo's supported single primary URL `TEST_URL`; the three-URL check remains for pre-publication quality filtering.

---

# 20260827 Scheduled Workflow Re-registration Fix (Round 17)

- Fixed on: 2026-08-27
- Scope: `workflow_dispatch` runs succeeded repeatedly, yet no `schedule` run was produced at any half-hourly slot since the repository was created
- Basis: the GitHub API confirmed the default branch is `main`, the workflow is `active`, two manual runs both succeeded, but the total count of `event=schedule` runs is 0; the earlier `chore(ci): re-register schedule trigger (nudge)` only added a comment without changing the cron value
- Verification: workflow YAML parsing, `git diff --check`, 163 unit tests, Ruff, format check and offline validation of the existing Clash config

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R17-M1 | Medium | A comment-only nudge does not change `schedule.cron`, so it cannot reliably re-register a GitHub scheduled workflow | Actually change the cron from `7,37 * * * *` to `13,43 * * * *`, forcing GitHub to re-parse the schedule config and update the schedule actor; sync README and CI/CD docs |
| R17-L1 | Low | The workflow file alone cannot distinguish a job failure from a platform-level event not being created | The maintenance notes add a GitHub API diagnostic baseline: workflow active, default branch main, manual runs succeed, schedule run count is 0; after pushing, continue verifying through real `schedule` runs |

## Follow-up verification

- After pushing to `main`, observe at least 2–3 cycles at UTC `:13` / `:43` and query the `event=schedule` run records.
- If still 0, do Disable workflow / Enable workflow on the GitHub Actions page; if it keeps failing, contact GitHub Support with workflow ID `343703897` and the missing schedule times.
- GitHub scheduling remains best-effort with no guarantee of punctuality or delivery; introduce an external `workflow_dispatch` timer only if a strict SLA is needed.

# 20260827 Untrusted Input and Failure Isolation Fixes (Round 16)

- Fixed on: 2026-08-27
- Scope: dedicated code review of public node-source parsing, Mihomo benchmark exception isolation, offline semantic validation and the retained URI converters
- Basis: reproduction confirmed that 350 bytes of YAML could expand into millions of nodes via aliases; a systemic engine error triggered 31 recursive starts for 16 nodes; scalar `rules` and proxies missing protocol credentials could pass `--validate-only`
- Verification: `git diff --check`, `uv run python -m unittest discover -s tests` **163/163 passed**, Ruff, format check and offline validation of the existing `docs/clash.yaml` all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R16-H1 | High | The raw response size limit could not stop YAML aliases from expanding exponentially during sanitization serialization; a single untrusted source could exhaust the runner's memory | The custom SafeLoader bounds alias count and nesting depth; before JSON serialization, node-structure visits are bounded by expanded semantics and cyclic structures are rejected |
| R16-M1 | Medium | `_benchmark_batch_isolated()` bisected on every exception; a systemic engine fault could start the engine up to 199 times for a 100-node batch | Introduce `BenchmarkConfigError`; bisect only when the engine explicitly rejects the benchmark config before the controller is ready; all other exceptions are raised directly |
| R16-M2 | Medium | `validate_config()` performed substring membership checks on scalar `rules`; a single string concatenating all required rules could pass offline validation | Require `rules` to be a non-empty list of strings before checking required rules |
| R16-M3 | Medium | The final validation did not reuse `REQUIRED_FIELDS`, so configs missing the SS cipher/password or the TUIC uuid/password could pass `--validate-only` | The final per-proxy validation reuses the protocol required fields and honors the `hy2` → `hysteria2` type alias |
| R16-L1 | Low | `_env_flag()` returned false for any invalid text, so a typo in `FREE_PROXY_AIRPORT_DISABLE_MIRRORS` accidentally enabled mirrors | Parse true/false tokens explicitly; invalid values warn and fall back to the caller's default |
| R16-L2 | Low | The retained VLESS/TUIC URI converters did not URL-encode `encryption` / `congestion_control`, letting upstream values inject extra query parameters | URL-encode both parameters uniformly and add regression tests; Pages remains Clash-only |

## Publishing boundaries

- The live artifact and quality gate are unchanged: only live Clash nodes with latency ≤ 800ms, all rounds passed and jitter ≤ 300ms are published.
- `update.yml` still runs project semantic validation, a real `mihomo -t`, the deployment-file whitelist and Pages SHA-256 verification.
- No new dependencies; Pages permissions and output path unchanged.

---

# 20260827 Subscription Validation and CI/CD Handoff Fixes (Round 15)

- Fixed on: 2026-08-27
- Scope: dedicated code review of node retrieval, sanitization, subscription generation and the GitHub Pages CI/CD handoff
- Basis: reproduction confirmed the project-level `validate_config()` accepted invalid proxies missing protocol, server or port; the generated-time check could be bypassed with a future timestamp
- Verification: `git diff --check`, `uv run python -m unittest discover -s tests` **155/155 passed**, Ruff and format check all passed; a minimal invalid-proxy sample confirmed that final config validation rejects publication

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R15-H1 | High | `validate_config()` only required a proxy entry to have a `name`; obviously invalid subscriptions missing `type`, `server` or a valid `port` still passed `--validate-only` | Final config validation adds supported-protocol, non-empty-server and 1–65535 integer-port checks; regression tests for a missing server and an unknown protocol were added |
| R15-M1 | Medium | Python's `int(1.2)` silently truncates, so sanitization and the final config could treat a YAML float port as a valid integer | Both sanitization and final validation reject non-integer float ports, keeping entry-point and publishing-layer behavior consistent |
| R15-M2 | Medium | CD only checked whether `generated-at` was older than 1 hour; it neither rejected missing timezones nor future timestamps, so a misconfigured env var could bypass the freshness constraint | Require the timestamp to carry a timezone and normalize to UTC; reject timestamps older than 1 hour or more than 5 minutes in the future |

## Publishing boundaries

- The final `docs/clash.yaml` still passes both project semantic validation and a real `mihomo -t` before the Pages artifact is uploaded.
- Pages SHA-256 verification happens after deployment; the current workflow cannot reliably retrieve the previous Pages artifact and the custom domain also makes live-snapshot rollback unstable, so a failed check still marks the workflow red but does not automatically deploy a second rollback.

---

# 20260827 Scheduled Workflow Reliability Fix (Round 14)

- Fixed on: 2026-08-27
- Scope: after a successful manual run, GitHub Actions did not create automatic runs across several consecutive half-hourly slots
- Basis: the GitHub API showed the workflow was `active` with `main` as the default branch, but the `schedule` event run count was 0; the original cron sat on high-traffic scheduling minutes
- Verification: `git diff --check`, workflow YAML parsing and `uv run python -m unittest discover -s tests` **153/153 passed**

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R14-M1 | Medium | The cron sat at minute 00 and 30 of every hour, inside GitHub Actions' common scheduling peaks; under high platform load the scheduled task could be delayed or dropped | Change the cron to `7,37 * * * *`, keeping 30-minute cadence while avoiding the top and middle of the hour; sync README and CI/CD docs |
| R14-L1 | Low | README described scheduled runs as deterministic behavior without noting that GitHub Actions scheduling does not guarantee punctuality or delivery | Clarify that scheduling uses UTC and that scheduled tasks may be delayed or dropped, and that manual triggering via `workflow_dispatch` is available |

---

# 20260827 Clash-Only Node Pipeline Review Fixes (Round 13)

- Fixed on: 2026-08-27
- Scope: GitHub Actions periodically filters valid nodes and publishes a subscription for Clash clients via GitHub Pages
- Basis: dedicated code review of node filtering and subscription generation (2 medium + 2 low items, all addressed), with scheduling moved to every half hour
- Verification: `git diff --check`, workflow YAML parsing, `uv run python -m unittest discover -s tests` **153/153 passed**, Ruff, format check, offline validation of the existing Clash config and the `output/clash.yaml` / `docs/clash.yaml` byte-consistency check all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R13-M1 | Medium | `proxy_fingerprint()` hashed only a subset of protocol fields; config variants of the same endpoint such as IPv4/IPv6 or CA could be wrongly deduped before benchmarking, letting an invalid variant kept first crowd out a usable one | Compute SHA-256 over the full normalized config excluding `name`, merging only configs that are exactly identical but differ in name; regression tests for full-field and name-difference cases were added |
| R13-M2 | Medium | Even after a Clash config passed live Mihomo benchmarking and `mihomo -t`, a useless Shadowrocket/V2Ray URI that could not express a valid Clash node could still block the whole Pages deployment round | The publishing pipeline is now Clash-only; Rocket/V2Ray files are no longer generated, validated or deployed, and the related outputs, env vars and Pages hashes were removed; the only live artifact is `clash.yaml` |
| R13-L1 | Low | `ALL` contained all high-quality nodes, but `PROXY` did not reference it, so manually choosing one of those nodes in rule mode would not affect actual traffic | Add `ALL` to `PROXY`; sync both baseline configs and the constraints doc, and add a group-entry test |
| R13-L2 | Low | primary and fallbacks raced outright; a faster old fallback could win over a primary that succeeded slightly later, reducing candidate freshness | All URLs still start concurrently, but the primary gets a 3-second priority window; after the window the first non-empty fallback takes over, balancing source priority and Actions runtime; two concurrency-timing tests were added |
| R13-L3 | Low | The automatic update ran hourly; the subscription refresh interval was too long | Change the cron to `*/30 * * * *` (minutes 00 and 30 of every hour UTC) and sync README, CI/CD and maintenance docs |

## Final workflow

- `update.yml` triggers every half hour or manually, generating and deploying only `docs/clash.yaml` and `.nojekyll`.
- Candidates are deduped by full config first, then benchmarked with real Mihomo across three URLs and three rounds; publication still requires latency ≤ 800ms, 3/3 rounds passed and jitter ≤ 300ms.
- `FREE_PROXY_AIRPORT_REQUIRE_LIVE=1` stops deployment and keeps the previous Pages version when no qualifying live node exists.
- The final Clash config passes project semantic validation and a real `mihomo -t`; after deployment, `clash.yaml` is downloaded from Pages and checked against the SHA-256.

---

# 20260827 CI/CD Review and Workflow Simplification (Round 12)

- Fixed on: 2026-08-27
- Scope: the project only generates subscriptions via GitHub Actions and deploys them to GitHub Pages
- Basis: dedicated CI/CD code review (1 high + 1 medium + 5 low items, all addressed)
- Verification: `actionlint 1.7.11`, `git diff --check`, `uv run python -m unittest discover -s tests` **150/150 passed**, Ruff and format check all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R12-H1 | High | The manual recovery workflow only checked baseline-file non-emptiness and Base64 syntax; it did not validate freshness, Clash semantics, real Mihomo compatibility, consistency of the three subscriptions or live content — and the current baseline `generated-at: ci` could overwrite a valid Pages deployment | Delete `.github/workflows/pages.yml`; when automatic updates fail, GitHub Pages keeps the last successful deployment, and there is no longer an entry point that publishes unvalidated baseline samples |
| R12-M1 | Medium | Tests only ran during scheduled/manual generation; pushes and PRs had no immediate CI, and every scheduled run re-did checkout, Python/uv installation and dependency sync | Add `.github/workflows/ci.yml` running unittest, Ruff and format checks on PRs and pushes to `main`; the scheduled `update.yml` focuses on generation, validation and deployment |
| R12-L1 | Low | Before publishing, a file non-emptiness check, `--validate-only` and `--validate-with-mihomo` ran sequentially, though the latter already includes project config loading and semantic validation | Keep only `--validate-with-mihomo`, doing the project validation and real Mihomo validation in one step |
| R12-L2 | Low | `compileall` duplicated the existing unittest/Ruff coverage; a local Python HTTP Server can only validate stdlib static serving, not GitHub Pages | Remove the `compileall` and local HTTP Server steps; keep the post-deployment real Pages download verification |
| R12-L3 | Low | The same round uploaded both the Pages artifact and a diagnostic artifact containing duplicate `output/` / `docs/`, and the deploy job downloaded the latter only to compare three files | Remove the duplicate artifact upload/download; the `generate` job outputs three SHA-256s and `deploy-pages` downloads the files from real Pages and compares digests directly |
| R12-L4 | Low | The `deploy-pages` job did not read the repository yet held `contents: read` | Remove that permission, keeping only the deployment-required `pages: write` and `id-token: write` |
| R12-L5 | Low | README/AGENTS still described 30-minute execution, scheduled tests and manual baseline recovery, inconsistent with the actual flow | Move scheduling to hourly; sync the CI/CD separation of duties, Pages publishing boundaries and failure notes |

## Final workflow

- `ci.yml`: runs tests and static checks on PRs and pushes to `main`; a new run on the same ref cancels the old one.
- `update.yml`: triggers hourly or manually; performs live generation, the publication quality gate, real Mihomo validation, URI semantic validation, the deployment-file whitelist, Pages artifact deployment and live SHA-256 verification.
- The generate job keeps read-only repo permissions; only the deploy job gets Pages/OIDC write permissions. All third-party Actions stay pinned to commit SHAs.

# 20260827 Node Quality and Review Fixes (Round 11)

- Fixed on: 2026-08-27
- Basis: dedicated node-quality code review (2 high + 2 medium items, all addressed)
- Verification: `uv run python -m unittest discover -s tests` **150/150 passed**, Ruff, format check, compileall and offline validation of the existing `output/clash.yaml` all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R11-H1 | High | `TOP_N=20` just took the first 20 of all "alive" nodes; nodes with ~2000ms latency, only 2/3 rounds passed or heavy jitter were published too, padding the count with low-quality nodes | Add the `publishable_metrics()` publication quality gate: representative latency ≤ 800ms, all probe rounds passed (default 3/3) and inter-round jitter ≤ 300ms; `TOP_N` is now only an upper bound and shortfalls below 20 qualifying nodes are not filled |
| R11-H2 | High | `health_score` awarded points for regions identified from upstream node names, so a fake HK node at 1500ms could outrank a generic node at 700ms; names come from untrusted sources and must not affect quality ranking | Remove the region influence on the health score; publication ordering uses `(latency, jitter_ms, -health_score)` and region information is used only for HK/JP/US grouping |
| R11-M1 | Medium | Round-median latency and a simple success ratio cannot express inter-round jitter, so unstable nodes like 50ms/1950ms could be kept | `ProxyMetric` gains `pass_count` and `jitter_ms`; `test_single_proxy` records passed rounds and max-minus-min jitter, and the quality gate rejects out-of-limit nodes |
| R11-M2 | Medium | GitHub Actions only required at least one live node, without validating the absolute quality of the finally published nodes; when quality was insufficient it could still generate and deploy degraded results | `FREE_PROXY_AIRPORT_REQUIRE_LIVE=1` now requires at least one live node meeting the publication quality gate, otherwise the workflow fails and keeps the previous Pages version; an Actions-equivalent semantic test was added |

## Degradation strategy

- Historical output has no per-round benchmark or jitter evidence and is no longer treated as high-quality; local runs only generate a `DIRECT-FALLBACK` placeholder config when no qualifying live node exists.
- When live generation fails or no node passes the quality gate, GitHub Actions fails outright without reading historical output, so low-quality nodes are never published to Pages.
- Clash, Shadowrocket and V2Ray were all generated from the same batch of nodes that passed the quality gate; subscription node counts may be below 20.

## Tests

- New tests for the quality-gate filter, no Top-N padding with low-quality nodes, region names not overriding latency, jitter recording and the strict Actions failure.
- `ProxyMetric` probe results now keep passed rounds and jitter for later diagnostics and quality auditing.

---

# 20260827 Subscription and Engine Hardening (Round 10)

- Fixed on: 2026-08-27
- Basis: code review (1 high + 2 medium + 1 low, all addressed)
- Verification: `uv run python -m unittest discover -s tests` **146/146 passed**, Ruff, format check, compileall, existing config validation and current subscription URI semantic validation all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R10-H1 | High | SS userinfo used standard Base64; passwords with special bytes produced `/` or `+`, breaking URI authority parsing and making the subscription link unusable | SS URIs now use the already-available URL-safe Base64 encoding; URI parsing regression tests were added; Actions gained protocol-level URI semantic checks |
| R10-M1 | Medium | Hysteria's `auth-str` field survived sanitization, but URI conversion only read `auth`/`password`, silently dropping the authentication info | URI conversion now falls back through `auth` → `auth-str` → `password`; `auth-str` is included in the node fingerprint and tests were added |
| R10-M2 | Medium | TUIC nodes missing `password` could still enter Mihomo benchmarking; one bad entry could fail the whole batch of up to 100 nodes | TUIC's required fields now include `password`; when a batch fails to start, bad nodes are isolated by recursive bisection while healthy nodes in the same batch are kept |
| R10-L1 | Low | The Mihomo cache only checked magic bytes, so a replaced or corrupted cached binary could be executed directly | Add a separate `binary-sha256.txt`; on cache hit, recompute and verify the decompressed binary's digest, deleting and re-downloading on mismatch |

## Publishing validation

- Before Pages deployment, `update.yml` parses VMess/SSR payloads and uses the standard library to check every subscription URI's scheme, hostname, port and authentication fields.
- Archive digests and decompressed-binary digests are stored separately so the archive digest is never applied to the binary.

## Tests

- Regression tests were added for SS URL-safe Base64, Hysteria `auth-str`, TUIC required fields, batch bisection isolation and cache digest match/mismatch.
- Final state: **146/146 passed**; plus Ruff, format check, compileall and `--validate-only`.

---

# 20260826 GitHub Actions Hardening (Round 9)

- Fixed on: 2026-08-26
- Scope: the project only generates in GitHub Actions and publishes via GitHub Pages
- Verification: `uv sync --frozen`, **141/141** unit tests, Ruff, format check, compileall, workflow YAML parsing and local subscription semantic validation all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R9-C1 | Critical | A high-privilege job downloaded and executed Mihomo with only a magic-byte check; a poisoned third-party mirror could take over the repo/Pages publishing chain | Release API assets must provide an official SHA-256, verified byte-for-byte after download; mirrors are off by default and even when enabled must match the official digest; external engine processes have GitHub/OIDC env vars stripped; the generate job has only `contents: read` and checkout does not persist credentials |
| R9-H1 | High | Actions did not run unit tests and Ruff | Add a dedicated `test` job running unittest, Ruff check/format check and compileall; the generate job must depend on its success |
| R9-H2 | High | Project validation cannot replace Mihomo's real parsing of the final config | Add `--validate-with-mihomo`, running `mihomo -t -f` against the final `docs/clash.yaml` before publishing |
| R9-H3 | High | When all live benchmarks died, historical output was reused, the workflow stayed green and stale nodes were silently published | Add `FREE_PROXY_AIRPORT_REQUIRE_LIVE`; Actions always sets 1, failing and keeping the previous live version when no live node exists; published artifacts use a real UTC `generated-at` and admission requires it to be within 1 hour |
| R9-H4 | High | `update.yml` and `pages.yml` could deploy concurrently, letting an old artifact overwrite new content | Automatic deployment is only in `update.yml`; `pages.yml` is manual recovery only; both share the `proxy-pages-production` concurrency group |
| R9-H5 | High | Unknown fields and oversized nested values from upstream proxy objects passed verbatim into Mihomo/published files | `ALLOWED_PROXY_FIELDS` constrains the proxy schema; sanitized entries are capped at 64 KiB each, unknown fields are stripped and oversized nodes are dropped |
| R9-M1 | Medium | `uv` and Ruff were not locked as a reproducible CI toolchain | Pin `astral-sh/setup-uv@v9.0.0` by SHA with uv 0.12.6; Ruff 0.16.4 joins the dev dependencies and `uv.lock` |
| R9-M2 | Medium | Committing generated files every 30 minutes kept bloating Git history and forced the generate job to hold write permissions | The automatic flow no longer commits; the same-round validated Pages artifact is deployed directly and diagnostic artifacts are kept for 1 day; repo generated files are only baseline samples |
| R9-M3 | Medium | Only runner-local HTTP was checked; nothing confirmed the real Pages had published this round's content | After deployment, download the three public files with cache-busting parameters, byte-compare with the same-round artifact and retry; a mismatch fails the workflow |

## Publishing boundaries

- The workflow top level has only `contents: read`; only the `deploy-pages` job gets `pages: write` / `id-token: write`.
- Generated artifacts allow only the four fixed `docs` files and three fixed `output` files, rejecting symlinks, extra directories and files.
- Shadowrocket/V2Ray files must be byte-identical to the re-converted final Clash Top-N result and contain URI lists rather than degradation notices.

---

# 20260826 Code Review Fixes (Round 8)

- Fixed on: 2026-08-26
- Basis: eighth code review (3 high + 2 medium, all addressed)
- Verification: `uv run python -m unittest discover -s tests` **131/131 passed**, `compileall`, `ruff check` / `ruff format --check` and `--validate-only --config docs/clash.yaml` all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R8-H1 | High | The VLESS Reality converter only recognized `tls: reality` with top-level `pbk/sid/spx`, so real Clash's `tls: true`, `reality-opts` and `client-fingerprint` were exported as plain TLS URIs missing key parameters | Recognize nested `reality-opts`, map `public-key/short-id/spider-x` to `pbk/sid/spx`, keep `client-fingerprint`, and add the nested fields to the node fingerprint |
| R8-H2 | High | Hysteria2 URIs lost `obfs` / `obfs-password`, so Salamander nodes could not connect | Hysteria2 URIs encode and keep both obfuscation parameters; `obfs-password` joins the node fingerprint |
| R8-H3 | High | HTTP proxies with `tls: true` were always exported as plaintext `http://` | Select `https://` from the normalized TLS flag; non-TLS nodes keep `http://` |
| R8-M1 | Medium | Upstream nodes could be named `DIRECT`, `PROXY`, `BENCHMARK`, etc., sharing the namespace with Mihomo built-ins/groups and causing conflicts | Sanitization reserves built-in names, required group names and `BENCHMARK`, auto-suffixing conflicting nodes; config validation rejects reserved names and duplicate proxy/group names |
| R8-M2 | Medium | SS sanitization did not require `cipher`, so an invalid node could prevent the whole 100-node benchmark batch from starting | Add `cipher` to SS required fields, dropping invalid nodes before they reach the Mihomo benchmark config |

## Tests

- New `RoundEightFixTest` with 8 cases: Reality nested fields, Hysteria2 obfuscation parameters, HTTP/HTTPS scheme, reserved-name renaming and validation, required SS cipher, and fingerprints of nested protocol fields.
- Final state: **131/131 passed**; `compileall`, `ruff check`, `ruff format --check` and `--validate-only` all green.

# 20260827 Code Review Fixes (Round 7)

- Fixed on: 2026-08-27
- Basis: seventh code review (1 high + 2 medium + 3 low + 7 trivial, all addressed)
- Verification: `uv run python -m unittest discover -s tests` **123/123 passed**, `py_compile`, `ruff check` / `ruff format --check` and `--validate-only --config docs/clash.yaml` all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R7-H1 | High | `Update subscriptions` commits pushed by `update.yml` with `GITHUB_TOKEN` do not trigger other workflows (GitHub's anti-recursion rule), so `pages.yml`'s `on: push` was never fired by bot commits; Pages effectively only updated on manual pushes/triggers, contradicting README's "automatic publish every 30 minutes" description | `update.yml` gains `pages: write` / `id-token: write` and a `deploy-pages` job (`needs: update`, SHA-pinned checkout v6.1.0 / configure-pages v6.0.0 / upload-pages-artifact v5.0.0 / deploy-pages v5.0.0, checkout `ref: main` for the latest commit) that publishes `docs/` directly; README/AGENTS.md deployment descriptions were corrected |
| R7-M1 | Medium | The defensive `["DIRECT"]` branch of `names_for_region`/`low_latency_pool` conflicted with `validate_config`'s reference check (observed raising "references unknown node/group: 'DIRECT'"); once reachable it would turn a degradation into a crash | Add mihomo's built-in `DIRECT` to `validate_config`'s `resolvable` set; regression test added |
| R7-M2 | Medium | `_b64url` used the standard base64 alphabet (`+/`) while the SSR spec requires URL-safe (`-_`); passwords/params with special bytes produced links that strict clients mis-parse; the docstring did not match reality | `_b64url` switches to `urlsafe_b64encode`; SSR's outer payload goes through `_b64url` uniformly; alphabet and full-chain regression tests were added |
| R7-L1 | Low | The mihomo download fell back to third-party mirrors (ghfast.top etc.) with only magic+size checks; a poisoned mirror could execute on the runner (AGENTS.md had flagged this as accepted risk) | Add `FREE_PROXY_AIRPORT_DISABLE_MIRRORS` (default 0, preserving the original behavior; mirrors are only tried after a direct download fails); extraction entry point consolidated into `_download_attempt_urls`; switch regression test added |
| R7-L2 | Low | `update.yml` had no `timeout-minutes`; an extreme hang could occupy the runner up to the 6h default | `update` job 30 minutes, `deploy-pages` job 15 minutes |
| R7-L3 | Low | `fetch_text` backed off and retried on 4xx too, wasting ~6s per dead link | `requests.exceptions.HTTPError` 400–499 fails immediately without retries; 5xx/network errors keep the backoff retries; 404 fast-fail and 500-retry regression tests were added |
| R7-N1 | Trivial | Both `except` branches of `_probe_latency` returned None — redundant | Merge into a single `except Exception` |
| R7-N2 | Trivial | In `_fetch_source`, `except Exception: continue` was dead code (`_fetch_one_url` never raises) | Remove it |
| R7-N3 | Trivial | When the API returned matching assets but all were unreachable, `select_mihomo_asset` raised directly instead of falling back to the release-page path | Extract `_first_reachable_asset`; when all API assets are unreachable, clear the matches and take the release-page fallback (note: the availability check must be **before** the fallback branch); regression test added |
| R7-N4 | Trivial | The engine cache directory accumulated leftovers: extracted README/LICENSE files and old-version installers lingered forever | Extraction moves to a one-shot `TemporaryDirectory` and installers are deleted after install; gz/zip extraction regression tests added |
| R7-N5 | Trivial | `update.yml` ran `cmp` right after `cp` (always true, meaningless) | Remove the three `cmp` calls, keeping the non-empty/base64/http checks |
| R7-N6 | Trivial | `--validate-only` raised raw AttributeError/NoneType on missing/empty files | Friendly `[ERROR] cannot read config file` / `empty or not a mapping`, exit code 1 |
| R7-N7 | Trivial | The phrase in AGENTS.md that described the non-empty-output guarantee contained a typo | Corrected it to “output must not be empty” |

## Tests

- New `RoundSevenFixTest` with 9 cases: built-in DIRECT reference ×1, SSR URL-safe alphabet ×2, 404 fast-fail / 500 retry ×2, mirror switch ×1, unreachable-API-asset fallback ×1, gz/zip extraction ×2.
- Final state: **123/123 passed**; `ruff check` / `ruff format --check` all green; `--validate-only` passed.

---

# 20260826 Code Review Fixes (Round 6)

- Fixed on: 2026-08-26
- Basis: sixth code review (1 medium + 5 low + 2 suggestions, all fixed)
- Verification: `uv run python -m unittest discover -s tests` **114/114 passed**, `py_compile`, `ruff check` / `ruff format --check` and `--validate-only --config docs/clash.yaml` all passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R6-M1 | Medium | `health_score`'s tie-break magnitude badly disagreed with its comment: `hash/0xFFFFFFFF` ranges over [0,1] (the comment claimed ~2e-10), so latency ordering was flipped purely by the name hash (observed: a 1900ms node scored below a 2000ms node), breaking "latency dominates" | Add `TIE_BREAK_SCALE=1e-9` scaling far below the smallest real score difference (~2.4e-5 at 5000ms), used only to keep exact ties stably ordered |
| R6-L1 | Low | The vless/trojan/hysteria/tuic URI builders only read `sni`, ignoring `servername` (vmess accepts both); when upstreams only provide `servername`, the subscription link lacks SNI and becomes a dead link | All four builders now fall back `sni or servername`; 4 regression tests added |
| R6-L2 | Low | `proxy_fingerprint` omitted ss `plugin`/`plugin-opts`, http/socks `username`, hy `auth`/`up`/`down`, tuic `congestion_control`/`alpn` and vless reality `pbk`/`sid`/`spx`, so real nodes differing in credentials/transport could be wrongly deduped | The fingerprint includes all of the above; differentiating regression tests added |
| R6-L3 | Low | Upstream-declared `skip-cert-verify`/`insecure` were passed through verbatim (the committed artifacts already contained source nodes with `skip-cert-verify: true`), contradicting README's "off by default, security-sensitive" | `normalize_proxy` strips both fields on ingest, `_maybe_inject_skip_cert_verify` strips them as a bottom-line when off, and `load_existing_metrics` strips `insecure` too; certificate policy is decided solely by the single `FREE_PROXY_AIRPORT_SKIP_CERT_VERIFY` entry point |
| R6-L4 | Low | `sanitize_and_deduplicate` is dead code left over from the R4 refactor, with no callers/tests | Delete it; the AGENTS.md sanitization section title was synced to `sanitize_interleaved` |
| R6-L5 | Low | pyproject claimed "only E4/E7/E9/F run by default; BLE/S are dead config", but newer ruff (0.16.x) enables BLE/S preview rules by default, and `ruff check` reported 24 errors | `[tool.ruff.lint]` explicitly sets `select = ["E4", "E7", "E9", "F"]`, eliminating version drift |
| R6-N1 | Suggestion | `validate_config` did not check whether names referenced by group `proxies` exist, letting dangling references through | Add reference-resolution validation: every entry of every group must be in {proxy names} ∪ {group names}, otherwise raise |
| R6-N2 | Suggestion | `collect_proxies`'s `executor.map` had no per-source exception isolation; a structural source error (e.g. missing `primary`) crashed the whole collection | Switch to per-source `submit` + `as_completed`; a single-source failure degrades to an empty result with a `[WARN]`, and results keep `SOURCE_GROUPS` order |

## Tests

- New `RoundSixFixTest` with 15 cases: bounded tie-break / latency dominance / distinguishable ties ×4, servername fallback ×4, fingerprint differentiation ×3, centralized cert-policy control ×3, group reference validation ×2, per-source structural failure degradation ×1.
- Final state: **114/114 passed**; `ruff check` / `ruff format` all green; `--validate-only` passed.

---

# 20260825 Code Review Fixes (Round 5)

- Fixed on: 2026-08-25
- Basis: fifth code review (1 medium + 5 low + 7 suggestions)
- Verification: `python -m unittest discover -s tests` **99/99 passed**, `py_compile` and `--validate-only --config docs/clash.yaml` passed (ruff unavailable locally, so lint was not re-run)

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R5-M1 | Medium | URI converters leaked explicit `null` fields as the literal string `"None"` (vmess `sni`/`fp`/`alterId`/`cipher`, vless `tls`/`encryption`/`fp`); same root cause as R4-L1, which had only hardened alpn | `_normalize_tls(None)` returns `default`; vmess/vless fields use `... or ""` / `... or default` defensively; 3 regression tests added |
| R5-L1 | Low | When `_fetch_source` returns early, non-daemon background threads linger; process exit could be delayed by needless waiting | Comment documents that exit joins background threads, bounded by `fetch_text`'s retry/timeout budget (behavior kept; trade-off made explicit) |
| R5-L2 | Low | `mihomo_asset_available`'s Range probe was not `stream=True`; a target ignoring Range would load the whole body into memory | Use `stream=True` + `next(iter_content(1))` to read a single byte and close |
| R5-L3 | Low | `download_file` had no User-Agent and no backoff between mirror fallbacks | Add the same UA header as `fetch_text`; `time.sleep(2)` before mirror retries |
| R5-L4 | Low | When reusing historical output, `load_existing_metrics` gave every node the same 5000ms, losing the original latency ordering | Docstring documents that only the node list is persisted and the degraded fallback cannot restore latency ordering (deliberate trade-off; behavior unchanged) |
| R5-L5 | Low | `SKIP_CERT_VERIFY` (`_env_int(...)==1`) and `ALLOW_LAN` (`_env_flag`) parsed booleans inconsistently | `SKIP_CERT_VERIFY` now uses `_env_flag()` too |
| R5-N1 | Suggestion | `.ruff_cache/` was missing from `.gitignore` | Add `.ruff_cache/` |
| R5-N2 | Suggestion | pyproject's ruff `ignore` (BLE001/S110/S112) was dead config (selectors not enabled; only E4/E7/E9/F by default) | Remove the dead ignores and add a comment |
| R5-N3 | Suggestion | `avg_latency` was computed twice with inconsistent precision | Remove the duplicate computation in `main()`'s region block, keeping `print_summary`'s `avg_latency_ms` |
| R5-N4 | Suggestion | `low_latency_pool`'s empty-list `return ["DIRECT"]` is unreachable in `build_config` | Mark it as a defensive branch |
| R5-N5 | Suggestion | `validate_config` did not validate `proxies` entries | Per-entry validation: each entry must be a dict with a non-empty `name`, otherwise raise |
| R5-N6 | Suggestion | `extract_mihomo_binary` left behind the versioned intermediate binary | `extracted.unlink(missing_ok=True)` after copying to the canonical `binary` |
| R5-N7 | Suggestion | `mihomo_asset_score`'s `go120` rule lacked a comment | Docstring explains compatible-first and go120 de-prioritization |

## Tests

- New `NullFieldGuardTest` (3 cases): `_normalize_tls(None)`, vmess empty fields and vless empty fields no longer produce `"None"`.
- `test_download_rejects_oversized_payload` now patches `gen.time.sleep` so `download_file` backoff does not slow the test with real sleeps.
- Final state: **99/99 passed**; `py_compile` passed; `--validate-only` passed.

# 20260825 CI Action Version Re-check (Round 4 Addendum)

- Date: 2026-08-25 (api.github.com was unreachable; verified via `git ls-remote` + raw action.yml)
- Result: every action version used by the workflows really exists and all runtime at node24 (`upload-pages-artifact` is composite); `checkout` / `setup-python` have newer v7 majors, but they are also node24, so there is no urgent upgrade need.
- Action taken: both workflows are locked to commit SHAs (with version comments), removing floating-tag supply-chain risk:
  - `actions/checkout@d23441a...` (v6.1.0)
  - `actions/setup-python@ece7cb0...` (v6.3.0)
  - `actions/configure-pages@45bfe01...` (v6.0.0)
  - `actions/upload-pages-artifact@fc324d3...` (v5.0.0)
  - `actions/deploy-pages@cd2ce8f...` (v5.0.0)

---

# 20260825 Code Review Fixes (Round 4)

- Fixed on: 2026-08-25
- Basis: fourth review round (3 medium + 8 low)
- Verification: uv sync + 96/96 unit tests, `ruff check` / `ruff format` all green, `--validate-only` passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R4-M1 | Medium | DIRECT-FALLBACK used the illegal proxy type `direct`; mihomo/clients would refuse to load the degraded config | The placeholder node is now a legal `socks5://127.0.0.1:1` (never connects but parses); `_uri_list` excludes the placeholder and the subscription still outputs `DEGRADED_SUBSCRIPTION_NOTICE` |
| R4-M2 | Medium | When vless/vmess/trojan lacked the `network` key (having only `ws-opts`/`grpc-opts`), the transport silently fell back to tcp and path/host fields were lost | New `_effective_network()`: explicit network wins, otherwise inferred from ws/grpc opts; all three builders use it |
| R4-M3 | Medium | Serial multi-URL fetching within one source (e.g. 8 discovered uploads) took ~11 minutes worst case | `_fetch_source` fetches within a source in parallel (≤ 4 concurrent) and returns the first non-empty result; discover / release-page auxiliary requests use a shortened timeout (12s, 2 retries) |
| R4-L1 | Low | `alpn: null` leaked as the literal `"None"` (vmess JSON / tuic URI) | `alpn_value()` returns an empty string for falsy values; tuic omits the parameter for empty alpn instead of emitting `alpn=` |
| R4-L2 | Low | `health_score`'s "stability" was a node-name hash wobble, unrelated to probe stability | Use real probe stability = passed rounds / `PROBE_TIMES` (historical nodes default to 1.0); a deterministic tie-break (~2e-10) only keeps exact ties stably ordered |
| R4-L3 | Low | `MAX_CANDIDATES` truncated in source order; a large source could crowd out other sources' nodes | New `sanitize_interleaved()` round-robins sources before truncation; `_sanitize_one()` extracts single-item sanitization |
| R4-L4 | Low | `_DISCOVER_CACHE` cached failed empty results too, so retries never happened within the same process | Cache only on success; the next call can retry |
| R4-L5 | Low | `ALLOW_LAN` compared a raw `os.getenv` against `"1"` without trimming/validation | New `_env_flag()` (1/true/yes/on, case- and whitespace-insensitive) unifies boolean env parsing |
| R4-L6 | Low | The cached mihomo binary had no version-refresh mechanism | Record an asset-url marker at download time; on cache hit compare freshness first and refresh new versions automatically; if the check fails, fall back to the cache (works offline) |
| R4-L7 | Low | Logs mixed Chinese and English | Subscription generation and region-distribution summaries are now English (consistent with the rest) |
| R4-L8 | Low | CI produced a diff commit every run because of the `generated-at` timestamp | `update.yml` sets `FREE_PROXY_AIRPORT_GENERATED_AT=ci`, committing only on substantive node changes |

## Tests

- 16 new regression tests (placeholder legality ×2, transport inference ×3, parallel fetch ×1, empty alpn ×3, real stability ×2, source interleaving ×1, discover cache ×2, mihomo cache fallback ×1, env flag ×1).
- `test_direct_fallback_when_empty` assertion updated (`direct` → `socks5` placeholder).
- Final state: **96/96 passed**; `ruff check` and `ruff format --check` clean.

---

# 20260823 Code Review Fixes (Round 3)

- Fixed on: 2026-08-23
- Basis: third review round (3 medium + 8 low)
- Verification: uv sync + 80/80 unit tests, `ruff check` green, `--validate-only` passed

## Fix list

| ID | Severity | Issue | Fix |
| --- | --- | --- | --- |
| R3-M1 | Medium | `_vless_to_uri` only read top-level `path`/`host`, ignoring Clash's `ws-opts.path` / `ws-opts.headers.Host`, so vless+ws subscription links were broken (path always `/`) | New shared `_ws_transport()` helper; vmess/vless read nested fields uniformly, with top-level fields still taking priority |
| R3-M2 | Medium | SS nodes' `plugin`/`plugin-opts` were silently dropped, making links with obfs / v2ray-plugin unusable | New `_ss_plugin_param()` emits `?plugin=` per SIP002; other plugins that cannot be expressed skip that node entirely (empty URI) |
| R3-M3 | Medium | hysteria/hysteria2 URIs only honored the `insecure` field, ignoring Clash's `skip-cert-verify` and the global `SKIP_CERT_VERIFY` switch, inconsistent with trojan/tuic behavior | Any of the three conditions emits `insecure=1` |
| R3-L1 | Low | vmess JSON serialized the alpn list with `str()` into a Python repr (`"['h2', 'http/1.1']"`) | Reuse the existing `alpn_value()` normalizer |
| R3-L2 | Low | The release-page asset regex had no uppercase letters, so assets with uppercase names were missed entirely | Add `re.IGNORECASE` to the regex |
| R3-L3 | Low | `.tar.gz` assets failed during extraction without trying the next candidate | `filter_mihomo_assets` excludes tar archives outright (the extractor only handles single-file .gz/.zip) |
| R3-L4 | Low | When the GitHub API returned valid assets but the tag was missing/invalid, the code failed outright without falling into the release-page fallback branch | The API branch validates the tag first (same regex as page scraping); an invalid tag takes the fallback |
| R3-L5 | Low | Engine downloads and gzip/zip extraction had no size limits; a malicious mirror could fill the disk or detonate a zip bomb | New `MAX_DOWNLOAD_BYTES` (256MB): streamed download counting, gzip decompression counting, and zip total `file_size` validation before extraction |
| R3-L6 | Low | Converting trojan over ws/grpc to a subscription lost the transport information | Non-tcp transports output `type=` plus `path/host/serviceName` (pure tcp links stay byte-identical) |
| R3-L7 | Low | `PROBE_PASS_MIN > PROBE_TIMES` silently killed everything (only a degraded fallback with no warning) | Print a `[WARN]` before benchmarking that 0 survivors are guaranteed |
| R3-L8 | Low | `fetch_text` did not explicitly close the streaming response on error paths | Use a context manager to guarantee closure |

## Tests

- 19 new regression tests (URI transport fields ×9, hy2 cert policy ×4, asset filtering and fallback ×4, download size cap, probe-config warning, response closure).
- Final state: **80/80 passed**; `ruff check generator.py tests/test_generator.py` clean.

---

# 20260822 Code Review Fixes (Round 2)

- Fixed on: 2026-08-22
- Basis: second review round (re-check after fixing the `collect_proxies` NameError; 8 new findings)
- Verification: uv sync + 61/61 unit tests, `ruff check` green, `--validate-only` passed

## Fix list

| ID | Issue | Fix |
| --- | --- | --- |
| R2-A | `collect_proxies` / `main()` main flow had zero test coverage (the NameError was missed) | New `MainFlowTest`: parallel fetch path, `main()` smoke (stubbed fetch/benchmark/write), benchmark batch fault tolerance |
| R2-B | `clean_sni` kept `:port` and whitespace, producing illegal SNI in subscriptions | Strip `host:port` (single-colon form only; IPv6 literals unaffected) and all whitespace |
| R2-C | trojan/tuic subscriptions hardcoded `allowInsecure=1`, contradicting the default keep-certificate-checks policy | Emit it only when the node has `skip-cert-verify` or the global `SKIP_CERT_VERIFY` is on; TLS security is no longer lowered by default |
| R2-D | IPv6 SSR produced non-standard subscriptions (`[addr]:port:...`) | `normalize_proxy` drops IPv6 SSR nodes outright (the SSR protocol defines no IPv6 host form) |
| R2-E | vmess/vless tls normalization was asymmetric | Comment that folding unknown tokens in vmess is intentional (vmess has no real-world Reality deployments); behavior unchanged |
| R2-F | A single node poisoned the entire 500-node benchmark config, degrading everything to old output | Benchmark in batches (`BENCHMARK_BATCH_SIZE=100`); a startup failure in one batch only drops that batch |
| R2-G | 24 lint noise items | Fixed 4 (import ordering, `Callable` moved to `collections.abc`, `datetime.UTC`, `endswith(tuple)`); `BLE001`/`S110` explicitly ignored in a new pyproject ruff config |
| R2-H | `PROBE_TIMES=0` silently killed everything (0 probe rounds → all nodes eliminated) | New `_probe_times()` / `_probe_pass_min()` clamp 0 and negatives to 1; README / AGENTS note this |

## Tests

- 8 new regression tests (clean_sni port/whitespace, SSR IPv6 drop, trojan/tuic allowInsecure policy, probe clamping, collect_proxies, main smoke, benchmark batch fault tolerance).
- Final state: **61/61 passed**; `ruff check generator.py tests/test_generator.py` clean.

# 20260822 Code Review Fixes

- Fixed on: 2026-08-22
- Basis: `D:\Work\workspace\Code Review.md` (full re-review from 2026-08-21)
- Verification: uv sync + 53/53 unit tests passed, compileall passed, line width compliant, workflow YAML parsing passed

## Fix list

### High priority

| ID | Issue | Fix |
| --- | --- | --- |
| F-H2 | URI builders did not bracket IPv6 servers, so IPv6 nodes passed benchmarking but produced illegal subscription links | New `_uri_host()`, used uniformly by all protocol builders |
| F-H1 | The Mihomo binary was downloaded from third-party mirrors without a checksum check | **Not fixed by project decision**: CI-only use; the magic-byte + minimum-size check is kept |

### Medium priority

| ID | Issue | Fix |
| --- | --- | --- |
| F-M1 | No argparse; any argument triggered the full side-effect flow | Add argparse; new `--validate-only [--config PATH]` offline validation entry point |
| F-M2 | `_probe_latency` JSON parsing exceptions violated the "return None on failure" contract | Move parsing into try/except, returning None and degrading silently |
| F-M3 | `find_free_port()` TOCTOU races caused the whole benchmark round to degrade | Add retries (default 3); raise an explicit error after exhaustion |
| F-M4 | Output files were not written atomically; a mid-write crash destroyed the degradation fallback basis | New `_atomic_write_text()` (temp file + `os.replace()`), used uniformly for all three output files |
| F-M5 | The `generated-at` timestamp produced a git diff on every run | Support `FREE_PROXY_AIRPORT_GENERATED_AT` override (CI can pin the timestamp to remove commit noise) |
| F-M6 | vless non-reality `flow` and vmess grpc serviceName were lost in conversion | Emit `flow` whenever present; add the vmess grpc-service-name mapping and tests |

### Low priority (F-L1 to F-L9)

| ID | Issue | Fix |
| --- | --- | --- |
| L1 | `allow-lan: true` default was too aggressive | Default to false; requires explicit `FREE_PROXY_AIRPORT_ALLOW_LAN=1` |
| L2 | Negative integer env vars caused wrong behavior | `_env_int` warns on negative values and falls back to the default |
| L3 | `health_score` comment described the old formula | Update the comment to match the implementation |
| L4 | Reusing historical output left `skip-cert-verify` inconsistent with the switch | Strip the field before reuse, then route through the unified injection logic |
| L5 | CI Python version `"3.x"` could drift | Pin to `"3.12"` |
| L6 | Source responses had no size limit | Streamed reads with an 8MB cap (`MAX_SOURCE_BYTES`) |
| L7 | Boolean ports passed validation, producing absurd nodes | `normalize_proxy` explicitly rejects boolean ports |
| L8 | Fetching 7 sources serially took too long | Parallel fetching with a thread pool (max 8 concurrent); sanitization/dedup stays on the main thread |
| L9 | The Python/CI YAML validation logic existed twice and could drift | Extract the `REQUIRED_RULES` constant; CI now directly calls `generator.py --validate-only --config docs/clash.yaml` |

## Documentation sync

- `AGENTS.md`: added the new env vars (ALLOW_LAN / GENERATED_AT), negative-integer fallback semantics, the Mihomo download verification policy and `--validate-only` usage.
- `README.md`: new "Local validation and environment variables" section (offline validation command + full env var table).

## Tests

- 10 new regression tests covering the fixes above (IPv6 URI, vless flow, vmess grpc, bad-JSON probing, negative env, boolean port, atomic writes, etc.).
- Final state: **53/53 passed**.

---

# 20260820 Code Review

- Reviewed on: 2026-08-20
- Method: dual-axis (Standards / Spec) parallel sub-agents plus local reproduction verification

## 1. Review scope

The repository has a single code commit `dfacd4a` ("Init repository."); the 50 commits after it are all "Update subscriptions" auto-generated by GitHub Actions (modifying only the `docs/` / `output/` artifacts). This review therefore covers the entire codebase introduced by `dfacd4a` (identical to HEAD's non-artifact files):

| File | Description |
| --- | --- |
| `generator.py` | The only main script (v7, ~1500 lines) |
| `tests/test_generator.py` | Unit tests (40) |
| `pyproject.toml` / `uv.lock` | Project metadata and locked dependencies |
| `.github/workflows/update.yml` | CI/CD (generate + validate + publish) |
| `.github/workflows/pages.yml` | GitHub Pages deployment |
| `AGENTS.md` / `README.md` | Project guide / user docs |

- Spec baseline: the repository has no issue tracker; `AGENTS.md` + `README.md` + `update.yml` validation logic serve as the requirements baseline.
- Standards baseline: `AGENTS.md` (modification guidelines, code style) + the Fowler code-smell baseline (all judgment calls; documented conventions take priority).

## 2. Runtime environment verification (uv)

```text
uv sync                                             # OK (Python 3.14.7, 7 dependencies)
uv run python -m unittest discover -s tests         # 40/40 passed
uv run python -m py_compile generator.py tests/test_generator.py  # OK
```

The key conclusion was reproduced locally: `generate_shadowrocket_sub([{"type": "direct"}]) == ""` (see S1 / P2).

## 3. Standards-axis findings

### S1 (hard violation) DIRECT-FALLBACK degradation path produces empty subscription files

When `main()` (original generator.py L1486-1494) hit DIRECT-FALLBACK with no live nodes and no historical output, `proxy_to_uri()` returned `""` for `type="direct"` and `generate_shadowrocket_sub()` (L1441-1449) produced an empty string from `base64.b64encode(b"")` on the empty list. `output/rocket.txt` and `output/v2ray.txt` were therefore written as **empty files**, violating "output files must never be empty or incomplete" and "no crash when there are no nodes"; in addition, update.yml's "Validate Shadowrocket/V2Ray subscriptions" (`st_size==0` exits) necessarily failed in this fallback scenario — the degradation contract was broken at the CI layer.

### S2 (judgment) Mysterious Name: `all_names` / `all_nodes` naming is inverted

L1021's `all_names = _top_names(metrics, AUTO_FAST_MAX)` is actually the AUTO-FAST curated subset, while L1022's `all_nodes` is the full set — names are the opposite of their meaning.

### S3 (judgment) Duplicated Code: vmess / vless tls normalization is nearly isomorphic

The tls boolean/string normalization in `_vmess_to_uri` (L1280-1287) and `_vless_to_uri` (L1330-1341) is duplicated (only empty-value token and unknown-string handling differ); a shared helper should be extracted.

### S4 (judgment) Duplicated Code: SNI parameter concatenation is repeated

The pattern `sni = clean_sni(...); if sni: params.append(f"sni={quote(...)}")` repeats 4 times across trojan (L1379-1381), hysteria (L1404-1406), tuic (L1430-1432) and vless.

### S5 (judgment) Repeated Switches / Shotgun Surgery: proxy-type classification is scattered across 4 places

Proxy-type classification is scattered across `SUPPORTED_PROXY_TYPES` (L125), `REQUIRED_FIELDS` (L142), `proxy_to_uri`'s if/elif (L1216-1234) and `_maybe_inject_skip_cert_verify`'s TLS type set (L757). Adding a type requires changing 4+ places; the TLS set literal is also duplicated in tests (L296).

### S6 (judgment) Data Clumps: every `_*_to_uri` destructures the same (server, port, name) at the start

Each converter destructures the same triplet at its start.

### S7 (judgment) the five url-test group dicts are isomorphic

The five url-test group dicts at L1041-1080 (AUTO-FAST / HK / JP / US / AI) share an identical structure and could be data-driven.

### S8 (judgment) test typo `casm`

The variable `casm` at tests/test_generator.py L110 should be `cases`.

### S9 (judgment) the test and the source duplicate the TLS type literal

tests/test_generator.py L296 and generator.py L757 repeat the same TLS type set — same-source duplication.

### S10 (judgment) CI installs dependencies with pip instead of the lock file

update.yml L28 used `pip install --upgrade requests pyyaml`, inconsistent with the `uv sync` + uv.lock approach recommended by AGENTS.md / README; CI dependencies could drift.

### S11 (judgment) orphan empty package `src/free_proxy_airport`

`src/free_proxy_airport/__init__.py` is an empty package with no references, mentioned nowhere in the README / AGENTS directory layouts — scaffold leftover.

## 4. Spec-axis findings

### P1 (severe) CI validation runs after commit+push and cannot "prevent committing invalid subscriptions"

- Spec: AGENTS.md L52 "...so committing invalid subscriptions is prevented"; L140 "CI validation (update.yml) independently checks again..." express admission semantics.
- Code: in update.yml, "Generate Clash subscription" (L30-49) already contained `git commit`/`git push` (L48), while "Validate Clash Verge subscription" (L51) and "Validate Shadowrocket/V2Ray subscriptions" (L119) came after. If type/base64/HTTP-200 checks failed, the bad commit was already pushed and pages.yml would publish it to Pages. **Validation must move before the commit.**

### P2 (severe) the DIRECT-FALLBACK degradation path writes an empty base64 subscription

- Spec: AGENTS.md L52 "output files must never be empty or incomplete" and the three-level degradation guarantee.
- Code: generator.py `main()` L1487-1490 — degradation contains only DIRECT-FALLBACK, `proxy_to_uri` does not support `direct`, and `generate_shadowrocket_sub` yields an empty string; `validate_config()` (L1118) only validates the Clash config, and update.yml's non-empty/base64 checks run after the push. In the worst degradation, rocket.txt / v2ray.txt were necessarily empty and pushed.

### P3 (medium) `validate_config()` does not validate group types

- Spec: AGENTS.md L145 "always update REQUIRED_GROUPS, validate_config() and update.yml's validation logic (group types, required rules, byte consistency) together, keeping the three consistent".
- Code: generator.py L1118's `validate_config` only checks group existence, non-emptiness and rules; types (url-test / select / fallback) were only checked in update.yml (L57-69) after the commit.

### P4 (medium) `FREE_PROXY_AIRPORT_MAX_LATENCY_MS=0` semantics not implemented

- Spec: AGENTS.md L124 "(0 = use `LATENCY_TIMEOUT_MS`)".
- Code: L60 directly used `_env_int(..., 2000)` without handling 0; at L873 `latency > MAX_LATENCY_PASS_MS` was always true when 0 → every node failed probing → silent degradation instead of switching to 5000ms.

### P5 (minor) the "median" implementation deviates

- Spec: AGENTS.md L90/L103 "use the median of passed-round latencies as the representative value".
- Code: L908's `rounds[len(rounds) // 2]` took the larger of two values (upper median) when exactly 2 rounds passed, not the standard median (even counts should average the two middle values).

### P6 (minor) the cmp byte-check scope does not match the docs

- Spec: AGENTS.md L140 claims `cmp` runs on all three docs files.
- Code: update.yml only ran `cmp` on clash.yaml (L53); rocket/v2ray only got existence/non-empty/base64 checks. (`cp` happens right after generation so byte-identity holds naturally; low impact.)

### Spec (a) missing items / (b) out-of-scope behavior

- Missing: none found — 7 sources + discover, benchmarking/scoring/grouping/env-var defaults, REQUIRED_GROUPS and the three output paths are all implemented.
- Out-of-scope: no significant findings; mihomo mirror fallback, magic-byte checks and zip-slip protection were already documented in AGENTS.md.

## 5. Compliance

Comments/docstrings are all English, no lines exceed 99 chars, no tabs, and PEP 8 indentation/continuation is good; the `validate_config`/REQUIRED_GROUPS/update.yml trio is in sync (except for the missing type validation, see P3).

## 6. Fix status

| ID | Issue | Status | Fix |
| --- | --- | --- | --- |
| S1 / P2 | DIRECT-FALLBACK empty subscription | Fixed | New `shadowrocket_subscription_content()`: when no node is URI-expressible it writes a base64-encoded `DEGRADED_SUBSCRIPTION_NOTICE`, so subscription files are never empty and stay legal base64; `main()` uses it with distinct WARN/OK logs |
| P1 | CI validation ordering | Fixed | `update.yml` moves both Validate steps before `Commit output`; a failed validation neither commits nor pushes |
| P3 | validate_config type validation | Fixed | New `REQUIRED_GROUP_TYPES`; `validate_config()` validates each required group's type, consistent with update.yml |
| P4 | MAX_LATENCY_MS=0 | Fixed | New `_max_latency_pass_ms()`: env=0 falls back to `LATENCY_TIMEOUT_MS` (5000ms), matching AGENTS.md |
| P5 | Median | Fixed | `test_single_proxy` averages the two middle values for even rounds (e.g. [100,200] → 150) |
| P6 | cmp scope | Fixed | `update.yml` also runs `cmp --silent output/... docs/...` for rocket.txt / v2ray.txt |
| S2 | all_names naming | Fixed | Renamed to `auto_fast_names`, matching its content (AUTO-FAST curated subset) |
| S3 | tls normalization duplication | Fixed | Extracted `_normalize_tls(value, default, pass_through)` shared by vmess/vless |
| S4 | sni concatenation duplication | Fixed | Extracted `_append_sni_param()` shared across vless/trojan/hysteria/tuic |
| S5 | Scattered type classification | Fixed (partial) | TLS set extracted as `TLS_PROXY_TYPES` (tests reference it too); `proxy_to_uri`'s if/elif replaced by the `_URI_BUILDERS` dispatch table; adding a protocol still requires maintaining 3 data sites (SUPPORTED_PROXY_TYPES / REQUIRED_FIELDS / _URI_BUILDERS) but no more logic changes |
| S6 | Data Clumps | Kept (judgment) | The (server, port, name) extraction at the start of each `_*_to_uri` is already gathered via `ProxyMetric`/the proxy dict and the remaining fields (uuid/password/cipher…) differ per builder; a unified type would only add indirection; status quo kept |
| S7 | Isomorphic url-test groups | Fixed | `build_config` generates the 5 url-test groups data-driven via `url_test_groups` |
| S8 | casm typo | Fixed | Renamed to `cases` |
| S9 | TLS literal duplication | Fixed | Tests use `gen.TLS_PROXY_TYPES` |
| S10 | CI pip→uv | Fixed | `update.yml`: `pip install uv` + `uv sync --frozen`; Python invocations uniformly use `uv run python`, consistent with AGENTS.md/uv.lock |
| S11 | Orphan empty package | Fixed | Deleted `src/free_proxy_airport/__init__.py` (pyproject uses `py-modules=["generator"]`; no references) |

## 7. Fix verification (diff baseline vs post-change)

| Verification item | Before | After |
| --- | --- | --- |
| Unit tests | 40/40 | **45/45** (5 new: even-round median, group type validation, non-empty degraded subscription, subscription fidelity, MAX_LATENCY=0) |
| `py_compile` | OK | OK |
| Workflow YAML parsing | — | OK (step order: Generate → Validate Clash → Validate Rocket/V2Ray → Commit) |
| Degraded subscription content | `""` (empty file) | 132-char legal base64, decoding to `# DIRECT-FALLBACK: no live nodes available; ...` |
| `validate_config` type error | Not intercepted | Raises `RuntimeError: ... has type ...; expected ...` |
| Line width ≤ 99 | OK | OK (no overlong lines in generator.py / tests) |