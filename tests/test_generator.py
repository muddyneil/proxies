"""Unit tests for the pure functions in generator.py.

Run from the repository root:

    uv run python -m unittest discover -s tests
"""

import base64
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Tests are expected to run from the repository root, e.g.:
#   uv run python -m unittest discover -s tests
import generator as gen


class ProxyToUriTest(unittest.TestCase):
    """proxy_to_uri / per-protocol converters."""
    def setUp(self) -> None:
        self.base = {"name": "node", "server": "1.2.3.4", "port": 443}

    def decode_vmess(self, proxy: dict) -> dict:
        uri = gen.proxy_to_uri(proxy)
        self.assertTrue(uri.startswith("vmess://"))
        payload = uri[len("vmess://"):]
        return json.loads(base64.b64decode(payload).decode("utf-8"))

    def test_ss(self):
        proxy = {
            **self.base, "type": "ss", "port": 8388,
            "password": "pw", "cipher": "aes-256-gcm",
        }
        self.assertTrue(gen.proxy_to_uri(proxy).startswith("ss://"))

    def test_ssr(self):
        proxy = {
            **self.base, "type": "ssr", "port": 1001, "password": "z",
            "cipher": "aes-256-cfb", "protocol": "origin", "obfs": "plain",
        }
        self.assertTrue(gen.proxy_to_uri(proxy).startswith("ssr://"))

    def test_vmess_tls_boolean_true(self):
        proxy = {**self.base, "type": "vmess", "uuid": "u", "tls": True}
        self.assertEqual(self.decode_vmess(proxy)["tls"], "tls")

    def test_vmess_tls_string_true(self):
        proxy = {**self.base, "type": "vmess", "uuid": "u", "tls": "true"}
        self.assertEqual(self.decode_vmess(proxy)["tls"], "tls")

    def test_vmess_tls_string_bogus(self):
        proxy = {**self.base, "type": "vmess", "uuid": "u", "tls": "bogus"}
        self.assertEqual(self.decode_vmess(proxy)["tls"], "")

    def test_vless_tls_false_string(self):
        proxy = {**self.base, "type": "vless", "uuid": "u", "tls": "false"}
        uri = gen.proxy_to_uri(proxy)
        self.assertIn("security=none", uri)

    def test_vless_reality_kept(self):
        proxy = {**self.base, "type": "vless", "uuid": "u", "tls": "reality"}
        self.assertIn("security=reality", gen.proxy_to_uri(proxy))

    def test_trojan(self):
        proxy = {**self.base, "type": "trojan", "password": "pw", "sni": "x.com"}
        self.assertTrue(gen.proxy_to_uri(proxy).startswith("trojan://"))

    def test_hysteria_and_hysteria2_scheme(self):
        h1 = {**self.base, "type": "hysteria", "auth": "a"}
        self.assertTrue(gen.proxy_to_uri(h1).startswith("hysteria://"))
        h2 = {**self.base, "type": "hy2", "password": "p"}
        self.assertTrue(gen.proxy_to_uri(h2).startswith("hysteria2://"))

    def test_tuic(self):
        proxy = {**self.base, "type": "tuic", "uuid": "u", "password": "p"}
        self.assertTrue(gen.proxy_to_uri(proxy).startswith("tuic://"))

    def test_http_credentials_with_password_only(self):
        proxy = {**self.base, "type": "http", "port": 8080, "password": "secret"}
        uri = gen.proxy_to_uri(proxy)
        self.assertTrue(uri.startswith("http://:secret@1.2.3.4:8080"))

    def test_http_credentials_with_user_and_password(self):
        proxy = {**self.base, "type": "http", "port": 8080, "username": "u", "password": "s"}
        self.assertTrue(gen.proxy_to_uri(proxy).startswith("http://u:s@1.2.3.4:8080"))

    def test_socks5_credentials_with_password_only(self):
        proxy = {**self.base, "type": "socks5", "port": 1080, "password": "secret"}
        self.assertTrue(gen.proxy_to_uri(proxy).startswith("socks5://:secret@1.2.3.4:1080"))

    def test_missing_port_rejected(self):
        proxy = {**self.base, "type": "ss", "port": None, "password": "pw"}
        self.assertEqual(gen.proxy_to_uri(proxy), "")

    def test_invalid_port_rejected(self):
        proxy = {**self.base, "type": "ss", "port": 70000, "password": "pw"}
        self.assertEqual(gen.proxy_to_uri(proxy), "")

    def test_missing_server_rejected(self):
        proxy = {"type": "ss", "name": "n", "port": 443, "password": "pw"}
        self.assertEqual(gen.proxy_to_uri(proxy), "")

    def test_unsupported_type_rejected(self):
        proxy = {**self.base, "type": "unknown"}
        self.assertEqual(gen.proxy_to_uri(proxy), "")


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

    def test_region_bonus(self):
        self.assertEqual(gen.region_bonus("HK"), 3)
        self.assertEqual(gen.region_bonus("JP"), 3)
        self.assertEqual(gen.region_bonus("SG"), 3)
        self.assertEqual(gen.region_bonus("US"), 2)
        self.assertEqual(gen.region_bonus("OTHER"), 1)


class BuildAndValidateTest(unittest.TestCase):
    def _metric(self, mod, name, latency, region):
        return mod.ProxyMetric(
            proxy={"name": name, "type": "ss", "server": "1.2.3.4", "port": 8388},
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
        self.assertEqual(config["proxies"][0]["type"], "direct")

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
        config["proxy-groups"] = [
            g for g in config["proxy-groups"] if g["name"] != "PROXY"
        ]
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


class CleanupAndFingerprintTest(unittest.TestCase):
    def test_clean_sni(self):
        self.assertEqual(gen.clean_sni("https://a.com/path#frag"), "a.com")
        self.assertEqual(gen.clean_sni(None), "")

    def test_alpn_value(self):
        self.assertEqual(gen.alpn_value(["h2", "http/1.1"]), "h2,http/1.1")
        self.assertEqual(gen.alpn_value("h3"), "h3")

    def test_fingerprint_stable(self):
        proxy = {"type": "ss", "server": "x", "port": 1}
        self.assertEqual(gen.proxy_fingerprint(proxy), gen.proxy_fingerprint(proxy))

    def test_b64url(self):
        self.assertNotIn("=", gen._b64url("a" * 6))


class ProbePolicyTest(unittest.TestCase):
    """Multi-round / multi-URL survival policy."""
    def setUp(self) -> None:
        self.proxy = {"name": "n", "type": "ss", "server": "1.2.3.4", "port": 8388}

    def test_flaky_node_dropped_when_below_pass_min(self):
        # Defaults: PROBE_TIMES=3, PROBE_PASS_MIN=2. Two rounds fail -> dropped.
        with mock.patch.object(
            gen, "_probe_round_latency", side_effect=[100, None, None]
        ):
            self.assertIsNone(gen.test_single_proxy("http://x", self.proxy))

    def test_stable_node_kept_with_median_latency(self):
        with mock.patch.object(
            gen, "_probe_round_latency", side_effect=[200, 100, 30]
        ):
            metric = gen.test_single_proxy("http://x", self.proxy)
        self.assertIsNotNone(metric)
        self.assertEqual(metric.latency, 100)  # median of [30, 100, 200]

    def test_even_rounds_use_average_median(self):
        original = gen.PROBE_TIMES
        try:
            gen.PROBE_TIMES = 2
            with mock.patch.object(
                gen, "_probe_round_latency", side_effect=[100, 200]
            ):
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


class TruncationTest(unittest.TestCase):
    """AUTO-FAST is a curated top-N pool; ALL holds every node."""
    def setUp(self) -> None:
        self.metrics = [
            gen.ProxyMetric(
                proxy={"name": f"n{i}", "type": "ss", "server": "x", "port": 8080},
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


class SubscriptionTest(unittest.TestCase):
    """Subscriptions must stay non-empty and valid base64 when degraded."""

    def test_degraded_mode_writes_notice(self):
        content = gen.shadowrocket_subscription_content([])
        self.assertTrue(content)
        decoded = base64.b64decode(content, validate=True).decode("utf-8")
        self.assertIn("DIRECT-FALLBACK", decoded)

    def test_real_nodes_keep_uri_content(self):
        proxy = {
            "name": "n",
            "type": "ss",
            "server": "1.2.3.4",
            "port": 8388,
            "password": "pw",
            "cipher": "aes-256-gcm",
        }
        expected = gen.generate_shadowrocket_sub([proxy])
        self.assertTrue(expected)
        self.assertEqual(gen.shadowrocket_subscription_content([proxy]), expected)


class EnvConfigTest(unittest.TestCase):
    """Environment-variable config semantics."""

    def test_max_latency_zero_falls_back_to_timeout(self):
        with mock.patch.dict(
            os.environ, {"FREE_PROXY_AIRPORT_MAX_LATENCY_MS": "0"}
        ):
            self.assertEqual(gen._max_latency_pass_ms(), gen.LATENCY_TIMEOUT_MS)
        with mock.patch.dict(
            os.environ, {"FREE_PROXY_AIRPORT_MAX_LATENCY_MS": "1234"}
        ):
            self.assertEqual(gen._max_latency_pass_ms(), 1234)


class FixRegressionTest(unittest.TestCase):
    """Coverage for the 2026-08 review fixes."""

    def test_uri_host_brackets_ipv6(self):
        self.assertEqual(gen._uri_host("2001:db8::1"), "[2001:db8::1]")
        self.assertEqual(gen._uri_host("[2001:db8::1]"), "[2001:db8::1]")
        self.assertEqual(gen._uri_host("example.com"), "example.com")

    def test_ss_uri_with_ipv6_server(self):
        proxy = {
            "name": "n",
            "type": "ss",
            "server": "2001:db8::1",
            "port": 8388,
            "password": "pw",
            "cipher": "aes-256-gcm",
        }
        self.assertIn("@[2001:db8::1]:8388", gen.proxy_to_uri(proxy))

    def test_normalize_proxy_drops_ipv6_ssr(self):
        self.assertIsNone(gen.normalize_proxy(
            {"type": "ssr", "name": "n", "server": "2001:db8::1",
             "port": 1001, "cipher": "aes-256-cfb", "password": "p"}, 1
        ))
        kept = gen.normalize_proxy(
            {"type": "ssr", "name": "n", "server": "1.2.3.4",
             "port": 1001, "cipher": "aes-256-cfb", "password": "p"}, 2
        )
        self.assertIsNotNone(kept)

    def test_clean_sni_strips_port_and_whitespace(self):
        self.assertEqual(gen.clean_sni("a.com:8443"), "a.com")
        self.assertEqual(gen.clean_sni("  my server.com "), "myserver.com")
        self.assertEqual(gen.clean_sni("2001:db8::1"), "2001:db8::1")

    def test_vless_flow_outside_reality(self):
        proxy = {
            "name": "n",
            "type": "vless",
            "server": "1.2.3.4",
            "port": 443,
            "uuid": "u",
            "tls": True,
            "flow": "xtls-rprx-vision",
        }
        uri = gen.proxy_to_uri(proxy)
        self.assertIn("security=tls", uri)
        self.assertIn("flow=xtls-rprx-vision", uri)

    def test_vmess_grpc_service_name(self):
        proxy = {
            "name": "n",
            "type": "vmess",
            "server": "1.2.3.4",
            "port": 443,
            "uuid": "u",
            "network": "grpc",
            "grpc-opts": {"grpc-service-name": "svc"},
        }
        payload = json.loads(
            base64.b64decode(gen.proxy_to_uri(proxy)[len("vmess://"):])
        )
        self.assertEqual(payload["net"], "grpc")
        self.assertEqual(payload["serviceName"], "svc")

    def test_probe_latency_bad_json_returns_none(self):
        class FakeResponse:
            status_code = 200
            def json(self):
                raise ValueError("not json")
        with mock.patch.object(gen.requests, "get", return_value=FakeResponse()):
            self.assertIsNone(gen._probe_latency("http://x", "n", "http://u"))

    def test_negative_env_int_falls_back(self):
        with mock.patch.dict(os.environ, {"FREE_PROXY_AIRPORT_TOP_N": "-5"}):
            self.assertEqual(gen._env_int("FREE_PROXY_AIRPORT_TOP_N", 20), 20)

    def test_normalize_proxy_rejects_bool_port(self):
        self.assertIsNone(gen.normalize_proxy(
            {"type": "ss", "server": "s", "port": True, "password": "p"}, 1
        ))


    def test_atomic_write_text(self):
        target = Path("test-atomic-write.txt")
        try:
            gen._atomic_write_text(target, "hello")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")
        finally:
            target.unlink(missing_ok=True)


    def test_probe_times_zero_clamped(self):
        with mock.patch.dict(
            os.environ, {"FREE_PROXY_AIRPORT_PROBE_TIMES": "0"}
        ):
            self.assertEqual(gen._probe_times(), 1)
        with mock.patch.dict(
            os.environ, {"FREE_PROXY_AIRPORT_PROBE_PASS_MIN": "0"}
        ):
            self.assertEqual(gen._probe_pass_min(), 1)


class MainFlowTest(unittest.TestCase):
    """Coverage for the main entry path that the old suite never touched."""

    def test_collect_proxies_parallel_path(self):
        proxy = {"name": "n", "type": "ss", "server": "1.2.3.4",
                 "port": 8388, "password": "pw", "cipher": "aes-256-gcm"}
        with mock.patch.object(gen, "_fetch_source", return_value=[proxy]):
            total, sanitized = gen.collect_proxies()
        self.assertEqual(total, len(gen.SOURCE_GROUPS))
        self.assertEqual(len(sanitized), 1)  # identical nodes dedup to one

    def test_main_smoke(self):
        proxy = {"name": "n", "type": "ss", "server": "1.2.3.4",
                 "port": 8388, "password": "pw", "cipher": "aes-256-gcm"}
        metric = gen.ProxyMetric(proxy=proxy, latency=50, region="HK",
                                 health_score=gen.health_score("n", 50, "HK"))
        with mock.patch.object(gen, "collect_proxies", return_value=(1, [proxy])), \
             mock.patch.object(gen, "benchmark_proxies", return_value=[metric]), \
             mock.patch.object(gen, "write_config"), \
             mock.patch.object(gen, "_atomic_write_text"), \
             mock.patch.object(gen, "print_summary"):
            gen.main()  # must not raise and must not touch real outputs

    def test_benchmark_batches_partial_failure(self):
        original = gen.BENCHMARK_BATCH_SIZE
        gen.BENCHMARK_BATCH_SIZE = 1
        engine = Path("fake-mihomo")
        ok = {"name": "ok", "type": "ss", "server": "1.2.3.4",
              "port": 8388, "password": "pw"}
        metric = gen.ProxyMetric(proxy=ok, latency=10, region="HK",
                                 health_score=gen.health_score("ok", 10, "HK"))
        try:
            # Batch 1 fails; batch 2 yields nothing -> overall [] without raising.
            with mock.patch.object(gen, "find_or_install_mihomo",
                                   return_value=engine), \
                 mock.patch.object(gen, "_benchmark_batch", side_effect=[
                     RuntimeError("batch 1 failed"), []]):
                self.assertEqual(gen.benchmark_proxies([ok, ok]), [])
            # Batch 2 failure must not discard batch 1's metrics.
            with mock.patch.object(gen, "find_or_install_mihomo",
                                   return_value=engine), \
                 mock.patch.object(gen, "_benchmark_batch", side_effect=[
                     [metric], RuntimeError("batch 2 failed")]):
                got = gen.benchmark_proxies([ok, ok])
            self.assertEqual(len(got), 1)
        finally:
            gen.BENCHMARK_BATCH_SIZE = original


class CertVerifyPolicyTest(unittest.TestCase):
    """Trojan/TUIC URIs keep TLS verification on by default."""

    def tearDown(self) -> None:
        gen.SKIP_CERT_VERIFY = 0

    def test_trojan_allow_insecure_only_on_opt_in(self):
        base = {"name": "n", "type": "trojan", "server": "1.2.3.4",
                "port": 443, "password": "pw"}
        self.assertNotIn("allowInsecure", gen.proxy_to_uri(base))
        self.assertIn(
            "allowInsecure=1",
            gen.proxy_to_uri({**base, "skip-cert-verify": True}),
        )
        gen.SKIP_CERT_VERIFY = 1
        self.assertIn("allowInsecure=1", gen.proxy_to_uri(base))

    def test_tuic_allow_insecure_only_on_opt_in(self):
        base = {"name": "n", "type": "tuic", "server": "1.2.3.4",
                "port": 443, "uuid": "u", "password": "p"}
        self.assertNotIn("allowInsecure", gen.proxy_to_uri(base))
        self.assertIn(
            "allowInsecure=1",
            gen.proxy_to_uri({**base, "skip-cert-verify": True}),
        )


class UriTransportRegressionTest(unittest.TestCase):
    """Regression coverage for URI transport-field fixes (R3-M1/M2/L1/L6)."""

    def test_vless_ws_reads_ws_opts(self):
        proxy = {
            "name": "n", "type": "vless", "server": "1.2.3.4", "port": 443,
            "uuid": "u", "tls": True, "network": "ws",
            "ws-opts": {"path": "/wspath", "headers": {"Host": "cdn.example.com"}},
        }
        uri = gen.proxy_to_uri(proxy)
        self.assertIn("type=ws", uri)
        self.assertIn("path=%2Fwspath", uri)
        self.assertIn("host=cdn.example.com", uri)

    def test_vless_top_level_path_still_wins(self):
        proxy = {
            "name": "n", "type": "vless", "server": "1.2.3.4", "port": 443,
            "uuid": "u", "tls": True, "network": "ws",
            "path": "/top", "ws-opts": {"path": "/nested"},
        }
        self.assertIn("path=%2Ftop", gen.proxy_to_uri(proxy))

    def test_vmess_ws_reads_ws_opts(self):
        proxy = {
            "name": "n", "type": "vmess", "server": "1.2.3.4", "port": 443,
            "uuid": "u", "tls": True, "network": "ws",
            "ws-opts": {"path": "/wspath", "headers": {"Host": "cdn.example.com"}},
        }
        payload = json.loads(
            base64.b64decode(gen.proxy_to_uri(proxy)[len("vmess://"):])
        )
        self.assertEqual(payload["path"], "/wspath")
        self.assertEqual(payload["host"], "cdn.example.com")

    def test_vmess_alpn_list_joined(self):
        proxy = {
            "name": "n", "type": "vmess", "server": "1.2.3.4", "port": 443,
            "uuid": "u", "alpn": ["h2", "http/1.1"],
        }
        payload = json.loads(
            base64.b64decode(gen.proxy_to_uri(proxy)[len("vmess://"):])
        )
        self.assertEqual(payload["alpn"], "h2,http/1.1")

    def test_ss_obfs_plugin_encoded(self):
        proxy = {
            "name": "n", "type": "ss", "server": "1.2.3.4", "port": 8388,
            "password": "pw", "cipher": "aes-256-gcm",
            "plugin": "obfs", "plugin-opts": {"mode": "http", "host": "bing.com"},
        }
        uri = gen.proxy_to_uri(proxy)
        self.assertIn("?plugin=", uri)
        self.assertIn("obfs-local%3Bobfs%3Dhttp%3Bobfs-host%3Dbing.com", uri)

    def test_ss_v2ray_plugin_encoded(self):
        proxy = {
            "name": "n", "type": "ss", "server": "1.2.3.4", "port": 8388,
            "password": "pw", "cipher": "aes-256-gcm",
            "plugin": "v2ray-plugin",
            "plugin-opts": {"mode": "websocket", "tls": True, "host": "x.com"},
        }
        uri = gen.proxy_to_uri(proxy)
        self.assertIn("v2ray-plugin", uri)

    def test_ss_unknown_plugin_skips_node(self):
        proxy = {
            "name": "n", "type": "ss", "server": "1.2.3.4", "port": 8388,
            "password": "pw", "cipher": "aes-256-gcm", "plugin": "shadowsocksr",
        }
        # An unexpressible plugin must drop the node instead of emitting a
        # dead link.
        self.assertEqual(gen.proxy_to_uri(proxy), "")

    def test_trojan_ws_transport_kept(self):
        proxy = {
            "name": "n", "type": "trojan", "server": "1.2.3.4", "port": 443,
            "password": "pw", "network": "ws",
            "ws-opts": {"path": "/ws", "headers": {"Host": "h.com"}},
        }
        uri = gen.proxy_to_uri(proxy)
        self.assertIn("type=ws", uri)
        self.assertIn("path=%2Fws", uri)
        self.assertIn("host=h.com", uri)

    def test_trojan_plain_tcp_has_no_type_param(self):
        proxy = {
            "name": "n", "type": "trojan", "server": "1.2.3.4", "port": 443,
            "password": "pw",
        }
        self.assertNotIn("type=", gen.proxy_to_uri(proxy))


class HysteriaCertPolicyTest(unittest.TestCase):
    """Hysteria/hysteria2 URIs honour skip-cert-verify like trojan/tuic."""

    def tearDown(self) -> None:
        gen.SKIP_CERT_VERIFY = False

    def _proxy(self, extra: dict | None = None) -> dict:
        proxy = {
            "name": "n", "type": "hysteria2", "server": "1.2.3.4",
            "port": 443, "password": "p",
        }
        if extra:
            proxy.update(extra)
        return proxy

    def test_skip_cert_verify_field_adds_insecure(self):
        proxy = self._proxy({"skip-cert-verify": True})
        self.assertIn("insecure=1", gen.proxy_to_uri(proxy))

    def test_insecure_field_adds_insecure(self):
        self.assertIn("insecure=1", gen.proxy_to_uri(self._proxy({"insecure": True})))

    def test_no_flag_keeps_verification(self):
        self.assertNotIn("insecure", gen.proxy_to_uri(self._proxy()))

    def test_global_opt_in_applies(self):
        gen.SKIP_CERT_VERIFY = True
        self.assertIn("insecure=1", gen.proxy_to_uri(self._proxy()))


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
        with mock.patch.object(
            gen, "mihomo_platform_tokens", return_value=self.LINUX_TOKENS
        ):
            matched = gen.filter_mihomo_assets(names)
        self.assertNotIn(names[0], matched)
        self.assertEqual(matched[0], names[1])  # compatible variant scores best

    def test_scrape_handles_uppercase_asset_names(self):
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
            mock.patch.object(
                gen, "mihomo_platform_tokens", return_value=self.LINUX_TOKENS
            ),
            mock.patch.object(gen, "mihomo_asset_available", return_value=True),
        ):
            url = gen.select_mihomo_asset()
        self.assertIn("Mihomo-Linux-amd64-v1.19.2.gz", url)

    def test_invalid_api_tag_falls_back_to_release_page(self):
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
            mock.patch.object(
                gen, "mihomo_platform_tokens", return_value=self.LINUX_TOKENS
            ),
            mock.patch.object(gen, "mihomo_asset_available", return_value=True),
        ):
            url = gen.select_mihomo_asset()
        self.assertTrue(url.endswith("mihomo-linux-amd64-v1.19.2.gz"))

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
                self.assertRaises(RuntimeError),
            ):
                gen.download_file(
                    "https://example.com/mihomo-windows-amd64-v1.zip", target_dir
                )
        finally:
            shutil.rmtree(target_dir, ignore_errors=True)


class ProbeConfigWarningTest(unittest.TestCase):
    """PASS_MIN above PROBE_TIMES must warn instead of failing silently."""

    def test_pass_min_above_times_warns(self):
        buffer = io.StringIO()
        original_times, original_min = gen.PROBE_TIMES, gen.PROBE_PASS_MIN
        gen.PROBE_TIMES, gen.PROBE_PASS_MIN = 1, 2
        try:
            with (
                mock.patch.object(
                    gen,
                    "find_or_install_mihomo",
                    side_effect=RuntimeError("no engine"),
                ),
                contextlib.redirect_stdout(buffer),
                self.assertRaises(RuntimeError),
            ):
                gen.benchmark_proxies([{"name": "n"}])
        finally:
            gen.PROBE_TIMES, gen.PROBE_PASS_MIN = original_times, original_min
        self.assertIn("[WARN] PROBE_PASS_MIN=2", buffer.getvalue())


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


if __name__ == "__main__":
    unittest.main()
