# Proxies

Derived from https://github.com/sunmiao4458/free-proxy-airport

## Capabilities

GitHub Actions aggregates public free node sources automatically every half hour, runs real latency tests (multiple URLs, multiple sample rounds), removes nodes that time out, jitter or are unstable, and generates automatic groups ordered by latency. Final publication requires node latency ≤ 800ms, all 3/3 rounds passed and inter-round jitter ≤ 300ms.

- Multi-URL, three-round real latency testing
- Hard latency, success-rate and jitter gates enforced before publication
- Ranked purely by latency and stability; upstream node names do not affect quality ranking
- Automatic HK / JP / US / AI grouping
- Automatic FALLBACK degradation
- Smart routing for OpenAI / ChatGPT / Claude / Anthropic

## Automatic updates

Workflow name: `AI Self-Healing Proxy v7`

Clash subscription:

https://muddyneil.github.io/proxies/clash.yaml

Output files:

```text
output/clash.yaml
docs/clash.yaml
```

## Why automatic updates may fail

`schedule`-based triggering is a best-effort mechanism in GitHub Actions: the official documentation explicitly states that scheduled workflows may be delayed or skipped and provides no SLA. In practice, this project (two slots per hour, UTC :13 / :43) once saw an extreme case in which only 1 of 18 slots fired over about 9 hours during peak load — this is not a configuration error, and editing the cron expression cannot fix it. Possible failure causes and mitigations for each stage are listed below.

| Stage | Cause | Symptom | Mitigation |
|------|------|------|------|
| Schedule trigger (most common) | GitHub's scheduler delays or drops `schedule` events | No new `schedule` entry appears in the Actions run list for a long time | Trigger manually (see the self-check commands below); if exact timing is truly required, switch to an external cron service calling the `workflow_dispatch` API |
| Schedule not registered | Workflow is not on the default branch; or cron was just changed and registration can take up to a few minutes | A long period of zero triggers after a change | Confirm the workflow is on `main`; run `git push` once to refresh registration |
| No live nodes this round | Actions always sets `FREE_PROXY_AIRPORT_REQUIRE_LIVE=1`, and all candidates fail the publication gate (latency ≤ 800ms, 3/3 rounds passed, jitter ≤ 300ms) | The generate job fails and the previous Pages version is kept | This is intended protection; wait for the next round or retry manually |
| All upstream node sources down | All 3 currently enabled public sources fail to fetch (network/upstream offline) | No candidate nodes; same result as above | Run `uv run python generator.py` locally to see the exact errors |
| Mihomo unavailable | GitHub Release API unavailable, no asset matching the architecture, official SHA-256 verification failed (third-party mirrors disabled by default) | The benchmark engine fails to start; this round aborts or degrades | Wait for GitHub to recover; for local experiments you can set `FREE_PROXY_AIRPORT_DISABLE_MIRRORS=0` |
| Config validation failed | Any of `validate_config`, real `mihomo -t`, the generated-at freshness check or the deployment-file whitelist fails | The corresponding step errors and fails | Open the failing step's logs to investigate |
| Run timeout | generate capped at 30 minutes, deploy capped at 15 minutes | The run is terminated | Inspect the logs to locate the slow step |
| Publish failed | `deploy-pages` failed, or the clash.yaml downloaded live does not match the artifact SHA-256 | Pages keeps the previous version | Re-run once; confirm Pages Source is "GitHub Actions" |

Self-check toolkit (requires the gh CLI):

```bash
gh run list --workflow update.yml   # recent trigger records; note whether the event is schedule or workflow_dispatch
gh run view <run-id>                # step results and failure logs of a run
gh workflow run update.yml          # manual re-run (same as the Run workflow button on the repo page)
```

Windows users can also run `run-update.ps1` at the repository root for a one-click "trigger → wait → view summary" flow.

## Local validation and environment variables

The script is primarily meant for CI, but it supports offline validation of an already-generated config:

```bash
uv sync
uv run python generator.py --validate-only --config docs/clash.yaml
```

All environment variables below are optional.

| Variable | Default | Description |
|------|------|------|
| `FREE_PROXY_AIRPORT_MAX_WORKERS` | 24 | Concurrent benchmark threads |
| `FREE_PROXY_AIRPORT_MAX_CANDIDATES` | 500 | Maximum candidate node count (0 = unlimited) |
| `FREE_PROXY_AIRPORT_MAX_LATENCY_MS` | 2000 | Initial survival delay cap (0 = use the 5000ms timeout); final publication always requires ≤ 800ms |
| `FREE_PROXY_AIRPORT_PROBE_TIMES` | 3 | Probe rounds (1–10); below 3 rounds the publication gate cannot be met and benchmarking is skipped; nodes must pass every round with jitter ≤ 300ms |
| `FREE_PROXY_AIRPORT_AUTO_FAST_MAX` | 50 | AUTO-FAST pool cap (0 = all) |
| `FREE_PROXY_AIRPORT_REGION_POOL_MAX` | 20 | Region pool cap (0 = all) |
| `FREE_PROXY_AIRPORT_SKIP_CERT_VERIFY` | 0 | Inject skip-cert-verify into TLS-class nodes (upstream-provided values of this field and `insecure` are stripped during sanitization; security-sensitive, off by default) |
| `FREE_PROXY_AIRPORT_ALLOW_LAN` | 0 | allow-lan switch of the generated config (set to 1 to allow LAN sharing) |
| `FREE_PROXY_AIRPORT_DISABLE_MIRRORS` | 1 | Third-party Mihomo download mirrors disabled by default; set 0 to enable, but downloads must still match the GitHub Release API SHA-256 |
| `FREE_PROXY_AIRPORT_REQUIRE_LIVE` | 0 | Require at least one live node meeting the publication quality gate this round; GitHub Actions always sets 1 to avoid publishing historical or low-quality degraded results |
| `FREE_PROXY_AIRPORT_GENERATED_AT` | Current time | Override the generated-at timestamp |

Boolean variables only accept `1/true/yes/on` or `0/false/no/off` (case and surrounding whitespace are ignored); invalid values print a warning and fall back to the default, so security-sensitive switches are never accidentally relaxed by a typo.

## Deployment steps

### 1. Push the code to GitHub

```bash
git push origin main
```

### 2. Run GitHub Actions

- Open the **Actions** tab of the repository and select the `AI Self-Healing Proxy v7` workflow
- For the first deployment, click **Run workflow** to trigger it once manually; afterwards it runs automatically at minute 13 and 43 of every hour UTC (GitHub Actions scheduled tasks are not guaranteed to be on time or to fire at all)
- The `CI` workflow runs tests, Ruff and format checks on PRs and pushes to `main`
- The `AI Self-Healing Proxy v7` workflow performs live generation, project validation, real Mihomo config validation, Pages publishing and live content hash verification
- Automatic runs publish the temporary Pages artifact directly and no longer write `Update subscriptions` commits to `main`; the repo's `output/` / `docs/` are only in-version baseline samples

### 3. Enable GitHub Pages (GitHub Actions mode)

1. Repository **Settings** → **Pages**
2. Under **Build and deployment**, set Source to **"GitHub Actions"**
3. `AI Self-Healing Proxy v7` (`update.yml`) publishes the validated `docs/` artifact of the current round directly after every successful run; the generate job has read-only repo permissions only, and Pages/OIDC permissions are granted to the deploy job only

> You must use "GitHub Actions" mode rather than "Deploy from a branch", otherwise branch content could bypass `update.yml`'s live quality gate and publishing validation.

### 4. Verify the subscription URL

Open it in a browser; it should display/download the YAML content directly.

### 5. Import the subscription in Clash Verge

> Port note: `mixed-port` is the local proxy listening port. Clash Verge uses the port from its own settings (default 7897) and overrides the subscription value, so there is no need to change `mixed-port` in the subscription.

## FAQ

| Issue | Cause / Fix |
|------|-----------|
| Actions run failed | First narrow it down with the "Why automatic updates may fail" table; on the generation side the common causes are no live nodes meeting the publication gate this round (latency ≤ 800ms, 3/3 rounds passed, jitter ≤ 300ms) or the official Mihomo SHA-256 being unavailable, while publish-side failures relate to Pages deployment/SHA verification; any unmet condition keeps the previous Pages content |
| Pages site 404 | Confirm Settings → Pages Source has been switched to "GitHub Actions" and the `deploy-pages` job succeeded |
| Subscription updates slowly | Actions runs at minute 13 and 43 of every hour UTC, but GitHub Actions scheduled tasks may be delayed or dropped (see "Why automatic updates may fail"); run `AI Self-Healing Proxy v7` manually or use `run-update.ps1` at the repo root. Confirm the `deploy-pages` job succeeded (that job publishes the Pages content) |
| Node.js runtime warnings | Confirm Pages Source is "GitHub Actions" to avoid GitHub creating an implicit branch deployment that bypasses this project's workflow |

**Please use only in compliance with local laws, regulations and relevant terms of service.**