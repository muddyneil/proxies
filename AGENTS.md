# AGENTS.md — Free Proxy Airport v7

Project guide for AI coding assistants. Written based on an analysis of the current `generator.py` source; describes the architecture, key constraints, commands and modification rules.

## Project overview

An airport-grade free Clash / Mihomo node subscription generator. Core workflow (triggered by GitHub Actions every half hour): aggregate public free node sources → deduplicate and sanitize → benchmark surviving nodes through a real Mihomo instance with multiple URLs and rounds (keeping only stable, low-latency nodes) → sort by health score, group and truncate → generate `output/clash.yaml` → copy to `docs/clash.yaml` for GitHub Pages subscription distribution.

Version: `v7` (see the `VERSION` constant at the top of `generator.py`).

## Directory layout

- `generator.py` — the only main script; implements all analysis, benchmarking and generation logic
- `pyproject.toml` / `uv.lock` — project metadata and dependency lock (`requests>=2.34.2`, `pyyaml>=6.0.3`, Python >= `3.12`)
- `output/clash.yaml` — generated Clash config (final artifact)
- `docs/clash.yaml` — copy distributed via GitHub Pages (byte-identical to output)
- `docs/.nojekyll` — lets Pages serve YAML / txt
- `tests/test_generator.py` — unit tests for pure functions (sanitization/dedup, region detection, scoring, config validation, etc.) (`python -m unittest discover -s tests`)
- `.github/workflows/ci.yml` — runs unit tests, Ruff and format checks on PRs / pushes to `main`
- `.github/workflows/update.yml` — generates, validates and publishes the subscription every half hour
- `README.md` — end-user usage instructions
- `AGENTS.md` — this file
- `.venv/` — local virtual environment created by uv (git-ignored)

## Entry point and execution

```bash
# Run locally (Python 3.12+, requires requests / pyyaml); `generator.py --validate-only [--config PATH]` only validates a config without touching the network
python generator.py

# Create a virtual environment with uv and run
uv run --with requests --with pyyaml python generator.py
# Or sync the environment from pyproject.toml/uv.lock first, then run
uv sync && uv run python generator.py
```

When run as the main module: exceptions from `main()` are caught, printed as `[ERROR]` to stderr, then re-raised. The entry point forces stdout/stderr to UTF-8 so non-ASCII output stays compatible.

`main()` flow:
1. `collect_proxies()` fetches and sanitizes candidate nodes from all sources
2. `benchmark_proxies(candidates)` benchmarks with real Mihomo (degrades if it fails)
3. `publishable_metrics()` keeps only high-quality nodes with latency ≤ 800ms, 3/3 rounds passed and jitter ≤ 300ms
4. If there are no high-quality nodes → ignore the previous output that lacks live quality evidence and use DIRECT-FALLBACK (Actions fail immediately because `REQUIRE_LIVE=1`)
5. Still no nodes → use the DIRECT-FALLBACK degraded config
6. `build_config` → `validate_config` → `write_config` → `print_summary`; generates only the Clash subscription and prints a node region-distribution summary

### Output files and degradation guarantees

- The final artifact has the fixed path `output/clash.yaml` (module constant `OUTPUT_PATH`); Actions copies it to `docs/clash.yaml` before publishing.
- **The script never crashes just because no usable nodes exist**: when live benchmarking fails or the quality gate is not met, Actions fails immediately to keep the previous Pages version; local runs produce an explicit DIRECT-FALLBACK and never publish historical nodes that lack live verification.
- **The output file must never be empty or incomplete**: `validate_config()` guarantees at least one proxy entry, raises on a missing required group, and raises on a missing AI routing / key rule, preventing invalid subscriptions from being committed.
- In the extreme degraded case (no live nodes and no historical output), `clash.yaml` is written with a `DIRECT-FALLBACK` placeholder node (`socks5://127.0.0.1:1`, a **legal** Clash proxy entry that simply never connects; the `direct` pseudo-type would be rejected by mihomo), keeping local output always parseable.

## Node sources (SOURCE_GROUPS)

3 sources are currently enabled; each source has a `name`, a `primary` URL and `fallbacks`. All URLs start concurrently; the primary result is preferred when it returns a non-empty result within the 3-second priority window, otherwise the first fallback that succeeds is used. The remaining 4 low-yield or unusable sources are temporarily disabled; their URLs and reasons are kept in the `collect_proxies()` docstring.

| Source | primary |
| --- | --- |
| snakem982 proxypool | `.../snakem982/proxypool/main/clash.yaml` (fallbacks: source/clash-meta*.yaml) |
| PuddinCat BestClash | `.../PuddinCat/BestClash/refs/heads/main/proxies.yaml` |
| zhuhaiuk free-nodes | `.../zhuhaiuk/free-nodes/main/clash_config.yaml` |

The special fallback marker `discover:free-clash-v2ray` dynamically resolves the GitHub Pages README, extracts `https://free-clash-v2ray\.github\.io/uploads/\d{4}/\d{2}/[0-9]-\d{8}\.yaml` with a regex, and takes the first 8.

Supported proxy types (`SUPPORTED_PROXY_TYPES`): `ss, ssr, vmess, vless, trojan, hysteria, hysteria2/hy2, tuic, socks5, http`. Types not in the list are dropped outright.

## Data flow

### Fetching (fetch_text)
- Custom `User-Agent` (`free-proxy-airport/v7 (+https://github.com/)`) and Accept headers
- `SOURCE_TIMEOUT=25` second timeout, at most `MAX_RETRIES=3` attempts with `2*attempt` second backoff; deterministic errors such as 4xx and exceeding the size limit are not retried — only network errors / 5xx are
- **Parallel per-source URL fetching** (`_fetch_source`, max 4 concurrent): all URLs start simultaneously; the primary is preferred when it succeeds within the 3-second window; once a result is chosen, a cancel event stops the other URLs from continuing to retry
- Auxiliary requests such as discover-page and expanded_assets fetches use a shortened timeout (12s, 2 retries); the `/releases/latest` redirect resolution is a single request using the default `SOURCE_TIMEOUT` (25s); discover results are cached only on success and retried on the next call after a failure

### Parsing (extract_proxies family)
- Content may be base64-wrapped: `maybe_base64_decode` decodes under strict conditions (compact with no whitespace, length divisible by 4, pure base64 charset, and the decoded output contains `proxies:` or `://`).
- `yaml.safe_load` parses the whole document first; on failure or when there are no proxies, fall back to `extract_proxy_block` for manual extraction of the `proxies:` block.

### Sanitization and deduplication (sanitize_interleaved)
1. `normalize_proxy`: filters None values, normalizes types (`hy2`→`hysteria2`), validates server and port (1–65535); invalid entries are dropped outright.
2. `proxy_fingerprint`: SHA-256 fingerprint of the full normalized config excluding `name`; only configs that are exactly identical are merged, so usable variants such as IPv4/IPv6 or CA are not wrongly removed.
3. Duplicate names are made unique with a `-N` suffix.
4. **Multi-source candidates are interleaved by source** (round-robin merge in `sanitize_interleaved`) before the `MAX_CANDIDATES` truncation, so one huge source cannot crowd out the tail nodes of the other sources.
5. **Per-source structural-failure degradation**: `collect_proxies` submits/collects each source independently; if a source hits a structural error (e.g. a missing `primary` key) only that source is dropped with a `[WARN]` without aborting the whole collection; results are still emitted in `SOURCE_GROUPS` order, keeping the interleaved output deterministic.
6. **Untrusted-field containment**: `normalize_proxy` keeps only the proxy-entry fields in `ALLOWED_PROXY_FIELDS`, and a sanitized per-node JSON must not exceed `MAX_PROXY_BYTES` (64 KiB); YAML parsing also bounds alias count, nesting depth and node-structure expansion, and nested mappings accept string keys only; unknown top-level keys, cyclic structures and oversized nodes are dropped before they reach Mihomo.

## Benchmarking and scoring (core mechanism)

- **Real benchmarking**: `benchmark_proxies` downloads/installs `mihomo` automatically (prefers an existing system `mihomo`/`clash-meta`/`clash`, otherwise selects the asset matching OS+architecture from GitHub releases and caches it in a temp directory). A temporary benchmark config is generated and latency is tested concurrently through the external controller REST API; bad nodes are recursively isolated by bisection only when Mihomo explicitly rejects the config before the controller is ready; systemic failures such as an unavailable engine or controller timeout abort the current round of benchmarking to avoid repeatedly starting the engine.
- **Survival and publication probing (multiple URLs × multiple rounds)**: `test_single_proxy` benchmarks each node against `TEST_URLS` (gstatic + cloudflare + android connectivity checks) one by one; **every URL** must return a valid latency within a round. A node must pass all `PROBE_TIMES` rounds; any failed round stops the node immediately; the representative latency is the median across rounds, and a round latency above `MAX_LATENCY_PASS_MS` (default 2000) fails the node.
- **Hard publication quality gate**: `publishable_metrics` requires at least 3 configured rounds, representative latency ≤ 800ms, all rounds passed and ≤ 300ms jitter between rounds (max minus min).
- **mihomo asset selection**: prefers the GitHub Release API, and candidate assets must carry a valid `digest: sha256:...`; when the API is unavailable the HTML fallback is used for discovery only, and a missing official digest fails closed without executing that asset. The `compatible` variant is preferred and assets are sorted by score; the chosen URL is probed with HEAD first, then with Range on failure.
- **Binary verification**: the downloaded archive must match the official Release API SHA-256 byte for byte, then size, compression format and the decompressed binary's magic bytes (MZ / ELF / Mach-O) are checked. Third-party mirrors are disabled by default; set `FREE_PROXY_AIRPORT_DISABLE_MIRRORS=0` explicitly to enable them, and mirrored content must still pass the same official SHA-256. Extraction uses a one-shot directory.
- **Cache refresh**: a new version is first downloaded, digest-verified and extracted in a unique temp directory inside the cache directory, then atomically replaces the binary; a failed refresh keeps the previous verified engine. Cache markers are also written atomically.
- Temp directories, the controller URL and the mixed port all use random free ports; the two listener ports are forced to differ, and explicit address-in-use errors trigger limited retries with fresh ports.
- Results are recorded via `ProxyMetric` (proxy/latency/region/health score/passed rounds/jitter).

### Health score (health_score)
```
0.6 * (1000.0 / latency) + 0.1 * stability
```
- Latency is in milliseconds; lower is a higher score; `stability` is passed rounds / `PROBE_TIMES`.
- Region names come from untrusted upstreams and are used only for HK/JP/US grouping; they do not affect quality scoring or publication ordering.
- A deterministic ≤ 1e-9 per-name hash tie-break is added, used only to keep order stable across runs on exact ties.

### Region detection (detect_region)
Matches English keywords (`\bhk\b`, `japan`, `united states`, etc.) / Chinese (香港, 日本, 美国/美國, 新加坡) / flag emoji in the node name to identify HK, JP, US, SG, otherwise `OTHER`.

### Grouping logic (build_config)
- All high-quality nodes are sorted by `(latency, jitter_ms, -health_score)`; the `proxies` list keeps only live nodes that meet the hard publication gate, with optional `skip-cert-verify` injection.
- `AUTO-FAST`: the curated pool of the top `AUTO_FAST_MAX` (default 50) nodes in quality order (url-test, `lazy`); keeps client load low when nodes are numerous
- `ALL`: **all published high-quality nodes** (select, no periodic probing); any published node can be selected manually and AUTO-FAST is a subset of it
- `HK-POOL` / `JP-POOL` / `US-POOL`: nodes of the corresponding region truncated to `REGION_POOL_MAX` (default 20); when a region has no nodes, fall back to the first 5 healthy nodes (`names_for_region`)
- `AI-POOL`: `low_latency_pool` takes the lowest-latency batch ordered by (latency, -health_score) (`min(max(3, len//5), 30)`)
- `FALLBACK` (fallback type): chains AUTO-FAST → HK-POOL → JP-POOL → US-POOL
- `PROXY` (select): AUTO-FAST → FALLBACK → ALL
- Rules: AI domains go to AI-POOL, `GEOIP,CN,DIRECT`, catch-all `MATCH,PROXY`
- Top-level fields: mixed-port 7890, allow-lan, ipv6, unified-delay, tcp-concurrent, generated-by/generated-at. Valid per-node `client-fingerprint` values are kept as-is; the top-level `global-client-fingerprint` that Mihomo removed is no longer generated.

## Runtime configuration (environment variables)

- `FREE_PROXY_AIRPORT_MAX_WORKERS` (default 24) — concurrent benchmark threads
- `FREE_PROXY_AIRPORT_MAX_CANDIDATES` (default 500) — maximum candidate node limit (0 = unlimited); sources are interleaved-merged before truncation
- `FREE_PROXY_AIRPORT_MAX_LATENCY_MS` (default 2000) — initial survival delay cap (0 = use `LATENCY_TIMEOUT_MS`); final publication always requires ≤ 800ms
- `FREE_PROXY_AIRPORT_PROBE_TIMES` (default 3, range 1–10) — probe rounds; below 3 rounds benchmarking that cannot meet the publication gate is skipped, and nodes must pass all rounds
- `FREE_PROXY_AIRPORT_AUTO_FAST_MAX` (default 50) — maximum AUTO-FAST curated pool size (0 = all live nodes)
- `FREE_PROXY_AIRPORT_REGION_POOL_MAX` (default 20) — maximum nodes per region pool (0 = all)
- `FREE_PROXY_AIRPORT_SKIP_CERT_VERIFY` (default 0) — globally injects `skip-cert-verify` into vmess/vless/trojan/hysteria/hy2/tuic. Upstream-provided `skip-cert-verify` / `insecure` are always stripped during sanitization; certificate-verification policy is decided only by this switch. **Security-sensitive, off by default**; see "Modification guidelines".
- `FREE_PROXY_AIRPORT_ALLOW_LAN` (default 0) — `allow-lan` switch of the generated config; only `1` (`true`/`yes`/`on`, case- and whitespace-insensitive) allows LAN proxy sharing.
- `FREE_PROXY_AIRPORT_DISABLE_MIRRORS` (default 1) — third-party mirrors fully disabled by default; only `0` allows trying mirrors after direct download fails, and mirrored archives must still match the official GitHub Release API SHA-256.
- `FREE_PROXY_AIRPORT_REQUIRE_LIVE` (default 0) — fail immediately when no live node meets the publication quality gate this round, without reusing historical output; GitHub Actions always sets 1.
- `FREE_PROXY_AIRPORT_GENERATED_AT` — overrides the `generated-at` timestamp in the output.
- Negative integer environment variables are treated as invalid and fall back to defaults; in Actions the Mihomo Release API requests use the read-only `github.token` to avoid anonymous rate limits, and GitHub/OIDC credential environment variables are stripped before any external Mihomo process is started.
- Invalid integer or boolean values print a `[WARN]` and fall back to defaults, avoiding script crashes and accidental policy relaxation from typos in security-sensitive boolean switches.

## Required output constraints (must be preserved when changing code)

`REQUIRED_GROUPS` / `REQUIRED_GROUP_TYPES` together with `validate_config()` enforce that the groups below exist with the correct types (AUTO-FAST/HK-POOL/JP-POOL/US-POOL/AI-POOL are `url-test`, ALL/PROXY are `select`, FALLBACK is `fallback`), that every node/group name referenced by a group really exists (mihomo's built-in `DIRECT` counts as a legal member), and that `rules` contains the AI routing and catch-all rules — otherwise an error is raised:
- `AUTO-FAST`, `HK-POOL`, `JP-POOL`, `US-POOL`, `AI-POOL` (url-test)
- `ALL` (select, all live nodes)
- `FALLBACK` (fallback, chains AUTO-FAST + region pools)
- `PROXY` (select, AUTO-FAST → FALLBACK → ALL)
- Rules: 4 AI domains + `GEOIP,CN,DIRECT` + `MATCH,PROXY`

CI (`ci.yml`) runs unit tests, Ruff and format checks on PRs and pushes to `main`. The read-only `generate` job requires at least one live node meeting the publication quality gate and a real UTC `generated-at` within 1 hour, runs the project `validate_config`, a real `mihomo -t` and a deployment-file whitelist check, then uploads the Pages artifact.

**Pages publishing**: the automatic flow never commits generated files to `main`. Only the `deploy-pages` job has `pages: write` / `id-token: write`; it deploys the same validated artifact of the round and downloads `clash.yaml` from the real Pages URL to check it against the SHA-256 emitted by the `generate` job. On automatic generation failure the previous Pages version is kept; there is no recovery workflow that publishes the in-repo baseline samples.

## Modification guidelines

- Prefer changing only `generator.py`: the script is self-contained and CI already handles fetching, validation and publishing.
- When adding or modifying `SOURCE_GROUPS` / groups / rules, always update `REQUIRED_GROUPS`, `REQUIRED_GROUP_TYPES`, `validate_config()` and the validation logic in `update.yml` (group types, required rules, byte consistency) together, keeping all three consistent.
- If benchmark logic changes, keep the "no crash with no nodes, non-empty output" degradation guarantee.
- Change output paths carefully: `OUTPUT_PATH`, the `docs/clash.yaml` copy, CI validation and README references must all be synced.
- **Code style**: keep source comments and docstrings in English; follow PEP 8 (4-space indentation, ≤ 99 line width, aligned implicit continuations, etc.). When adding/changing dependencies, sync `pyproject.toml` and verify locally with `uv sync`.
- Comply with local laws, regulations and relevant terms of service (this project is an educational / personal aggregation experiment).