"""Unit tests for the pure functions in generator.py.

Run from the repository root:

    uv run python -m unittest discover -s tests
"""

import ast
import contextlib
import gzip
import hashlib
import io
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

# Tests are expected to run from the repository root, e.g.:
#   uv run python -m unittest discover -s tests
import generator as gen


class RegionAndScoreTest(unittest.TestCase):
    def test_detect_region(self):
        cases = {
            "hk-node": "HK",
            "🇭🇰": "HK",
            "香港": "HK",
            "japan server": "JP",
            "日本": "JP",
            "us-west": "US",
            "美国": "US",
            "singapore": "SG",
            "新加坡": "SG",
            "unknown": "OTHER",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(gen.detect_region(name), expected)

    def test_deterministic_health_score(self):
        a = gen.health_score("node", 100, "HK")
        b = gen.health_score("node", 100, "HK")
        c = gen.health_score("node", 200, "HK")
        self.assertEqual(a, b)
        self.assertGreater(a, c)  # lower latency => higher score


class BuildAndValidateTest(unittest.TestCase):
    def _metric(self, mod, name, latency, region):
        return mod.ProxyMetric(
            proxy={
                "name": name,
                "type": "ss",
                "server": "1.2.3.4",
                "port": 8388,
                "password": "p",
                "cipher": "aes-256-gcm",
            },
            latency=latency,
            region=region,
            health_score=mod.health_score(name, latency, region),
        )

    def test_build_and_validate(self):
        metrics = [
            self._metric(gen, "HK1", 10, "HK"),
            self._metric(gen, "JP1", 50, "JP"),
            self._metric(gen, "US1", 120, "US"),
            self._metric(gen, "SG1", 80, "SG"),
        ]
        config = gen.build_config(metrics)
        gen.validate_config(config)  # must not raise
        self.assertTrue(config["proxies"])
        names = {g["name"] for g in config["proxy-groups"]}
        self.assertEqual(names, set(gen.REQUIRED_GROUPS))

    def test_direct_fallback_when_empty(self):
        config = gen.build_config([])
        gen.validate_config(config)
        # The placeholder must be a legal proxy entry, not the pseudo-type
        # "direct" which mihomo would reject inside the proxies: list.
        self.assertEqual(config["proxies"][0]["type"], "socks5")
        self.assertEqual(config["proxies"][0]["server"], "127.0.0.1")

    def test_benchmark_network_settings_match_published_config(self):
        proxy = {
            "name": "node",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "pw",
            "cipher": "aes-256-gcm",
        }
        metric = self._metric(gen, "node", 100, "OTHER")
        published = gen.build_config([metric])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark.yaml"
            gen.write_benchmark_config(path, [proxy], 12345, 12346)
            benchmark = gen._safe_load_yaml(path.read_text(encoding="utf-8"))

        for key in ("ipv6", "unified-delay", "tcp-concurrent"):
            self.assertEqual(benchmark[key], published[key])
        self.assertNotIn("global-client-fingerprint", benchmark)
        self.assertNotIn("global-client-fingerprint", published)
        auto_fast = next(
            group for group in published["proxy-groups"] if group["name"] == "AUTO-FAST"
        )
        benchmark_group = benchmark["proxy-groups"][0]
        self.assertEqual(benchmark_group["proxies"], auto_fast["proxies"])
        self.assertEqual(auto_fast["url"], gen.TEST_URL)

    def test_validate_rejects_empty_group(self):
        metrics = [self._metric(gen, "HK1", 10, "HK")]
        config = gen.build_config(metrics)
        for group in config["proxy-groups"]:
            if group["name"] == "JP-POOL":
                group["proxies"] = []
        with self.assertRaises(RuntimeError):
            gen.validate_config(config)

    def test_validate_rejects_missing_group(self):
        metrics = [self._metric(gen, "HK1", 10, "HK")]
        config = gen.build_config(metrics)
        config["proxy-groups"] = [g for g in config["proxy-groups"] if g["name"] != "PROXY"]
        with self.assertRaises(RuntimeError):
            gen.validate_config(config)

    def test_validate_rejects_wrong_group_type(self):
        metrics = [self._metric(gen, "HK1", 10, "HK")]
        config = gen.build_config(metrics)
        for group in config["proxy-groups"]:
            if group["name"] == "AUTO-FAST":
                group["type"] = "select"
        with self.assertRaises(RuntimeError):
            gen.validate_config(config)

    def test_validate_rejects_invalid_proxy_entry(self):
        metrics = [self._metric(gen, "HK1", 10, "HK")]
        config = gen.build_config(metrics)
        config["proxies"][0].pop("server")
        with self.assertRaises(RuntimeError):
            gen.validate_config(config)

    def test_validate_rejects_unsupported_proxy_type(self):
        metrics = [self._metric(gen, "HK1", 10, "HK")]
        config = gen.build_config(metrics)
        config["proxies"][0]["type"] = "unknown"
        with self.assertRaises(RuntimeError):
            gen.validate_config(config)

    def test_validate_rejects_missing_protocol_fields(self):
        metrics = [self._metric(gen, "HK1", 10, "HK")]
        config = gen.build_config(metrics)
        config["proxies"][0].pop("cipher")
        with self.assertRaisesRegex(RuntimeError, "missing required fields"):
            gen.validate_config(config)

    def test_validate_rejects_scalar_rules(self):
        config = gen.build_config([self._metric(gen, "HK1", 10, "HK")])
        config["rules"] = "\n".join(gen.REQUIRED_RULES)
        with self.assertRaisesRegex(RuntimeError, "rules must be a list"):
            gen.validate_config(config)


class CleanupAndFingerprintTest(unittest.TestCase):
    def test_fingerprint_stable(self):
        proxy = {"type": "ss", "server": "x", "port": 1}
        self.assertEqual(gen.proxy_fingerprint(proxy), gen.proxy_fingerprint(proxy))

    def test_fingerprint_ignores_name_but_keeps_all_config_fields(self):
        base = {"type": "vless", "server": "x", "port": 443, "uuid": "u"}
        self.assertEqual(
            gen.proxy_fingerprint({**base, "name": "a"}),
            gen.proxy_fingerprint({**base, "name": "b"}),
        )
        self.assertNotEqual(
            gen.proxy_fingerprint({**base, "ip-version": "ipv4", "ca": "ca-a"}),
            gen.proxy_fingerprint({**base, "ip-version": "ipv6", "ca": "ca-b"}),
        )


class ProbePolicyTest(unittest.TestCase):
    """Multi-round / multi-URL survival policy."""

    def setUp(self) -> None:
        self.proxy = {"name": "n", "type": "ss", "server": "1.2.3.4", "port": 8388}

    def test_flaky_node_stops_after_first_failed_round(self):
        with mock.patch.object(
            gen,
            "_probe_round_latency",
            side_effect=[100, None, 50],
        ) as probe:
            self.assertIsNone(gen.test_single_proxy("http://x", self.proxy))
        self.assertEqual(probe.call_count, 2)

    def test_stable_node_kept_with_median_latency(self):
        with mock.patch.object(gen, "_probe_round_latency", side_effect=[200, 100, 30]):
            metric = gen.test_single_proxy("http://x", self.proxy)
        self.assertIsNotNone(metric)
        self.assertEqual(metric.latency, 100)  # median of [30, 100, 200]
        self.assertEqual(metric.pass_count, 3)
        self.assertEqual(metric.jitter_ms, 170)

    def test_even_rounds_use_average_median(self):
        original = gen.PROBE_TIMES
        try:
            gen.PROBE_TIMES = 2
            with mock.patch.object(gen, "_probe_round_latency", side_effect=[100, 200]):
                metric = gen.test_single_proxy("http://x", self.proxy)
        finally:
            gen.PROBE_TIMES = original
        self.assertIsNotNone(metric)
        self.assertEqual(metric.latency, 150)  # median of [100, 200]

    def test_round_requires_all_urls_to_pass(self):
        with mock.patch.object(gen, "_probe_latency", side_effect=[50, None, 60]):
            self.assertIsNone(gen._probe_round_latency("http://x", "n"))

    def test_round_uses_slowest_passing_url(self):
        with mock.patch.object(gen, "_probe_latency", side_effect=[50, 200, 60]):
            self.assertEqual(gen._probe_round_latency("http://x", "n"), 200)

    def test_publishable_metrics_rejects_slow_flaky_and_jittery_nodes(self):
        def metric(name, latency, pass_count=3, jitter_ms=0):
            return gen.ProxyMetric(
                proxy={"name": name, "type": "ss", "server": name, "port": 8388},
                latency=latency,
                region="OTHER",
                health_score=gen.health_score(name, latency, "OTHER", pass_count / 3),
                pass_count=pass_count,
                jitter_ms=jitter_ms,
            )

        metrics = [
            metric("good", 300, jitter_ms=50),
            metric("slow", gen.PUBLISH_MAX_LATENCY_MS + 1),
            metric("flaky", 100, pass_count=2),
            metric("jittery", 100, jitter_ms=gen.PUBLISH_MAX_JITTER_MS + 1),
        ]
        self.assertEqual(
            [item.proxy["name"] for item in gen.publishable_metrics(metrics)],
            ["good"],
        )

    def test_low_quality_nodes_are_not_published(self):
        good = [
            gen.ProxyMetric(
                proxy={"name": f"good-{i}", "type": "ss", "server": f"good-{i}", "port": 8388},
                latency=100 + i,
                region="OTHER",
                health_score=gen.health_score(f"good-{i}", 100 + i, "OTHER"),
                pass_count=gen.PROBE_TIMES,
                jitter_ms=20,
            )
            for i in range(3)
        ]
        slow = [
            gen.ProxyMetric(
                proxy={"name": f"slow-{i}", "type": "ss", "server": f"slow-{i}", "port": 8388},
                latency=1500,
                region="HK",
                health_score=gen.health_score(f"slow-{i}", 1500, "HK"),
                pass_count=gen.PROBE_TIMES,
                jitter_ms=20,
            )
            for i in range(22)
        ]
        published = gen.publishable_metrics(good + slow)
        self.assertEqual(
            [item.proxy["name"] for item in published], ["good-0", "good-1", "good-2"]
        )

    def test_region_name_does_not_override_latency(self):
        self.assertGreater(
            gen.health_score("other", 700, "OTHER"),
            gen.health_score("hk", 1500, "HK"),
        )


class TruncationTest(unittest.TestCase):
    """AUTO-FAST is a curated top-N pool; ALL holds every node."""

    def setUp(self) -> None:
        self.metrics = [
            gen.ProxyMetric(
                proxy={
                    "name": f"n{i}",
                    "type": "ss",
                    "server": "x",
                    "port": 8080,
                    "password": "p",
                    "cipher": "aes-256-gcm",
                },
                latency=10 + i,
                region="HK" if i % 2 == 0 else "OTHER",
                health_score=1.0 - i / 1000.0,
            )
            for i in range(60)
        ]

    def _groups(self):
        config = gen.build_config(self.metrics)
        return {
            group["name"]: group["proxies"]
            for group in config["proxy-groups"]
            if isinstance(group, dict)
        }

    def test_all_group_contains_every_node(self):
        groups = self._groups()
        self.assertEqual(set(groups["ALL"]), {m.proxy["name"] for m in self.metrics})
        self.assertIsInstance(groups["ALL"], list)

    def test_auto_fast_is_capped_at_default(self):
        auto = self._groups()["AUTO-FAST"]
        self.assertEqual(len(auto), gen.AUTO_FAST_MAX)

    def test_auto_fast_is_subset_of_all(self):
        groups = self._groups()
        self.assertTrue(set(groups["AUTO-FAST"]) <= set(groups["ALL"]))

    def test_proxy_can_select_all_nodes(self):
        self.assertIn("ALL", self._groups()["PROXY"])

    def test_auto_fast_all_opt_in(self):
        original = gen.AUTO_FAST_MAX
        try:
            gen.AUTO_FAST_MAX = 0
            groups = self._groups()
            self.assertEqual(set(groups["AUTO-FAST"]), set(groups["ALL"]))
        finally:
            gen.AUTO_FAST_MAX = original

    def test_region_pool_is_capped(self):
        hk = self._groups()["HK-POOL"]
        self.assertLessEqual(len(hk), gen.REGION_POOL_MAX)
        gen.validate_config(gen.build_config(self.metrics))


class SkipCertTest(unittest.TestCase):
    """skip-cert-verify injection is gated off by default."""

    def tearDown(self) -> None:
        gen.SKIP_CERT_VERIFY = 0

    def test_disabled_by_default(self):
        gen.SKIP_CERT_VERIFY = 0
        proxy = {"name": "a", "type": "vmess", "server": "x", "port": 1, "uuid": "u"}
        out = gen._maybe_inject_skip_cert_verify(proxy)
        self.assertEqual(out, proxy)
        self.assertNotIn("skip-cert-verify", out)

    def test_enabled_injects_for_tls_types(self):
        gen.SKIP_CERT_VERIFY = 1
        for ptype in gen.TLS_PROXY_TYPES:
            with self.subTest(ptype=ptype):
                out = gen._maybe_inject_skip_cert_verify({"name": "a", "type": ptype})
                self.assertTrue(out.get("skip-cert-verify"))
        out = gen._maybe_inject_skip_cert_verify({"name": "a", "type": "ss"})
        self.assertNotIn("skip-cert-verify", out)

    def test_build_config_keeps_cert_check_when_off(self):
        gen.SKIP_CERT_VERIFY = 0
        metric = gen.ProxyMetric(
            proxy={"name": "h", "type": "hysteria2", "server": "x", "port": 1, "password": "p"},
            latency=10,
            region="OTHER",
            health_score=1.0,
        )
        config = gen.build_config([metric])
        self.assertNotIn("skip-cert-verify", config["proxies"][0])


class EnvConfigTest(unittest.TestCase):
    """Environment-variable config semantics."""

    def test_max_latency_zero_falls_back_to_timeout(self):
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_MAX_LATENCY_MS": "0"}):
            self.assertEqual(gen._max_latency_pass_ms(), gen.LATENCY_TIMEOUT_MS)
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_MAX_LATENCY_MS": "1234"}):
            self.assertEqual(gen._max_latency_pass_ms(), 1234)


class FixRegressionTest(unittest.TestCase):
    """Coverage for the 2026-08 review fixes."""

    def test_normalize_proxy_drops_ipv6_ssr(self):
        self.assertIsNone(
            gen.normalize_proxy(
                {
                    "type": "ssr",
                    "name": "n",
                    "server": "2001:db8::1",
                    "port": 1001,
                    "cipher": "aes-256-cfb",
                    "password": "p",
                },
                1,
            )
        )
        kept = gen.normalize_proxy(
            {
                "type": "ssr",
                "name": "n",
                "server": "1.2.3.4",
                "port": 1001,
                "cipher": "aes-256-cfb",
                "password": "p",
            },
            2,
        )
        self.assertIsNotNone(kept)

    def test_probe_latency_bad_json_returns_none(self):
        class FakeResponse:
            status_code = 200

            def json(self):
                raise ValueError("not json")

        with mock.patch.object(gen.requests, "get", return_value=FakeResponse()):
            self.assertIsNone(gen._probe_latency("http://x", "n", "http://u"))

    def test_negative_env_int_falls_back(self):
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_MAX_CANDIDATES": "-5"}):
            self.assertEqual(gen._env_int("FREE_PROXY_AIRPORT_MAX_CANDIDATES", 500), 500)

    def test_normalize_proxy_rejects_bool_port(self):
        self.assertIsNone(
            gen.normalize_proxy({"type": "ss", "server": "s", "port": True, "password": "p"}, 1)
        )

    def test_atomic_write_text(self):
        target = Path("test-atomic-write.txt")
        try:
            gen._atomic_write_text(target, "hello")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")
        finally:
            target.unlink(missing_ok=True)

    def test_probe_times_are_bounded(self):
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_PROBE_TIMES": "0"}):
            self.assertEqual(gen._probe_times(), 1)
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_PROBE_TIMES": "100"}):
            self.assertEqual(gen._probe_times(), 10)


class MainFlowTest(unittest.TestCase):
    """Coverage for the main entry path that the old suite never touched."""

    def test_collect_proxies_parallel_path(self):
        proxy = {
            "name": "n",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "pw",
            "cipher": "aes-256-gcm",
        }
        with mock.patch.object(gen, "_fetch_source", return_value=[proxy]):
            total, sanitized = gen.collect_proxies()
        self.assertEqual(total, len(gen.SOURCE_GROUPS))
        self.assertEqual(len(sanitized), 1)  # identical nodes dedup to one

    def test_main_smoke(self):
        proxy = {
            "name": "n",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "pw",
            "cipher": "aes-256-gcm",
        }
        metric = gen.ProxyMetric(
            proxy=proxy, latency=50, region="HK", health_score=gen.health_score("n", 50, "HK")
        )
        with (
            mock.patch.object(gen, "collect_proxies", return_value=(1, [proxy])),
            mock.patch.object(gen, "benchmark_proxies", return_value=[metric]),
            mock.patch.object(gen, "write_config"),
            mock.patch.object(gen, "_atomic_write_text"),
            mock.patch.object(gen, "print_summary"),
        ):
            gen.main()  # must not raise and must not touch real outputs

    def test_main_does_not_publish_low_quality_nodes(self):
        def metric(name, latency, pass_count=3, jitter_ms=20):
            proxy = {
                "name": name,
                "type": "ss",
                "server": f"{name}.example.com",
                "port": 8388,
                "password": "pw",
                "cipher": "aes-256-gcm",
            }
            return gen.ProxyMetric(
                proxy=proxy,
                latency=latency,
                region="OTHER",
                health_score=gen.health_score(name, latency, "OTHER", pass_count / 3),
                pass_count=pass_count,
                jitter_ms=jitter_ms,
            )

        live = [metric(f"good-{i}", 100 + i) for i in range(3)]
        live += [metric(f"slow-{i}", 1500) for i in range(22)]
        with (
            mock.patch.object(gen, "collect_proxies", return_value=(25, [live[0].proxy])),
            mock.patch.object(gen, "benchmark_proxies", return_value=live),
            mock.patch.object(gen, "write_config") as write_config,
            mock.patch.object(gen, "print_summary"),
        ):
            gen.main()

        config = write_config.call_args.args[0]
        self.assertEqual(
            [proxy["name"] for proxy in config["proxies"]], ["good-0", "good-1", "good-2"]
        )

    def test_benchmark_batches_partial_failure(self):
        original = gen.BENCHMARK_BATCH_SIZE
        gen.BENCHMARK_BATCH_SIZE = 1
        engine = Path("fake-mihomo")
        ok = {"name": "ok", "type": "ss", "server": "1.2.3.4", "port": 8388, "password": "pw"}
        metric = gen.ProxyMetric(
            proxy=ok, latency=10, region="HK", health_score=gen.health_score("ok", 10, "HK")
        )
        try:
            # Batch 1 fails; batch 2 yields nothing -> overall [] without raising.
            with (
                mock.patch.object(gen, "find_or_install_mihomo", return_value=engine),
                mock.patch.object(
                    gen,
                    "_benchmark_batch",
                    side_effect=[gen.BenchmarkConfigError("batch 1 failed"), []],
                ),
            ):
                self.assertEqual(gen.benchmark_proxies([ok, ok]), [])
            # Batch 2 failure must not discard batch 1's metrics.
            with (
                mock.patch.object(gen, "find_or_install_mihomo", return_value=engine),
                mock.patch.object(
                    gen,
                    "_benchmark_batch",
                    side_effect=[[metric], gen.BenchmarkConfigError("batch 2 failed")],
                ),
            ):
                got = gen.benchmark_proxies([ok, ok])
            self.assertEqual(len(got), 1)
        finally:
            gen.BENCHMARK_BATCH_SIZE = original

    def test_benchmark_batch_failure_isolates_bad_node(self):
        proxies = [
            {"name": name, "type": "ss", "server": "1.2.3.4", "port": 8388}
            for name in ("good-a", "bad", "good-b")
        ]

        def benchmark(_engine, batch):
            if any(proxy["name"] == "bad" for proxy in batch):
                raise gen.BenchmarkConfigError("invalid node")
            return [
                gen.ProxyMetric(proxy, 10, "HK", gen.health_score(proxy["name"], 10, "HK"))
                for proxy in batch
            ]

        with mock.patch.object(gen, "_benchmark_batch", side_effect=benchmark):
            metrics = gen._benchmark_batch_isolated(Path("fake-mihomo"), proxies)
        self.assertEqual({metric.proxy["name"] for metric in metrics}, {"good-a", "good-b"})

    def test_benchmark_system_failure_does_not_bisect(self):
        proxies = [{"name": f"n-{index}"} for index in range(16)]
        with (
            mock.patch.object(
                gen, "_benchmark_batch", side_effect=RuntimeError("engine unavailable")
            ) as benchmark,
            self.assertRaisesRegex(RuntimeError, "engine unavailable"),
        ):
            gen._benchmark_batch_isolated(Path("fake-mihomo"), proxies)
        benchmark.assert_called_once()


class CertVerifyPolicyTest(unittest.TestCase):
    """Trojan/TUIC URIs keep TLS verification on by default."""

    def tearDown(self) -> None:
        gen.SKIP_CERT_VERIFY = 0


class HysteriaCertPolicyTest(unittest.TestCase):
    """Hysteria/hysteria2 URIs honour skip-cert-verify like trojan/tuic."""

    def tearDown(self) -> None:
        gen.SKIP_CERT_VERIFY = False

    def _proxy(self, extra: dict | None = None) -> dict:
        proxy = {
            "name": "n",
            "type": "hysteria2",
            "server": "1.2.3.4",
            "port": 443,
            "password": "p",
        }
        if extra:
            proxy.update(extra)
        return proxy


class MihomoAssetSelectionTest(unittest.TestCase):
    """Asset filtering / discovery robustness (R3-L2/L3/L4/L5)."""

    LINUX_TOKENS = ("linux", ["amd64"])

    def test_filter_rejects_tarball_and_orders_candidates(self):
        names = [
            "mihomo-linux-amd64-v1.0.0.tar.gz",
            "mihomo-linux-amd64-compatible-v1.0.0.gz",
            "mihomo-linux-amd64-v1.0.0.gz",
        ]
        # Pin the platform so the assertion holds on any dev machine.
        with mock.patch.object(gen, "mihomo_platform_tokens", return_value=self.LINUX_TOKENS):
            matched = gen.filter_mihomo_assets(names)
        self.assertNotIn(names[0], matched)
        self.assertEqual(matched[0], names[1])  # compatible variant scores best

    def test_scrape_without_digest_fails_closed(self):
        class RedirectResponse:
            status_code = 302

            def __init__(self) -> None:
                self.headers = {
                    "Location": "https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.2"
                }

        html = (
            '<a href="/MetaCubeX/mihomo/releases/download/v1.19.2/'
            'Mihomo-Linux-amd64-v1.19.2.gz">download</a>'
        )
        with (
            mock.patch.object(gen.requests, "get", return_value=RedirectResponse()),
            mock.patch.object(gen, "fetch_text", return_value=html),
            mock.patch.object(gen, "mihomo_platform_tokens", return_value=self.LINUX_TOKENS),
            mock.patch.object(gen, "mihomo_asset_available", return_value=True),
            self.assertRaises(RuntimeError),
        ):
            gen.select_mihomo_asset()

    def test_invalid_api_tag_fallback_without_digest_fails_closed(self):
        class ApiResponse:
            status_code = 200

            def json(self):
                return {
                    "tag_name": "",
                    "assets": [{"name": "mihomo-linux-amd64-v1.0.0.gz"}],
                }

        class RedirectResponse:
            status_code = 302

            def __init__(self) -> None:
                self.headers = {
                    "Location": "https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.2"
                }

        html = (
            '<a href="/MetaCubeX/mihomo/releases/download/v1.19.2/'
            'mihomo-linux-amd64-v1.19.2.gz">download</a>'
        )
        with (
            mock.patch.object(
                gen.requests, "get", side_effect=[ApiResponse(), RedirectResponse()]
            ),
            mock.patch.object(gen, "fetch_text", return_value=html),
            mock.patch.object(gen, "mihomo_platform_tokens", return_value=self.LINUX_TOKENS),
            mock.patch.object(gen, "mihomo_asset_available", return_value=True),
            self.assertRaises(RuntimeError),
        ):
            gen.select_mihomo_asset()

    def test_download_rejects_oversized_payload(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                while True:
                    yield b"x" * chunk_size

        target_dir = Path(tempfile.mkdtemp(prefix="dl-test-"))
        try:
            with (
                mock.patch.object(gen.requests, "get", return_value=FakeResponse()),
                mock.patch.object(gen, "MAX_DOWNLOAD_BYTES", 1024 * 1024),
                # Avoid the real retry backoff sleep in download_file.
                mock.patch.object(gen.time, "sleep"),
                self.assertRaises(RuntimeError),
            ):
                gen.download_file("https://example.com/mihomo-windows-amd64-v1.zip", target_dir)
        finally:
            shutil.rmtree(target_dir, ignore_errors=True)


class ProbeConfigWarningTest(unittest.TestCase):
    """An unpublishable probe count must skip engine setup."""

    def test_probe_times_below_publication_minimum_skips_benchmark(self):
        buffer = io.StringIO()
        original_times = gen.PROBE_TIMES
        gen.PROBE_TIMES = 1
        try:
            with (
                mock.patch.object(gen, "find_or_install_mihomo") as install,
                contextlib.redirect_stdout(buffer),
            ):
                self.assertEqual(gen.benchmark_proxies([{"name": "n"}]), [])
        finally:
            gen.PROBE_TIMES = original_times
        install.assert_not_called()
        self.assertIn("below publication minimum", buffer.getvalue())


class FetchTextCloseTest(unittest.TestCase):
    """fetch_text must close the streaming response even on failure."""

    def test_response_closed_on_http_error(self):
        events = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                events.append("closed")
                return False

            def raise_for_status(self):
                raise RuntimeError("boom")

        with (
            mock.patch.object(gen.requests, "get", return_value=FakeResponse()),
            self.assertRaises(RuntimeError),
        ):
            gen.fetch_text("http://example.com", retries=1)
        self.assertEqual(events, ["closed"])


class RoundFourFixTest(unittest.TestCase):
    """Coverage for the 2026-08-25 review fixes (Round 4)."""

    def setUp(self) -> None:
        gen._DISCOVER_CACHE.clear()

    # The degraded placeholder must remain a legal Mihomo proxy entry.
    def test_direct_fallback_is_valid_proxy_entry(self):
        config = gen.build_config([])
        gen.validate_config(config)
        proxy = config["proxies"][0]
        self.assertEqual(proxy["type"], "socks5")
        self.assertEqual(proxy["server"], "127.0.0.1")

    def test_fetch_source_prefers_primary_within_window(self):
        source = {"name": "s", "primary": "primary", "fallbacks": ["fallback"]}

        def fetch(_name, url, _cancel_event=None):
            if url == "primary":
                time.sleep(0.02)
            return [{"name": url}]

        with (
            mock.patch.object(gen, "_fetch_one_url", side_effect=fetch),
            mock.patch.object(gen, "PRIMARY_PRIORITY_SECONDS", 0.1),
        ):
            found = gen._fetch_source(source)
        self.assertEqual(found, [{"name": "primary"}])

    def test_fetch_source_uses_fast_fallback_after_priority_window(self):
        source = {"name": "s", "primary": "primary", "fallbacks": ["fallback"]}

        def fetch(_name, url, _cancel_event=None):
            if url == "primary":
                time.sleep(0.05)
            return [{"name": url}]

        with (
            mock.patch.object(gen, "_fetch_one_url", side_effect=fetch),
            mock.patch.object(gen, "PRIMARY_PRIORITY_SECONDS", 0.01),
        ):
            found = gen._fetch_source(source)
        self.assertEqual(found, [{"name": "fallback"}])

    # L2: health_score now consumes real probe stability (pass ratio).
    def test_health_score_uses_real_stability(self):
        low = gen.health_score("n", 100, "HK", stability=0.5)
        high = gen.health_score("n", 100, "HK", stability=1.0)
        self.assertGreater(high, low)
        self.assertEqual(high, gen.health_score("n", 100, "HK", stability=1.0))

    def test_probe_pass_ratio_reaches_health_score(self):
        proxy = {"name": "n", "type": "ss", "server": "1.2.3.4", "port": 8388}
        with mock.patch.object(gen, "_probe_round_latency", side_effect=[100, 100, 100]):
            metric = gen.test_single_proxy("http://x", proxy)
        # Defaults PROBE_TIMES=3 / all rounds passed -> stability 1.0.
        expected = gen.health_score("n", 100, "OTHER", stability=1.0)
        self.assertEqual(metric.health_score, expected)

    # L3: candidates are interleaved across sources before truncation.
    def test_collect_interleaves_sources_before_truncation(self):
        nodes_a = [
            {
                "name": f"a{i}",
                "type": "ss",
                "server": "1.2.3.4",
                "port": 8388 + i,
                "password": "pw",
                "cipher": "aes-256-gcm",
            }
            for i in range(10)
        ]
        nodes_b = [
            {
                "name": f"b{i}",
                "type": "ss",
                "server": "5.6.7.8",
                "port": 8388 + i,
                "password": "pw",
                "cipher": "aes-256-gcm",
            }
            for i in range(10)
        ]
        side = [nodes_a, nodes_b] + [[] for _ in range(len(gen.SOURCE_GROUPS) - 2)]
        cap = 6
        original = gen.MAX_CANDIDATES
        gen.MAX_CANDIDATES = cap
        try:
            with mock.patch.object(gen, "_fetch_source", side_effect=side):
                _, sanitized = gen.collect_proxies()
        finally:
            gen.MAX_CANDIDATES = original
        names = {p["name"] for p in sanitized}
        self.assertTrue(any(name.startswith("b") for name in names))
        self.assertEqual(len(sanitized), cap)

    # L4: discovery failures are not cached; mihomo cache survives a
    # freshness-check failure.
    def test_discover_cache_skips_failure(self):
        with mock.patch.object(gen, "fetch_text", side_effect=RuntimeError("boom")) as fetch:
            self.assertEqual(gen.discover_free_clash_v2ray_urls(), [])
            self.assertEqual(gen.discover_free_clash_v2ray_urls(), [])
        self.assertEqual(fetch.call_count, 2)  # retried, not cached

    def test_discover_cache_keeps_success(self):
        html = "https://free-clash-v2ray.github.io/uploads/2026/08/1-20260801.yaml"
        with mock.patch.object(gen, "fetch_text", return_value=html) as fetch:
            first = gen.discover_free_clash_v2ray_urls()
            second = gen.discover_free_clash_v2ray_urls()
        self.assertEqual(first, second)
        self.assertEqual(fetch.call_count, 1)  # cached after success

    def test_normalize_rejects_tuic_without_password(self):
        proxy = {
            "name": "tuic",
            "type": "tuic",
            "server": "1.2.3.4",
            "port": 443,
            "uuid": "u",
        }
        self.assertIsNone(gen.normalize_proxy(proxy, 1))

    def test_mihomo_cache_survives_freshness_check_failure(self):
        with tempfile.TemporaryDirectory(prefix="mihomo-test-") as tmp:
            install_dir = Path(tmp) / "free-proxy-airport-mihomo"
            install_dir.mkdir()
            binary = install_dir / ("mihomo.exe" if os.name == "nt" else "mihomo")
            binary.write_bytes(b"fake binary")
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            (install_dir / "asset-url.txt").write_text("https://verified", encoding="utf-8")
            (install_dir / "asset-sha256.txt").write_text("a" * 64, encoding="utf-8")
            (install_dir / "binary-sha256.txt").write_text(digest, encoding="utf-8")
            with (
                mock.patch.object(gen.tempfile, "gettempdir", return_value=tmp),
                mock.patch.object(gen.shutil, "which", return_value=None),
                mock.patch.object(gen, "looks_like_binary", return_value=True),
                mock.patch.object(
                    gen, "select_mihomo_asset", side_effect=RuntimeError("no network")
                ),
            ):
                result = gen.find_or_install_mihomo()
            self.assertEqual(result, binary)

    def test_mihomo_cache_digest_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="mihomo-test-") as tmp:
            install_dir = Path(tmp) / "free-proxy-airport-mihomo"
            install_dir.mkdir()
            binary = install_dir / ("mihomo.exe" if os.name == "nt" else "mihomo")
            binary.write_bytes(b"fake binary")
            (install_dir / "asset-url.txt").write_text("https://verified", encoding="utf-8")
            (install_dir / "asset-sha256.txt").write_text("a" * 64, encoding="utf-8")
            (install_dir / "binary-sha256.txt").write_text("b" * 64, encoding="utf-8")
            with (
                mock.patch.object(gen.tempfile, "gettempdir", return_value=tmp),
                mock.patch.object(gen.shutil, "which", return_value=None),
                mock.patch.object(gen, "looks_like_binary", return_value=True),
                mock.patch.object(
                    gen, "select_mihomo_asset", side_effect=RuntimeError("no network")
                ),
                self.assertRaises(RuntimeError),
            ):
                gen.find_or_install_mihomo()
            self.assertFalse(binary.exists())

    def test_env_flag_accepts_spaces_and_case(self):
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_ALLOW_LAN": " 1 "}):
            self.assertTrue(gen._env_flag("FREE_PROXY_AIRPORT_ALLOW_LAN", False))
        with mock.patch.dict(os.environ, {}):
            self.assertFalse(gen._env_flag("FREE_PROXY_AIRPORT_ALLOW_LAN", False))

    def test_env_flag_invalid_value_uses_default(self):
        with (
            mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_DISABLE_MIRRORS": "tru"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertTrue(gen._env_flag("FREE_PROXY_AIRPORT_DISABLE_MIRRORS", True))
        self.assertIn("invalid boolean", output.getvalue())


class RoundSixFixTest(unittest.TestCase):
    """Coverage for the 2026-08-26 review fixes (Round 6)."""

    def tearDown(self) -> None:
        gen.SKIP_CERT_VERIFY = False

    # M1: the health-score tie-break must be bounded far below any real
    # latency/stability difference.
    def test_tie_break_is_bounded_tiny(self):
        base = 0.6 * (1000.0 / 5000) + 0.1
        for name in ("aaaa", "zzzz", "hk-fast-1", "us-west-2"):
            with self.subTest(name=name):
                self.assertLess(abs(gen.health_score(name, 5000, "OTHER", 1.0) - base), 1e-6)

    def test_latency_always_dominates_tie_break(self):
        for low_name, high_name in (
            ("aaa", "zzz"),
            ("us-west-1", "hk-node-9"),
            ("singapore-3", "japan-2"),
        ):
            with self.subTest(low_name=low_name, high_name=high_name):
                self.assertGreater(
                    gen.health_score(low_name, 1999, "OTHER", 1.0),
                    gen.health_score(high_name, 2000, "OTHER", 1.0),
                )
                self.assertGreater(
                    gen.health_score(low_name, 100, "OTHER", 1.0),
                    gen.health_score(high_name, 110, "OTHER", 1.0),
                )

    def test_exact_ties_still_break_deterministically(self):
        a = gen.health_score("node-a", 500, "HK", 1.0)
        b = gen.health_score("node-b", 500, "HK", 1.0)
        self.assertNotEqual(a, b)
        self.assertEqual(a, gen.health_score("node-a", 500, "HK", 1.0))

    # L2: fingerprint must distinguish nodes that differ only in these fields.
    def test_fingerprint_distinguishes_plugin(self):
        base = {"type": "ss", "server": "s", "port": 1, "password": "p", "cipher": "aes-256-gcm"}
        self.assertNotEqual(
            gen.proxy_fingerprint({**base, "plugin": "obfs"}),
            gen.proxy_fingerprint({**base, "plugin": "v2ray-plugin"}),
        )

    def test_fingerprint_distinguishes_credentials_and_params(self):
        self.assertNotEqual(
            gen.proxy_fingerprint({"type": "http", "server": "s", "port": 1, "username": "u1"}),
            gen.proxy_fingerprint({"type": "http", "server": "s", "port": 1, "username": "u2"}),
        )
        self.assertNotEqual(
            gen.proxy_fingerprint({"type": "hysteria2", "server": "s", "port": 443, "auth": "a1"}),
            gen.proxy_fingerprint({"type": "hysteria2", "server": "s", "port": 443, "auth": "a2"}),
        )
        self.assertNotEqual(
            gen.proxy_fingerprint(
                {"type": "hysteria", "server": "s", "port": 443, "auth-str": "a1"}
            ),
            gen.proxy_fingerprint(
                {"type": "hysteria", "server": "s", "port": 443, "auth-str": "a2"}
            ),
        )
        self.assertNotEqual(
            gen.proxy_fingerprint(
                {
                    "type": "vless",
                    "server": "s",
                    "port": 443,
                    "uuid": "u",
                    "tls": "reality",
                    "pbk": "x",
                }
            ),
            gen.proxy_fingerprint(
                {
                    "type": "vless",
                    "server": "s",
                    "port": 443,
                    "uuid": "u",
                    "tls": "reality",
                    "pbk": "y",
                }
            ),
        )

    # L3: untrusted sources cannot disable TLS verification; the global
    # opt-in is the single control.
    def test_normalize_strips_upstream_cert_flags(self):
        out = gen.normalize_proxy(
            {
                "type": "hysteria2",
                "name": "n",
                "server": "x",
                "port": 1,
                "password": "p",
                "skip-cert-verify": True,
                "insecure": True,
            },
            1,
        )
        self.assertNotIn("skip-cert-verify", out)
        self.assertNotIn("insecure", out)

    def test_build_config_does_not_leak_upstream_flag_when_off(self):
        gen.SKIP_CERT_VERIFY = False
        metric = gen.ProxyMetric(
            proxy={
                "name": "h",
                "type": "hysteria2",
                "server": "x",
                "port": 1,
                "password": "p",
                "skip-cert-verify": True,
            },
            latency=10,
            region="OTHER",
            health_score=1.0,
        )
        config = gen.build_config([metric])
        self.assertNotIn("skip-cert-verify", config["proxies"][0])

    def test_build_config_injects_when_global_opt_in(self):
        gen.SKIP_CERT_VERIFY = True
        metric = gen.ProxyMetric(
            proxy={"name": "h", "type": "hysteria2", "server": "x", "port": 1, "password": "p"},
            latency=10,
            region="OTHER",
            health_score=1.0,
        )
        config = gen.build_config([metric])
        self.assertTrue(config["proxies"][0].get("skip-cert-verify"))

    # N1: validate_config rejects dangling group references.
    def test_validate_rejects_dangling_group_reference(self):
        metric = gen.ProxyMetric(
            proxy={
                "name": "A",
                "type": "ss",
                "server": "x",
                "port": 1,
                "password": "p",
                "cipher": "aes-256-gcm",
            },
            latency=10,
            region="HK",
            health_score=1.0,
        )
        config = gen.build_config([metric])
        for group in config["proxy-groups"]:
            if group["name"] == "AUTO-FAST":
                group["proxies"] = ["GHOST"]
        with self.assertRaises(RuntimeError):
            gen.validate_config(config)

    def test_validate_accepts_group_references(self):
        metric = gen.ProxyMetric(
            proxy={
                "name": "A",
                "type": "ss",
                "server": "x",
                "port": 1,
                "password": "p",
                "cipher": "aes-256-gcm",
            },
            latency=10,
            region="HK",
            health_score=1.0,
        )
        gen.validate_config(gen.build_config([metric]))  # must not raise

    # N2: a structurally broken source degrades instead of aborting the run.
    def test_collect_tolerates_structurally_broken_source(self):
        proxy = {
            "name": "n",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "pw",
            "cipher": "aes-256-gcm",
        }
        with mock.patch.object(
            gen,
            "_fetch_source",
            side_effect=[KeyError("missing 'primary'")] + [[proxy]] * (len(gen.SOURCE_GROUPS) - 1),
        ):
            total, sanitized = gen.collect_proxies()
        self.assertEqual(total, len(gen.SOURCE_GROUPS) - 1)
        self.assertEqual(len(sanitized), 1)  # identical nodes dedup to one


class RoundSevenFixTest(unittest.TestCase):
    """Coverage for the Round 7 review fixes (R7-H1/M1/M2/L1/L3 + nits)."""

    # M1: mihomo built-in DIRECT is a legal group reference.
    def test_validate_accepts_builtin_direct(self):
        metric = gen.ProxyMetric(
            proxy={
                "name": "A",
                "type": "ss",
                "server": "x",
                "port": 1,
                "password": "p",
                "cipher": "aes-256-gcm",
            },
            latency=10,
            region="HK",
            health_score=1.0,
        )
        config = gen.build_config([metric])
        for group in config["proxy-groups"]:
            if group["name"] == "US-POOL":
                group["proxies"] = ["DIRECT"]
        gen.validate_config(config)  # must not raise

    # L3: 4xx fails fast; 5xx keeps retrying with backoff.
    def test_fetch_text_fails_fast_on_client_error(self):
        err = gen.requests.exceptions.HTTPError(response=mock.Mock(status_code=404))
        with mock.patch.object(gen.requests, "get", side_effect=err) as get:
            with self.assertRaises(RuntimeError):
                gen.fetch_text("http://example.com", retries=3)
        self.assertEqual(get.call_count, 1)

    def test_fetch_text_retries_server_error(self):
        err = gen.requests.exceptions.HTTPError(response=mock.Mock(status_code=500))
        with (
            mock.patch.object(gen.requests, "get", side_effect=err) as get,
            mock.patch.object(gen.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError):
                gen.fetch_text("http://example.com", retries=3)
        self.assertEqual(get.call_count, 3)

    # L1: third-party download mirrors can be disabled entirely.
    def test_download_attempts_respect_mirror_flag(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gen._download_attempt_urls("https://u"), ["https://u"])
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_DISABLE_MIRRORS": "1"}):
            self.assertEqual(gen._download_attempt_urls("https://u"), ["https://u"])
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_DISABLE_MIRRORS": "0"}):
            attempts = gen._download_attempt_urls("https://u")
        self.assertEqual(attempts[0], "https://u")
        self.assertEqual(len(attempts), 1 + len(gen.MIHOMO_MIRRORS))

    # Nit N3: unreachable API assets fall back to the release-page path.
    def test_api_assets_unreachable_fallback_without_digest_fails_closed(self):
        class ApiResponse:
            status_code = 200

            def json(self):
                return {
                    "tag_name": "v1.0.0",
                    "assets": [
                        {
                            "name": "mihomo-linux-amd64-v1.0.0.gz",
                            "digest": "sha256:" + "a" * 64,
                        }
                    ],
                }

        class RedirectResponse:
            status_code = 302

            def __init__(self) -> None:
                self.headers = {
                    "Location": "https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.2"
                }

        html = (
            '<a href="/MetaCubeX/mihomo/releases/download/v1.19.2/'
            'mihomo-linux-amd64-v1.19.2.gz">download</a>'
        )
        with (
            mock.patch.object(
                gen.requests, "get", side_effect=[ApiResponse(), RedirectResponse()]
            ),
            mock.patch.object(gen, "fetch_text", return_value=html),
            mock.patch.object(gen, "mihomo_platform_tokens", return_value=("linux", ["amd64"])),
            mock.patch.object(gen, "mihomo_asset_available", side_effect=[False, True]),
            self.assertRaises(RuntimeError),
        ):
            gen.select_mihomo_asset()

    # Nit N4: archive extraction returns the engine payload (gz + zip).
    def test_extract_gz_archive(self):
        with tempfile.TemporaryDirectory(prefix="extract-test-") as tmp:
            directory = Path(tmp)
            raw = b"\x7fELF fake engine"
            archive = directory / "mihomo-linux-amd64-v1.0.0.gz"
            with gzip.open(archive, "wb") as gzipped:
                gzipped.write(raw)
            extracted = gen.extract_mihomo_binary(archive, directory)
            self.assertEqual(extracted.read_bytes(), raw)

    def test_extract_zip_archive(self):
        with tempfile.TemporaryDirectory(prefix="extract-test-") as tmp:
            directory = Path(tmp)
            archive = directory / "mihomo-windows-amd64.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("mihomo.exe", b"MZ fake engine")
                zipped.writestr("README.md", "extras must not matter")
            extracted = gen.extract_mihomo_binary(archive, directory)
            self.assertEqual(extracted.name, "mihomo.exe")


class RoundEightFixTest(unittest.TestCase):
    """Regression coverage for the Round 8 review findings."""

    def test_reserved_proxy_name_is_renamed_during_sanitize(self):
        proxy = {
            "name": "DIRECT",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "p",
            "cipher": "aes-256-gcm",
        }
        sanitized = gen.sanitize_interleaved([[proxy]])
        self.assertEqual(sanitized[0]["name"], "DIRECT-2")

    def test_validate_rejects_reserved_proxy_name(self):
        metric = gen.ProxyMetric(
            proxy={"name": "PROXY", "type": "socks5", "server": "x", "port": 1},
            latency=10,
            region="OTHER",
            health_score=1.0,
        )
        with self.assertRaises(RuntimeError):
            gen.validate_config(gen.build_config([metric]))

    def test_ss_without_cipher_is_rejected(self):
        proxy = {
            "name": "bad-ss",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "p",
        }
        self.assertIsNone(gen.normalize_proxy(proxy, 1))
        self.assertIsNotNone(gen.normalize_proxy({**proxy, "cipher": "aes-256-gcm"}, 1))

    def test_fingerprint_includes_nested_protocol_fields(self):
        base = {"type": "vless", "server": "s", "port": 443, "uuid": "u"}
        self.assertNotEqual(
            gen.proxy_fingerprint(
                {**base, "reality-opts": {"public-key": "a"}, "client-fingerprint": "chrome"}
            ),
            gen.proxy_fingerprint(
                {**base, "reality-opts": {"public-key": "b"}, "client-fingerprint": "firefox"}
            ),
        )
        hy2 = {"type": "hysteria2", "server": "s", "port": 443, "password": "p"}
        self.assertNotEqual(
            gen.proxy_fingerprint({**hy2, "obfs-password": "a"}),
            gen.proxy_fingerprint({**hy2, "obfs-password": "b"}),
        )


class RoundNineFixTest(unittest.TestCase):
    """GitHub Actions deployment hardening regressions."""

    def test_api_asset_digest_is_recorded(self):
        name = "mihomo-linux-amd64-compatible-v1.0.0.gz"

        class ApiResponse:
            status_code = 200

            def json(self):
                return {
                    "tag_name": "v1.0.0",
                    "assets": [{"name": name, "digest": "sha256:" + "a" * 64}],
                }

        with (
            mock.patch.object(gen.requests, "get", return_value=ApiResponse()),
            mock.patch.object(gen, "mihomo_platform_tokens", return_value=("linux", ["amd64"])),
            mock.patch.object(gen, "mihomo_asset_available", return_value=True),
            mock.patch.dict(gen.MIHOMO_ASSET_SHA256, {}, clear=True),
        ):
            url = gen.select_mihomo_asset()
            self.assertEqual(gen.MIHOMO_ASSET_SHA256[url], "a" * 64)

    def test_normalize_strips_unknown_fields(self):
        proxy = {
            "name": "n",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "p",
            "cipher": "aes-256-gcm",
            "proxy-providers": {"evil": "value"},
        }
        normalized = gen.normalize_proxy(proxy, 1)
        self.assertIsNotNone(normalized)
        self.assertNotIn("proxy-providers", normalized)

    def test_proxy_fields_read_by_code_are_allowlisted(self):
        tree = ast.parse(Path("generator.py").read_text(encoding="utf-8"))
        read_fields = {
            call.args[0].value
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "proxy"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        }
        self.assertEqual(read_fields - gen.ALLOWED_PROXY_FIELDS, set())

    def test_normalize_rejects_oversized_retained_field(self):
        proxy = {
            "name": "n",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "p",
            "cipher": "aes-256-gcm",
            "plugin-opts": {"host": "x" * 1000},
        }
        with mock.patch.object(gen, "MAX_PROXY_BYTES", 256):
            self.assertIsNone(gen.normalize_proxy(proxy, 1))

    def test_yaml_alias_expansion_is_bounded_before_serialization(self):
        levels = ["a: &a [x, x, x, x, x]"]
        for previous, name in zip("abc", "bcd"):
            levels.append(f"{name}: &{name} [*{previous}, *{previous}, *{previous}, *{previous}]")
        text = "\n".join(levels) + (
            "\nproxies:\n  - {name: bomb, type: socks5, server: 127.0.0.1, port: 1, smux: *d}\n"
        )
        proxies = gen.extract_proxies(text)
        with mock.patch.object(gen, "MAX_PROXY_STRUCTURE_NODES", 100):
            self.assertIsNone(gen.normalize_proxy(proxies[0], 1))

    def test_yaml_alias_count_is_limited(self):
        text = "a: &a [x]\nb: [*a, *a]\n"
        with (
            mock.patch.object(gen, "MAX_YAML_ALIASES", 1),
            self.assertRaises(gen.yaml.YAMLError),
        ):
            gen._safe_load_yaml(text)

    def test_download_verifies_official_sha256(self):
        payload = b"\x1f\x8b" + b"verified archive"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield payload

        url = "https://example.com/mihomo-linux-amd64.gz"
        digest = __import__("hashlib").sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(gen.requests, "get", return_value=FakeResponse()),
                mock.patch.object(gen, "MIN_ARCHIVE_SIZE", 1),
                mock.patch.dict(gen.MIHOMO_ASSET_SHA256, {url: digest}, clear=True),
            ):
                path = gen.download_file(url, Path(tmp))
            self.assertEqual(path.read_bytes(), payload)

    def test_download_rejects_sha256_mismatch(self):
        payload = b"\x1f\x8b" + b"tampered archive"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield payload

        url = "https://example.com/mihomo-linux-amd64.gz"
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(gen.requests, "get", return_value=FakeResponse()),
                mock.patch.object(gen, "MIN_ARCHIVE_SIZE", 1),
                mock.patch.dict(gen.MIHOMO_ASSET_SHA256, {url: "0" * 64}, clear=True),
                self.assertRaises(RuntimeError),
            ):
                gen.download_file(url, Path(tmp))

    def test_github_api_uses_token_when_available(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "read-token"}):
            headers = gen._github_headers()
        self.assertEqual(headers["Authorization"], "Bearer read-token")

    def test_engine_environment_strips_github_credentials(self):
        with mock.patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "secret",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc",
                "SAFE_VALUE": "kept",
            },
        ):
            environment = gen._engine_environment()
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", environment)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", environment)
        self.assertEqual(environment["SAFE_VALUE"], "kept")

    def test_require_live_disables_stale_fallback(self):
        with (
            mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_REQUIRE_LIVE": "1"}),
            mock.patch.object(gen, "collect_proxies", return_value=(0, [])),
            mock.patch.object(gen, "load_existing_metrics") as existing,
            self.assertRaises(RuntimeError),
        ):
            gen.main()
        existing.assert_not_called()

    def test_require_live_rejects_only_low_quality_nodes(self):
        proxy = {
            "name": "slow",
            "type": "ss",
            "server": "slow.example.com",
            "port": 8388,
            "password": "pw",
            "cipher": "aes-256-gcm",
        }
        metric = gen.ProxyMetric(
            proxy=proxy,
            latency=1500,
            region="OTHER",
            health_score=gen.health_score("slow", 1500, "OTHER"),
            pass_count=gen.PROBE_TIMES,
            jitter_ms=20,
        )
        with (
            mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_REQUIRE_LIVE": "1"}),
            mock.patch.object(gen, "collect_proxies", return_value=(1, [proxy])),
            mock.patch.object(gen, "benchmark_proxies", return_value=[metric]),
            mock.patch.object(gen, "load_existing_metrics") as existing,
            self.assertRaisesRegex(RuntimeError, "no high-quality live nodes"),
        ):
            gen.main()
        existing.assert_not_called()

    def test_validate_with_mihomo_invokes_real_config_test(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(gen, "find_or_install_mihomo", return_value=Path("mihomo")),
            mock.patch.object(gen.subprocess, "run", return_value=completed) as run,
        ):
            gen.validate_with_mihomo(Path("docs/clash.yaml"))
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["mihomo", "-t", "-f"])
        self.assertEqual(Path(command[3]), Path("docs/clash.yaml"))


class ProtocolAndControllerReviewFixTest(unittest.TestCase):
    def test_normalize_strips_dialer_proxy(self):
        proxy = {
            "name": "chained",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "pw",
            "cipher": "aes-256-gcm",
            "dialer-proxy": "upstream-node",
        }
        normalized = gen.normalize_proxy(proxy, 1)
        self.assertIsNotNone(normalized)
        self.assertNotIn("dialer-proxy", normalized)

    def test_client_health_checks_use_https(self):
        config = gen.build_config([])
        tested_groups = [
            group for group in config["proxy-groups"] if group["type"] in {"url-test", "fallback"}
        ]
        self.assertTrue(gen.TEST_URL.startswith("https://"))
        self.assertTrue(all(group["url"].startswith("https://") for group in tested_groups))

    def test_benchmark_config_rejection_is_explicit(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="invalid proxy")
        with (
            mock.patch.object(gen.subprocess, "run", return_value=completed),
            self.assertRaisesRegex(gen.BenchmarkConfigError, "invalid proxy"),
        ):
            gen._validate_benchmark_config(Path("mihomo"), Path("benchmark.yaml"))

    def test_early_controller_exit_is_a_system_failure(self):
        process = mock.Mock()
        process.poll.return_value = 1
        with tempfile.TemporaryDirectory() as directory:
            stderr_path = Path(directory) / "mihomo.stderr.log"
            stderr_path.write_text("address already in use", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "address already in use") as raised:
                gen.wait_for_controller("http://127.0.0.1:1", process, stderr_path)
        self.assertNotIsInstance(raised.exception, gen.BenchmarkConfigError)

    def test_benchmark_reaps_process_after_kill(self):
        process = mock.Mock()
        process.wait.side_effect = [gen.subprocess.TimeoutExpired("mihomo", 5), 0]
        proxy = {"name": "node", "type": "socks5", "server": "127.0.0.1", "port": 1}
        with (
            mock.patch.object(gen, "write_benchmark_config"),
            mock.patch.object(gen, "_validate_benchmark_config"),
            mock.patch.object(gen.subprocess, "Popen", return_value=process),
            mock.patch.object(gen, "wait_for_controller"),
            mock.patch.object(gen, "run_delay_tests", return_value=[]),
        ):
            self.assertEqual(gen._benchmark_batch(Path("mihomo"), [proxy]), [])
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)


class ReviewImplementationRegressionTest(unittest.TestCase):
    def test_mixed_nested_mapping_keys_drop_only_bad_node(self):
        bad = {
            "name": "bad",
            "type": "vmess",
            "server": "1.2.3.4",
            "port": 443,
            "uuid": "u",
            "ws-opts": {"headers": {"Host": "example.com", 1: "invalid"}},
        }
        good = {
            "name": "good",
            "type": "socks5",
            "server": "1.2.3.4",
            "port": 1080,
        }
        self.assertEqual(gen.sanitize_interleaved([[bad, good]]), [good])

    def test_validate_rejects_scalar_group_references(self):
        config = gen.build_config([])
        config["proxy-groups"][0]["proxies"] = "DIRECT-FALLBACK"
        with self.assertRaisesRegex(RuntimeError, "non-empty string list"):
            gen.validate_config(config)

    def test_validate_requires_final_match_rule(self):
        config = gen.build_config([])
        config["rules"].insert(0, config["rules"].pop())
        with self.assertRaisesRegex(RuntimeError, "end with MATCH,PROXY"):
            gen.validate_config(config)

    def test_failed_refresh_keeps_verified_cached_engine(self):
        with tempfile.TemporaryDirectory(prefix="mihomo-refresh-test-") as tmp:
            install_dir = Path(tmp) / "free-proxy-airport-mihomo"
            install_dir.mkdir()
            binary = install_dir / ("mihomo.exe" if os.name == "nt" else "mihomo")
            binary.write_bytes(b"verified old engine")
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            (install_dir / "binary-sha256.txt").write_text(digest, encoding="utf-8")
            with (
                mock.patch.object(gen.tempfile, "gettempdir", return_value=tmp),
                mock.patch.object(gen.shutil, "which", return_value=None),
                mock.patch.object(gen, "looks_like_binary", return_value=True),
                mock.patch.object(gen, "_needs_engine_refresh", return_value=True),
                mock.patch.object(
                    gen,
                    "select_mihomo_asset",
                    side_effect=RuntimeError("download lookup failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "download lookup failed"),
            ):
                gen.find_or_install_mihomo()
            self.assertEqual(binary.read_bytes(), b"verified old engine")

    def test_benchmark_retries_port_conflict_with_distinct_ports(self):
        process = mock.Mock()
        process.wait.return_value = 0
        proxy = {"name": "node", "type": "socks5", "server": "127.0.0.1", "port": 1}
        with (
            mock.patch.object(
                gen,
                "find_free_port",
                side_effect=[1000, 1000, 1001, 1002, 1003],
            ),
            mock.patch.object(gen, "write_benchmark_config") as write_config,
            mock.patch.object(
                gen,
                "_validate_benchmark_config",
                side_effect=[gen.BenchmarkPortError("address already in use"), None],
            ) as validate,
            mock.patch.object(gen.subprocess, "Popen", return_value=process),
            mock.patch.object(gen, "wait_for_controller"),
            mock.patch.object(gen, "run_delay_tests", return_value=[]),
        ):
            self.assertEqual(gen._benchmark_batch(Path("mihomo"), [proxy]), [])
        self.assertEqual(validate.call_count, 2)
        first_ports = write_config.call_args_list[0].args[-2:]
        second_ports = write_config.call_args_list[1].args[-2:]
        self.assertEqual(first_ports, (1000, 1001))
        self.assertEqual(second_ports, (1002, 1003))

    def test_successful_refresh_atomically_installs_and_records_engine(self):
        with tempfile.TemporaryDirectory(prefix="mihomo-install-test-") as tmp:
            extracted = Path(tmp) / "downloaded-mihomo"
            extracted.write_bytes(b"new verified engine")
            url = "https://example.com/mihomo.gz"
            with (
                mock.patch.object(gen.tempfile, "gettempdir", return_value=tmp),
                mock.patch.object(gen.shutil, "which", return_value=None),
                mock.patch.object(gen, "select_mihomo_asset", return_value=url),
                mock.patch.object(gen, "download_file", return_value=Path(tmp) / "archive.gz"),
                mock.patch.object(gen, "extract_mihomo_binary", return_value=extracted),
                mock.patch.object(gen, "looks_like_binary", return_value=True),
                mock.patch.dict(gen.MIHOMO_ASSET_SHA256, {url: "a" * 64}, clear=True),
            ):
                binary = gen.find_or_install_mihomo()
            self.assertEqual(binary.read_bytes(), b"new verified engine")
            install_dir = binary.parent
            expected_digest = hashlib.sha256(b"new verified engine").hexdigest()
            self.assertEqual(
                (install_dir / "binary-sha256.txt").read_text(encoding="utf-8"),
                expected_digest,
            )
            self.assertEqual(
                (install_dir / "asset-url.txt").read_text(encoding="utf-8"),
                url,
            )

    def test_cancelled_fetch_does_not_start_request(self):
        cancel_event = gen.Event()
        cancel_event.set()
        with (
            mock.patch.object(gen.requests, "get") as get,
            self.assertRaises(gen.FetchCancelled),
        ):
            gen.fetch_text("https://example.com/cancelled", cancel_event=cancel_event)
        get.assert_not_called()

    def test_oversized_source_is_not_retried(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"x" * 6
                yield b"y" * 6

        with (
            mock.patch.object(gen.requests, "get", return_value=FakeResponse()) as get,
            mock.patch.object(gen, "MAX_SOURCE_BYTES", 10),
            mock.patch.object(gen.time, "sleep") as sleep,
            self.assertRaises(gen.SourcePolicyError),
        ):
            gen.fetch_text("https://example.com/oversized", retries=3)
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
