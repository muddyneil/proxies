from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import quote

import requests
import yaml
from yaml.events import AliasEvent

VERSION = "v7"
OUTPUT_PATH = Path("output/clash.yaml")


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable.

    Invalid values fall back to the default to avoid crashing outside main().
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[WARN] invalid integer for {name}={raw!r}; using default {default}")
        return default
    if value < 0:
        print(f"[WARN] negative integer for {name}={raw!r}; using default {default}")
        return default
    return value


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment variable, falling back on invalid input."""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    print(f"[WARN] invalid boolean for {name}={raw!r}; using default {default}")
    return default


TEST_URL = "https://cp.cloudflare.com/generate_204"
TEST_URLS = (
    TEST_URL,
    "http://www.gstatic.com/generate_204",
    "https://connectivitycheck.android.com/generate_204",
)
SOURCE_TIMEOUT = 25
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_YAML_ALIASES = 100
MAX_YAML_DEPTH = 50
MAX_PROXY_STRUCTURE_NODES = 10_000
LATENCY_TIMEOUT_MS = 5000


def _max_latency_pass_ms() -> int:
    """Node survival latency cap; env value 0 falls back to the timeout."""
    value = _env_int("FREE_PROXY_AIRPORT_MAX_LATENCY_MS", 2000)
    return value or LATENCY_TIMEOUT_MS


def _probe_times() -> int:
    """Probe rounds per node, bounded to avoid runaway runtime."""
    value = max(1, _env_int("FREE_PROXY_AIRPORT_PROBE_TIMES", 3))
    if value > 10:
        print(f"[WARN] FREE_PROXY_AIRPORT_PROBE_TIMES={value} exceeds maximum 10; using 10")
        return 10
    return value


MAX_LATENCY_PASS_MS = _max_latency_pass_ms()
PROBE_TIMES = _probe_times()
PUBLISH_MAX_LATENCY_MS = 800
PUBLISH_MAX_JITTER_MS = 300
PUBLISH_MIN_PROBE_TIMES = 3
MAX_RETRIES = 3
PRIMARY_PRIORITY_SECONDS = 3
MAX_WORKERS = _env_int("FREE_PROXY_AIRPORT_MAX_WORKERS", 24)
MAX_CANDIDATES = _env_int("FREE_PROXY_AIRPORT_MAX_CANDIDATES", 500)
# Benchmarks start in bounded batches. If a malformed node prevents Mihomo
# from starting, the failed batch is bisected until only that node is dropped.
BENCHMARK_BATCH_SIZE = 100
BENCHMARK_START_RETRIES = 3
AUTO_FAST_MAX = _env_int("FREE_PROXY_AIRPORT_AUTO_FAST_MAX", 50)
REGION_POOL_MAX = _env_int("FREE_PROXY_AIRPORT_REGION_POOL_MAX", 20)
SKIP_CERT_VERIFY = _env_flag("FREE_PROXY_AIRPORT_SKIP_CERT_VERIFY", False)

SOURCE_GROUPS = [
    {
        "name": "snakem982 proxypool",
        "primary": "https://raw.githubusercontent.com/snakem982/proxypool/main/clash.yaml",
        "fallbacks": [
            "https://raw.githubusercontent.com/snakem982/proxypool/main/source/clash-meta-2.yaml",
            "https://raw.githubusercontent.com/snakem982/proxypool/main/source/clash-meta.yaml",
        ],
    },
    {
        "name": "PuddinCat BestClash",
        "primary": (
            "https://raw.githubusercontent.com/PuddinCat/BestClash/refs/heads/main/proxies.yaml"
        ),
        "fallbacks": [],
    },
    {
        "name": "zhuhaiuk free-nodes",
        "primary": "https://raw.githubusercontent.com/zhuhaiuk/free-nodes/main/clash_config.yaml",
        "fallbacks": [],
    },
]

SUPPORTED_PROXY_TYPES = {
    "ss",
    "ssr",
    "vmess",
    "vless",
    "trojan",
    "hysteria",
    "hysteria2",
    "hy2",
    "tuic",
    "socks5",
    "http",
}

# Upstream YAML is untrusted. Keep only proxy-entry fields understood by the
# supported Mihomo protocols, and cap each retained entry so one source cannot
# inflate generated artifacts or feed arbitrary top-level options to Mihomo.
MAX_PROXY_BYTES = 64 * 1024
ALLOWED_PROXY_FIELDS = frozenset(
    {
        "name",
        "type",
        "server",
        "port",
        "ports",
        "username",
        "password",
        "uuid",
        "token",
        "cipher",
        "alterId",
        "network",
        "tls",
        "sni",
        "servername",
        "alpn",
        "fingerprint",
        "client-fingerprint",
        "fp",
        "flow",
        "encryption",
        "packet-encoding",
        "udp",
        "udp-over-tcp",
        "tfo",
        "mptcp",
        "ip-version",
        "interface-name",
        "routing-mark",
        "smux",
        "ws-opts",
        "http-opts",
        "h2-opts",
        "grpc-opts",
        "reality-opts",
        "host",
        "path",
        "serviceName",
        "pbk",
        "sid",
        "spx",
        "plugin",
        "plugin-opts",
        "protocol",
        "protocol-param",
        "obfs",
        "obfs-param",
        "obfs-password",
        "auth",
        "auth-str",
        "up",
        "down",
        "ca",
        "ca-str",
        "recv-window-conn",
        "recv-window",
        "disable-mtu-discovery",
        "fast-open",
        "hop-interval",
        "congestion-controller",
        "congestion_control",
        "udp-relay-mode",
        "reduce-rtt",
        "heartbeat-interval",
        "request-timeout",
        "max-udp-relay-packet-size",
        # These untrusted certificate flags are retained only long enough for
        # normalize_proxy() to remove them under the central security policy.
        "skip-cert-verify",
        "insecure",
    }
)

# Fields that each proxy type must have before benchmarking.
# Nodes missing any required field are dropped to avoid a single broken node
# canning an entire Mihomo delay run (mihomo may reject the whole config).
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "ss": ("server", "port", "password", "cipher"),
    "ssr": ("server", "port", "cipher", "password"),
    "vmess": ("server", "port", "uuid"),
    "vless": ("server", "port", "uuid"),
    "trojan": ("server", "port", "password"),
    "hysteria": ("server", "port"),
    "hysteria2": ("server", "port", "password"),
    "tuic": ("server", "port", "uuid", "password"),
    "socks5": ("server", "port"),
    "http": ("server", "port"),
}

# Proxy types that terminate TLS; used for skip-cert-verify injection.
TLS_PROXY_TYPES = frozenset({"vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic"})

# Group type contract enforced by validate_config() and update.yml together.
REQUIRED_GROUP_TYPES = {
    "AUTO-FAST": "url-test",
    "HK-POOL": "url-test",
    "JP-POOL": "url-test",
    "US-POOL": "url-test",
    "AI-POOL": "url-test",
    "ALL": "select",
    "FALLBACK": "fallback",
    "PROXY": "select",
}

REQUIRED_GROUPS = (
    "AUTO-FAST",
    "HK-POOL",
    "JP-POOL",
    "US-POOL",
    "AI-POOL",
    "ALL",
    "FALLBACK",
    "PROXY",
)

# Mihomo resolves proxies, groups, and built-ins in the same namespace.
# Reserving these names prevents an upstream node from making the benchmark
# config or final subscription ambiguous/unloadable.
RESERVED_PROXY_NAMES = frozenset(
    (
        *REQUIRED_GROUPS,
        "BENCHMARK",
        "DIRECT",
        "REJECT",
        "REJECT-DROP",
        "PASS",
        "GLOBAL",
        "COMPATIBLE",
    )
)


@dataclass
class ProxyMetric:
    proxy: dict[str, Any]
    latency: int
    region: str
    health_score: float
    pass_count: int = PROBE_TIMES
    jitter_ms: int = 0


class SourcePolicyError(RuntimeError):
    """A deterministic source-policy rejection that must not be retried."""


class FetchCancelled(RuntimeError):
    """A source request cancelled after another URL produced a result."""


def fetch_text(
    url: str,
    retries: int = MAX_RETRIES,
    timeout: float = SOURCE_TIMEOUT,
    cancel_event: Event | None = None,
) -> str:
    headers = {
        "User-Agent": f"free-proxy-airport/{VERSION} (+https://github.com/)",
        "Accept": "text/plain, text/yaml, application/yaml, */*",
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise FetchCancelled(f"fetch cancelled: {url}")
        try:
            # Context manager guarantees the streaming response is closed even
            # when a mid-body error aborts the read.
            with requests.get(url, headers=headers, timeout=timeout, stream=True) as response:
                response.raise_for_status()
                chunks = []
                received = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if cancel_event is not None and cancel_event.is_set():
                        raise FetchCancelled(f"fetch cancelled: {url}")
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > MAX_SOURCE_BYTES:
                        raise SourcePolicyError(
                            f"source response exceeds {MAX_SOURCE_BYTES} bytes"
                        )
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
        except (FetchCancelled, SourcePolicyError):
            raise
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500:
                # Client errors (e.g. 404) do not heal with retries; fail
                # fast instead of burning the backoff budget on a dead URL.
                raise RuntimeError(f"failed to fetch {url}: HTTP {status}") from exc
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def maybe_base64_decode(text: str) -> str:
    compact = "".join(text.split())
    if not compact or len(compact) % 4 != 0:
        return text
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        return text
    try:
        decoded = base64.b64decode(compact, validate=True).decode("utf-8")
    except Exception:
        return text
    return decoded if "proxies:" in decoded or "://" in decoded else text


class LimitedSafeLoader(yaml.SafeLoader):
    """Safe YAML loader with bounded aliases and composition depth."""

    def __init__(self, stream: Any) -> None:
        self._alias_count = 0
        self._compose_depth = 0
        super().__init__(stream)

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            self._alias_count += 1
            if self._alias_count > MAX_YAML_ALIASES:
                raise yaml.YAMLError(f"YAML alias limit exceeded ({MAX_YAML_ALIASES})")
        self._compose_depth += 1
        try:
            if self._compose_depth > MAX_YAML_DEPTH:
                raise yaml.YAMLError(f"YAML nesting limit exceeded ({MAX_YAML_DEPTH})")
            return super().compose_node(parent, index)
        finally:
            self._compose_depth -= 1


def _safe_load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=LimitedSafeLoader)


def load_yaml_document(text: str) -> Any:
    try:
        return _safe_load_yaml(maybe_base64_decode(text))
    except yaml.YAMLError as exc:
        print(f"[WARN] YAML document parse failed: {exc}")
        return None


def extract_proxy_block(text: str) -> list[Any]:
    lines = maybe_base64_decode(text).splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if re.match(r"^proxies\s*:\s*$", line):
            start = index
            break
    if start is None:
        return []

    block: list[str] = []
    for line in lines[start + 1 :]:
        if (
            line
            and not line.startswith((" ", "\t", "-"))
            and re.match(r"^[A-Za-z0-9_-]+\s*:", line)
        ):
            break
        block.append(line)

    try:
        parsed = _safe_load_yaml("proxies:\n" + "\n".join(block))
    except yaml.YAMLError as exc:
        print(f"[WARN] proxy block parse failed: {exc}")
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("proxies"), list):
        return parsed["proxies"]
    return []


def extract_proxies(text: str) -> list[dict[str, Any]]:
    document = load_yaml_document(text)
    if isinstance(document, dict):
        proxies = document.get("proxies", [])
    elif isinstance(document, list):
        proxies = document
    else:
        proxies = []

    if not proxies:
        proxies = extract_proxy_block(text)

    clean: list[dict[str, Any]] = []
    for proxy in proxies:
        if isinstance(proxy, dict):
            clean.append(dict(proxy))
    return clean


def _fetch_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch a source concurrently while briefly preferring its primary URL.

    All URLs start together, but a primary result arriving within the short
    priority window wins. After that window, the first non-empty result wins,
    avoiding the long delays caused by sequential fallback requests.
    """
    urls = expand_source_urls(source)
    if not urls:
        return []
    executor = ThreadPoolExecutor(max_workers=min(4, len(urls)))
    cancel_event = Event()
    try:
        futures = [
            executor.submit(_fetch_one_url, source["name"], url, cancel_event) for url in urls
        ]
        try:
            primary = futures[0].result(timeout=PRIMARY_PRIORITY_SECONDS)
        except TimeoutError:
            primary = []
        if primary:
            cancel_event.set()
            executor.shutdown(wait=False, cancel_futures=True)
            return primary

        for future in as_completed(futures):
            # _fetch_one_url never raises; failures already return [] with a log.
            found = future.result()
            if found:
                cancel_event.set()
                executor.shutdown(wait=False, cancel_futures=True)
                return found
    finally:
        cancel_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
    return []


def _fetch_one_url(
    source_name: str,
    url: str,
    cancel_event: Event | None = None,
) -> list[dict[str, Any]]:
    """Fetch and parse one source URL; failures are reported, not raised."""
    try:
        found = extract_proxies(fetch_text(url, cancel_event=cancel_event))
        print(f"[OK] source={source_name} proxies={len(found)} url={url}")
        return found
    except FetchCancelled:
        return []
    except Exception as exc:
        print(f"[WARN] source={source_name} skipped url={url} error={exc}")
        return []


def collect_proxies() -> tuple[int, list[dict[str, Any]]]:
    """Collect and sanitize candidates from the active source groups.

    Temporarily disabled sources retained for possible future reactivation:
    - openRunner clash-freenode:
      https://raw.githubusercontent.com/openRunner/clash-freenode/main/sub.yaml
      (currently returns 404 from the configured URLs)
    - Flikify Free-Node:
      https://raw.githubusercontent.com/Flikify/Free-Node/main/clash.yaml
      (currently returns an empty proxy list, including its fallbacks)
    - free-clash-v2ray GitHub Pages:
      https://free-clash-v2ray.github.io/uploads/latest.yaml
      (large source with near-zero publishable yield in the latest snapshot)
    - dongchengjie airport:
      https://raw.githubusercontent.com/dongchengjie/airport/refs/heads/main/
      subs/merged/tested_within.yaml
      (large source with low publishable yield in the latest snapshot)
    """
    per_source: list[list[dict[str, Any]]] = []
    total = 0
    with ThreadPoolExecutor(max_workers=min(8, len(SOURCE_GROUPS))) as executor:
        futures = {
            executor.submit(_fetch_source, source): index
            for index, source in enumerate(SOURCE_GROUPS)
        }
        ordered: list[list[dict[str, Any]] | None] = [None] * len(SOURCE_GROUPS)
        for future in as_completed(futures):
            index = futures[future]
            try:
                ordered[index] = future.result()
            except Exception as exc:
                # A structurally broken source (e.g. a missing "primary" key)
                # must degrade to an empty result instead of aborting the
                # whole run; logs stay deterministic in SOURCE_GROUPS order.
                print(f"[WARN] source={SOURCE_GROUPS[index].get('name', '?')} failed: {exc}")
                ordered[index] = []
        for found in ordered:
            if found is None:
                continue
            total += len(found)
            if found:
                per_source.append(found)

    sanitized = sanitize_interleaved(per_source)
    if MAX_CANDIDATES > 0 and len(sanitized) > MAX_CANDIDATES:
        print(f"[WARN] limiting candidates from {len(sanitized)} to {MAX_CANDIDATES}")
        sanitized = sanitized[:MAX_CANDIDATES]
    return total, sanitized


def expand_source_urls(source: dict[str, Any]) -> list[str]:
    urls = [str(source["primary"])]
    for item in source.get("fallbacks", []):
        if item == "discover:free-clash-v2ray":
            urls.extend(discover_free_clash_v2ray_urls())
        else:
            urls.append(str(item))
    return unique_ordered(urls)


_DISCOVER_CACHE: dict[str, list[str]] = {}


def discover_free_clash_v2ray_urls() -> list[str]:
    cached = _DISCOVER_CACHE.get("urls")
    if cached is not None:
        return cached
    readme_url = (
        "https://raw.githubusercontent.com/free-clash-v2ray/"
        "free-clash-v2ray.github.io/main/README.md"
    )
    try:
        text = fetch_text(readme_url, retries=2, timeout=12)
    except Exception as exc:
        print(f"[WARN] free-clash-v2ray discovery failed: {exc}")
        return []
    pattern = r"https://free-clash-v2ray\.github\.io/uploads/\d{4}/\d{2}/[0-9]-\d{8}\.yaml"
    result = unique_ordered(re.findall(pattern, text))[:8]
    # Cache only successful discoveries so a transient failure retries on the
    # next call instead of permanently leaving the discover fallback empty.
    if result:
        _DISCOVER_CACHE["urls"] = result
    return result


def unique_ordered(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _sanitize_one(
    raw: dict[str, Any],
    index: int,
    seen_fingerprints: set[str],
    seen_names: set[str],
) -> dict[str, Any] | None:
    """Normalize one proxy and enforce global dedup / name uniqueness."""
    proxy = normalize_proxy(raw, index)
    if not proxy:
        return None

    try:
        fingerprint = proxy_fingerprint(proxy)
    except (TypeError, ValueError, RecursionError):
        return None
    if fingerprint in seen_fingerprints:
        return None
    seen_fingerprints.add(fingerprint)

    base_name = str(proxy["name"]).strip() or f"node-{index}"
    name = base_name
    suffix = 2
    while name in seen_names:
        name = f"{base_name}-{suffix}"
        suffix += 1
    proxy["name"] = name
    seen_names.add(name)
    return proxy


def sanitize_interleaved(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Sanitize several per-source lists, walking them round-robin.

    Round-robin order means a single flooded source cannot crowd out the
    other sources before the MAX_CANDIDATES truncation happens later.
    """
    seen_fingerprints: set[str] = set()
    seen_names: set[str] = set(RESERVED_PROXY_NAMES)
    result: list[dict[str, Any]] = []
    index = 0
    for round_items in zip_longest(*groups):
        for raw in round_items:
            if raw is None:
                continue
            index += 1
            proxy = _sanitize_one(raw, index, seen_fingerprints, seen_names)
            if proxy:
                result.append(proxy)
    return result


def _proxy_structure_within_limits(value: Any) -> bool:
    """Bound alias expansion before JSON serialization of an untrusted node."""
    active: set[int] = set()
    visited = 0

    def visit(item: Any, depth: int) -> bool:
        nonlocal visited
        visited += 1
        if visited > MAX_PROXY_STRUCTURE_NODES or depth > MAX_YAML_DEPTH:
            return False
        if not isinstance(item, (dict, list, tuple, set)):
            return True
        identity = id(item)
        if identity in active:
            return False
        active.add(identity)
        try:
            if isinstance(item, dict):
                return all(
                    isinstance(key, str) and visit(key, depth + 1) and visit(child, depth + 1)
                    for key, child in item.items()
                )
            return all(visit(child, depth + 1) for child in item)
        finally:
            active.remove(identity)

    try:
        return visit(value, 0)
    except RecursionError:
        return False


def normalize_proxy(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    proxy = {
        key: value
        for key, value in raw.items()
        if key in ALLOWED_PROXY_FIELDS and value is not None
    }
    proxy_type = str(proxy.get("type", "")).lower().strip()
    if proxy_type not in SUPPORTED_PROXY_TYPES:
        return None

    if proxy_type == "hy2":
        proxy_type = "hysteria2"
    proxy["type"] = proxy_type

    missing = [
        field for field in REQUIRED_FIELDS.get(proxy_type, ()) if proxy.get(field) in (None, "")
    ]
    if missing:
        return None

    name = str(proxy.get("name", "")).strip() or f"node-{index}"
    server = str(proxy.get("server", "")).strip()
    if not server:
        return None
    if proxy_type == "ssr" and ":" in server:
        # The SSR protocol defines no IPv6 host form; bracketed payloads are
        # unparseable by SSR clients, so drop such nodes outright.
        return None

    raw_port = proxy.get("port")
    if isinstance(raw_port, bool) or (isinstance(raw_port, float) and not raw_port.is_integer()):
        return None
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None

    proxy["name"] = name
    proxy["server"] = server
    proxy["port"] = port
    # Certificate-verification flags are controlled centrally: drop any value
    # declared by the (untrusted) source and let the global SKIP_CERT_VERIFY
    # opt-in decide. Without this, a source could silently disable TLS
    # verification in the published subscription (README: security-sensitive,
    # default off).
    proxy.pop("skip-cert-verify", None)
    proxy.pop("insecure", None)
    if not _proxy_structure_within_limits(proxy):
        return None
    try:
        serialized = json.dumps(proxy, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return None
    if len(serialized) > MAX_PROXY_BYTES:
        return None
    return proxy


def proxy_fingerprint(proxy: dict[str, Any]) -> str:
    identity = {key: value for key, value in proxy.items() if key != "name"}
    payload = json.dumps(identity, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_free_port(retries: int = 3) -> int:
    """Pick a free localhost port, retrying if another process wins the race."""
    last_error: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"no free local port available: {last_error}")


def looks_like_binary(path: Path) -> bool:
    """Check the magic bytes so a corrupt/archive file is never used as the engine."""
    try:
        with path.open("rb") as file:
            magic = file.read(4)
    except Exception:
        return False
    if os.name == "nt":
        return magic[:2] == b"MZ"
    if sys.platform == "darwin":
        return magic in {
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
            b"\xfe\xed\xfa\xce",
            b"\xfe\xed\xfa\xcf",
        }
    return magic[:4] == b"\x7fELF"


def make_executable(path: Path) -> None:
    """Add the executable bit on POSIX.

    Windows has no such concept; executability is ensured by the magic byte
    check.
    """
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _cached_binary_matches(binary: Path, expected_sha256: str) -> bool:
    """Verify that a cached engine still matches its recorded digest."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        return False
    try:
        with binary.open("rb") as file:
            actual_sha256 = hashlib.file_digest(file, "sha256").hexdigest()
    except OSError:
        return False
    return actual_sha256 == expected_sha256.lower()


def find_or_install_mihomo() -> Path:
    for name in ("mihomo", "clash-meta", "clash"):
        found = shutil.which(name)
        if found:
            print(f"[OK] using proxy engine: {found}")
            return Path(found)

    install_dir = Path(tempfile.gettempdir()) / "free-proxy-airport-mihomo"
    install_dir.mkdir(parents=True, exist_ok=True)
    binary = install_dir / ("mihomo.exe" if os.name == "nt" else "mihomo")
    if binary.exists():
        if not looks_like_binary(binary):
            print(f"[WARN] cached proxy engine is corrupt; re-downloading: {binary}")
            binary.unlink()
        elif not _cached_binary_matches(binary, _cached_binary_sha256(install_dir)):
            print(f"[WARN] cached proxy engine digest mismatch; re-downloading: {binary}")
            binary.unlink()
        elif _needs_engine_refresh(install_dir):
            print(f"[INFO] cached proxy engine is outdated; preparing replacement: {binary}")
        else:
            print(f"[OK] using cached proxy engine: {binary}")
            return binary

    url = select_mihomo_asset()
    print(f"[INFO] downloading proxy engine: {url}")
    staged_binary = install_dir / f".{binary.name}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with tempfile.TemporaryDirectory(
            prefix="free-proxy-airport-install-",
            dir=install_dir,
        ) as staging_name:
            staging_dir = Path(staging_name)
            archive = download_file(url, staging_dir)
            extracted = extract_mihomo_binary(archive, staging_dir)
            make_executable(extracted)
            if not looks_like_binary(extracted):
                raise RuntimeError(f"extracted proxy engine is not a valid binary: {extracted}")
            shutil.copy2(extracted, staged_binary)
            make_executable(staged_binary)
            with staged_binary.open("rb") as file:
                binary_sha256 = hashlib.file_digest(file, "sha256").hexdigest()
            os.replace(staged_binary, binary)
    finally:
        staged_binary.unlink(missing_ok=True)
    _record_cached_asset(
        install_dir,
        url,
        MIHOMO_ASSET_SHA256[url],
        binary_sha256,
    )
    return binary


def _cached_asset_url(install_dir: Path) -> str:
    """The asset URL recorded when the cached engine was last downloaded."""
    try:
        return (install_dir / "asset-url.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _cached_asset_sha256(install_dir: Path) -> str:
    try:
        return (install_dir / "asset-sha256.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _cached_binary_sha256(install_dir: Path) -> str:
    try:
        return (install_dir / "binary-sha256.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _record_cached_asset(
    install_dir: Path,
    url: str,
    asset_sha256: str,
    binary_sha256: str,
) -> None:
    try:
        _atomic_write_text(install_dir / "asset-url.txt", url)
        _atomic_write_text(install_dir / "asset-sha256.txt", asset_sha256)
        _atomic_write_text(install_dir / "binary-sha256.txt", binary_sha256)
    except OSError:
        pass


def _needs_engine_refresh(install_dir: Path) -> bool:
    """True when the cached engine is stale relative to the current release.

    Freshness is derived from the asset URL and official archive SHA-256
    recorded at download time; cached binary integrity is checked separately.
    A pre-upgrade cache without all markers is refreshed.
    If the freshness check itself fails (no network), keep a previously
    verified cached engine instead of blocking an otherwise offline-capable run.
    """
    cached_url = _cached_asset_url(install_dir)
    cached_sha256 = _cached_asset_sha256(install_dir)
    if not cached_url or not cached_sha256:
        return True
    try:
        current_url = select_mihomo_asset()
    except Exception as exc:
        print(f"[WARN] engine freshness check failed ({exc}); using cached proxy engine")
        return False
    current_sha256 = MIHOMO_ASSET_SHA256.get(current_url, "")
    if cached_url == current_url and cached_sha256 == current_sha256:
        return False
    print(f"[INFO] cached engine {cached_url} -> {current_url}")
    return True


MIHOMO_REPO = "MetaCubeX/mihomo"
MIHOMO_ASSET_SHA256: dict[str, str] = {}
MIHOMO_MIRRORS = (
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
)


def mihomo_platform_tokens() -> tuple[str, list[str]]:
    """Return (os_token, arch_tokens) for the current platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        os_token = "darwin"
    elif system == "linux":
        os_token = "linux"
    elif system == "windows":
        os_token = "windows"
    else:
        raise RuntimeError(f"unsupported OS for Mihomo download: {system}")
    if machine in {"x86_64", "amd64"}:
        arch_tokens = ["amd64"]
    elif machine in {"arm64", "aarch64"}:
        arch_tokens = ["arm64"]
    else:
        raise RuntimeError(f"unsupported architecture for Mihomo download: {machine}")
    return os_token, arch_tokens


def mihomo_asset_score(name: str) -> int:
    """Ordinal score over matching assets: compatible first, then non-go120.

    The "compatible" variant is built for broader CPU compatibility; assets
    tagged with the go1.20 toolchain marker are de-prioritised.
    """
    score = 0
    if "compatible" in name:
        score += 10
    if "go120" not in name:
        score += 2
    return score


def filter_mihomo_assets(names: list[str]) -> list[str]:
    """Keep asset names matching the current platform, best scoring first."""
    os_token, arch_tokens = mihomo_platform_tokens()
    candidates: list[tuple[int, str]] = []
    for raw in names:
        name = str(raw).lower()
        if os_token not in name:
            continue
        if not any(token in name for token in arch_tokens):
            continue
        if not name.endswith((".gz", ".zip")):
            continue
        if name.endswith(".tar.gz"):
            # extract_mihomo_binary only handles single-binary .gz / .zip;
            # tarballs would fail late without a retry on the next candidate.
            continue
        candidates.append((mihomo_asset_score(name), raw))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [name for _, name in candidates]


def mihomo_asset_url(tag: str, name: str) -> str:
    return f"https://github.com/{MIHOMO_REPO}/releases/download/{tag}/{name}"


def _first_reachable_asset(tag: str, matched: list[str]) -> str | None:
    """First candidate asset that passes the HEAD/Range availability probe."""
    for name in matched:
        url = mihomo_asset_url(tag, name)
        if mihomo_asset_available(url):
            return url
    return None


def mihomo_asset_available(url: str, timeout: int = 15) -> bool:
    """HEAD precheck may be rejected/rate-limited by the CDN.

    On failure, fall back to a Range probe (HTTP 200/206 means downloadable).
    """
    headers = {"User-Agent": f"free-proxy-airport/{VERSION}"}
    try:
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        if response.status_code == 200:
            return True
    except Exception:
        pass
    try:
        with requests.get(
            url,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        ) as response:
            if response.status_code not in (200, 206):
                return False
            # Consume at most one byte so a mirror that ignores Range and
            # returns 200 does not get fully buffered into memory.
            next(response.iter_content(chunk_size=1), None)
            return True
    except Exception:
        return False


def _github_headers(user_agent: str = "free-proxy-airport") -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def select_mihomo_asset() -> str:
    tag: str | None = None
    matched: list[str] = []

    # 1) Preferred: GitHub API for the latest release.
    api_url = f"https://api.github.com/repos/{MIHOMO_REPO}/releases/latest"
    try:
        response = requests.get(
            api_url,
            headers=_github_headers(),
            timeout=SOURCE_TIMEOUT,
        )
        if response.status_code != 200:
            print(
                f"[WARN] GitHub API lookup failed (HTTP {response.status_code}); "
                "retrying via release page"
            )
        else:
            data = response.json()
            if not isinstance(data, dict):
                print(
                    "[WARN] GitHub API returned an unexpected payload; retrying via release page"
                )
            else:
                api_tag = str(data.get("tag_name", ""))
                assets = [asset for asset in data.get("assets", []) if isinstance(asset, dict)]
                names = [str(asset.get("name", "")) for asset in assets]
                digest_by_name = {}
                for asset in assets:
                    digest = str(asset.get("digest", ""))
                    if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                        digest_by_name[str(asset.get("name", ""))] = digest.removeprefix(
                            "sha256:"
                        ).lower()
                api_matched = [
                    name for name in filter_mihomo_assets(names) if name in digest_by_name
                ]
                if not re.fullmatch(r"v[\w.+-]+", api_tag):
                    # An unusable tag must fall through to the release-page
                    # path instead of failing at the final guard below.
                    print(
                        "[WARN] GitHub API returned an unexpected release tag; "
                        "retrying via release page"
                    )
                elif not names:
                    print(
                        "[WARN] GitHub API returned an empty asset list; retrying via release page"
                    )
                elif not api_matched:
                    print(
                        "[WARN] GitHub API had no matching Mihomo asset with SHA-256; "
                        "retrying via release page"
                    )
                else:
                    tag = api_tag
                    matched = api_matched
                    for name in matched:
                        MIHOMO_ASSET_SHA256[mihomo_asset_url(tag, name)] = digest_by_name[name]
    except Exception as exc:
        print(f"[WARN] GitHub API lookup failed ({exc}); retrying via release page")

    # Availability-check API candidates up front; when none is reachable the
    # release-page path still gets a chance instead of failing the install.
    if tag and matched:
        url = _first_reachable_asset(tag, matched)
        if url:
            return url
        print("[WARN] matched GitHub API assets are not reachable; retrying via release page")
        tag = None
        matched = []

    # 2) Fallback: resolve the latest tag via redirect, then scrape the release page.
    #    github.com HTML pages are not subject to the unauthenticated API rate limit.
    if not matched:
        try:
            release_url = f"https://github.com/{MIHOMO_REPO}/releases/latest"
            response = requests.get(
                release_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=SOURCE_TIMEOUT,
                allow_redirects=False,
            )
            location = response.headers.get("Location", "")
            if response.status_code in (301, 302) and location:
                tag = location.rstrip("/").rsplit("/", 1)[-1]
            if not tag or not re.fullmatch(r"v[\w.+-]+", tag):
                raise RuntimeError(f"unexpected Mihomo release tag: {tag}")
            page_url = f"https://github.com/{MIHOMO_REPO}/releases/expanded_assets/{tag}"
            html = fetch_text(page_url, retries=2, timeout=12)
            # IGNORECASE so asset names containing uppercase letters are kept.
            names = re.findall(r"mihomo-[a-z0-9.+-]+?\.(?:gz|zip)", html, re.IGNORECASE)
            matched = filter_mihomo_assets(unique_ordered(names))
            if not matched:
                raise RuntimeError(f"no matching Mihomo release asset found in {tag}")
        except Exception as exc:
            print(f"[WARN] release page discovery failed ({exc})")

    if not tag or not matched:
        system = platform.system().lower()
        machine = platform.machine().lower()
        raise RuntimeError(f"no matching Mihomo release asset found for {system}/{machine}")

    url = _first_reachable_asset(tag, matched)
    if url:
        if url in MIHOMO_ASSET_SHA256:
            return url
        raise RuntimeError(
            "matching Mihomo asset found, but the official GitHub API SHA-256 is unavailable"
        )

    raise RuntimeError(f"matched Mihomo assets are not reachable: {matched[:3]}")


MIN_ARCHIVE_SIZE = 1024 * 1024

# Upper bound for the downloaded archive and its decompressed contents so a
# hostile mirror cannot fill the disk (mihomo archives are tens of MB).
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


def archive_looks_valid(path: Path) -> bool:
    """Reject a downloaded archive that a CDN/mirror served as an error page.

    A mirror may answer HTTP 200 with an HTML error page instead of the real
    archive. This validates by minimum size plus magic bytes so the download
    can be retried through the remaining mirrors.
    """
    try:
        if path.stat().st_size < MIN_ARCHIVE_SIZE:
            return False
        with path.open("rb") as file:
            magic = file.read(4)
    except Exception:
        return False
    if path.name.endswith(".gz"):
        return magic[:2] == b"\x1f\x8b"
    if path.name.endswith(".zip"):
        return magic[:2] == b"PK"
    return True


def _download_attempt_urls(url: str) -> list[str]:
    """Direct URL first; third-party mirrors require an explicit opt-in.

    FREE_PROXY_AIRPORT_DISABLE_MIRRORS defaults to true. Setting it to false
    enables mirrors, but every downloaded archive still has to match the
    SHA-256 digest returned by the official GitHub Release API.
    """
    attempts = [url]
    if not _env_flag("FREE_PROXY_AIRPORT_DISABLE_MIRRORS", True):
        attempts.extend(f"{mirror}{url}" for mirror in MIHOMO_MIRRORS)
    return attempts


def download_file(url: str, directory: Path) -> Path:
    target = directory / Path(url.split("?")[0]).name
    attempts = _download_attempt_urls(url)
    headers = {"User-Agent": f"free-proxy-airport/{VERSION} (+https://github.com/)"}
    last_error: Exception | None = None
    for index, attempt in enumerate(attempts):
        if index:
            time.sleep(2)
        try:
            with requests.get(
                attempt, headers=headers, stream=True, timeout=SOURCE_TIMEOUT
            ) as response:
                response.raise_for_status()
                received = 0
                with target.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            received += len(chunk)
                            if received > MAX_DOWNLOAD_BYTES:
                                raise RuntimeError(f"download exceeds {MAX_DOWNLOAD_BYTES} bytes")
                            file.write(chunk)
            if not archive_looks_valid(target):
                raise RuntimeError(f"downloaded content is not a valid {target.suffix} archive")
            expected_sha256 = MIHOMO_ASSET_SHA256.get(url)
            if not expected_sha256:
                raise RuntimeError("Mihomo asset has no trusted SHA-256 digest")
            with target.open("rb") as file:
                actual_sha256 = hashlib.file_digest(file, "sha256").hexdigest()
            if actual_sha256 != expected_sha256:
                raise RuntimeError(
                    f"Mihomo asset SHA-256 mismatch: expected {expected_sha256}, "
                    f"got {actual_sha256}"
                )
            return target
        except Exception as exc:
            target.unlink(missing_ok=True)
            last_error = exc
            if index == 0 and len(attempts) > 1:
                print(f"[WARN] direct download failed ({exc}); trying mirrors")
    raise RuntimeError(f"failed to download {url}: {last_error}")


def _safe_zip_extract(archive: Path, directory: Path) -> None:
    """Extract a zip archive, rejecting entries that escape the target dir.

    Guards against Zip Slip (names containing ".." or absolute paths) so a
    malicious archive can never write outside ``directory``.
    """
    root = os.path.realpath(directory)
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            name = member.filename.replace("\\", "/")
            if os.path.isabs(name) or ".." in name.split("/"):
                raise RuntimeError(f"unsafe zip entry: {member.filename}")
            target = os.path.realpath(os.path.join(root, name))
            if os.path.commonpath([root, target]) != root:
                raise RuntimeError(f"unsafe zip entry: {member.filename}")
        total = sum(member.file_size for member in zipped.infolist())
        if total > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"decompressed archive exceeds {MAX_DOWNLOAD_BYTES} bytes")
        zipped.extractall(directory)


def extract_mihomo_binary(archive: Path, directory: Path) -> Path:
    if archive.suffix == ".gz" and not archive.name.endswith(".tar.gz"):
        target = directory / archive.name[:-3]
        written = 0
        with gzip.open(archive, "rb") as source, target.open("wb") as dest:
            while True:
                chunk = source.read(1024 * 512)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(f"decompressed archive exceeds {MAX_DOWNLOAD_BYTES} bytes")
                dest.write(chunk)
        return target

    if archive.suffix == ".zip":
        _safe_zip_extract(archive, directory)
        candidates = [
            path
            for path in directory.rglob("*")
            if path.is_file()
            and path != archive
            and path.suffix.lower() not in {".zip", ".gz"}
            and "mihomo" in path.name.lower()
        ]
        if not candidates:
            raise RuntimeError(f"no mihomo binary found inside archive: {archive}")
        for path in candidates:
            if path.name.lower() in {"mihomo.exe", "mihomo"}:
                return path
        return candidates[0]

    raise RuntimeError(f"unsupported Mihomo archive: {archive}")


def _maybe_inject_skip_cert_verify(proxy: dict[str, Any]) -> dict[str, Any]:
    """Optionally disable TLS cert verification on TLS-class proxies.

    Gated behind FREE_PROXY_AIRPORT_SKIP_CERT_VERIFY (default off) so the user
    subscription keeps certificate checks unless explicitly opted in. Applied
    consistently to the benchmark config and the final config so a node that
    passes testing is also usable by the end user.
    """
    if not SKIP_CERT_VERIFY:
        # Opt-in off: drop any upstream-declared flag instead of letting an
        # untrusted source disable TLS verification silently. normalize_proxy
        # strips it at ingestion; this covers reuse/edge paths.
        if proxy.get("skip-cert-verify"):
            proxy = dict(proxy)
            proxy.pop("skip-cert-verify", None)
        return proxy
    proxy_type = str(proxy.get("type", "")).lower()
    if proxy_type in TLS_PROXY_TYPES:
        proxy = dict(proxy)
        proxy["skip-cert-verify"] = True
    return proxy


def write_benchmark_config(
    path: Path,
    proxies: list[dict[str, Any]],
    controller_port: int,
    mixed_port: int,
) -> None:
    prepared = [_maybe_inject_skip_cert_verify(proxy) for proxy in proxies]
    names = [str(proxy["name"]) for proxy in prepared]
    config = {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": True,
        "unified-delay": True,
        "tcp-concurrent": True,
        "external-controller": f"127.0.0.1:{controller_port}",
        "proxies": prepared,
        "proxy-groups": [
            {
                "name": "BENCHMARK",
                "type": "select",
                "proxies": names or ["DIRECT"],
            }
        ],
        "rules": ["MATCH,BENCHMARK"],
    }
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _engine_environment() -> dict[str, str]:
    """Environment for the external engine, excluding GitHub credentials."""
    environment = dict(os.environ)
    environment.pop("GITHUB_TOKEN", None)
    environment.pop("ACTIONS_ID_TOKEN_REQUEST_TOKEN", None)
    environment.pop("ACTIONS_ID_TOKEN_REQUEST_URL", None)
    return environment


class BenchmarkConfigError(RuntimeError):
    """A Mihomo process rejected a benchmark config before becoming ready."""


class BenchmarkPortError(RuntimeError):
    """A benchmark listener port was occupied before Mihomo could bind it."""


def _is_address_conflict(detail: str) -> bool:
    normalized = detail.lower()
    return "address already in use" in normalized or "only one usage" in normalized


def _validate_benchmark_config(engine: Path, config_path: Path) -> None:
    """Separate proxy config rejection from runtime engine failures."""
    try:
        result = subprocess.run(
            [str(engine), "-t", "-d", str(config_path.parent), "-f", str(config_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_engine_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to validate Mihomo benchmark config: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if _is_address_conflict(detail):
            raise BenchmarkPortError(f"Mihomo benchmark port conflict: {detail}")
        raise BenchmarkConfigError(f"Mihomo rejected benchmark config: {detail}")


def wait_for_controller(
    controller_url: str,
    process: subprocess.Popen[str],
    stderr_path: Path,
) -> None:
    for _ in range(60):
        if process.poll() is not None:
            try:
                detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Mihomo exited before the controller became ready{suffix}")
        try:
            response = requests.get(f"{controller_url}/version", timeout=1)
            if response.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Mihomo controller did not become ready")


def _stop_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _benchmark_batch(engine: Path, proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    """Benchmark one batch, retrying listener-port races with fresh ports."""
    if not proxies:
        return []
    last_port_error: Exception | None = None
    with tempfile.TemporaryDirectory(prefix="free-proxy-airport-") as temp_name:
        temp_dir = Path(temp_name)
        config_path = temp_dir / "benchmark.yaml"
        stderr_path = temp_dir / "mihomo.stderr.log"

        for _ in range(BENCHMARK_START_RETRIES):
            controller_port = find_free_port()
            mixed_port = find_free_port()
            while mixed_port == controller_port:
                mixed_port = find_free_port()
            controller_url = f"http://127.0.0.1:{controller_port}"
            write_benchmark_config(
                config_path,
                proxies,
                controller_port,
                mixed_port,
            )
            try:
                _validate_benchmark_config(engine, config_path)
            except BenchmarkPortError as exc:
                last_port_error = exc
                continue

            with stderr_path.open("w", encoding="utf-8") as stderr_file:
                process = subprocess.Popen(
                    [str(engine), "-d", str(temp_dir), "-f", str(config_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    env=_engine_environment(),
                )
                try:
                    try:
                        wait_for_controller(controller_url, process, stderr_path)
                    except RuntimeError as exc:
                        if _is_address_conflict(str(exc)):
                            last_port_error = exc
                            continue
                        raise
                    return run_delay_tests(controller_url, proxies)
                finally:
                    _stop_process(process)

    raise BenchmarkPortError(
        f"Mihomo listener ports remained unavailable after "
        f"{BENCHMARK_START_RETRIES} attempts: {last_port_error}"
    )


def _benchmark_batch_isolated(
    engine: Path,
    proxies: list[dict[str, Any]],
) -> list[ProxyMetric]:
    """Benchmark a batch, bisecting startup failures down to bad nodes."""
    try:
        return _benchmark_batch(engine, proxies)
    except BenchmarkConfigError as exc:
        if len(proxies) <= 1:
            name = proxies[0].get("name", "?") if proxies else "?"
            print(f"[DROP] benchmark config rejected node={name}: {exc}")
            return []
        middle = len(proxies) // 2
        print(f"[WARN] benchmark batch failed ({exc}); isolating {len(proxies)} nodes")
        return _benchmark_batch_isolated(engine, proxies[:middle]) + _benchmark_batch_isolated(
            engine, proxies[middle:]
        )


def benchmark_proxies(proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    """Benchmark candidates in batches and isolate invalid configurations.

    A malformed proxy that prevents Mihomo from starting triggers recursive
    bisection, preserving healthy nodes from the same original batch.
    """
    if not proxies:
        return []
    if PROBE_TIMES < PUBLISH_MIN_PROBE_TIMES:
        print(
            f"[WARN] PROBE_TIMES={PROBE_TIMES} is below publication minimum "
            f"{PUBLISH_MIN_PROBE_TIMES}; skipping benchmark"
        )
        return []

    engine = find_or_install_mihomo()
    metrics: list[ProxyMetric] = []
    for offset in range(0, len(proxies), BENCHMARK_BATCH_SIZE):
        batch = proxies[offset : offset + BENCHMARK_BATCH_SIZE]
        batch_metrics = _benchmark_batch_isolated(engine, batch)
        metrics.extend(batch_metrics)
    return metrics


def run_delay_tests(controller_url: str, proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    workers = max(1, min(MAX_WORKERS, len(proxies)))
    metrics: list[ProxyMetric] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(test_single_proxy, controller_url, proxy): proxy for proxy in proxies
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            proxy = futures[future]
            try:
                metric = future.result()
            except Exception as exc:
                print(f"[DROP] {proxy.get('name')} failed: {exc}")
                continue
            if metric:
                metrics.append(metric)
            if completed % 25 == 0 or completed == len(futures):
                print(f"[INFO] tested {completed}/{len(futures)} kept={len(metrics)}")
    metrics.sort(key=lambda item: item.health_score, reverse=True)
    return metrics


def _probe_latency(controller_url: str, name: str, url: str) -> int | None:
    """Single-delay request against one URL; None on failure or too slow.

    The controller timeout is capped at MAX_LATENCY_PASS_MS so dead nodes are
    resolved quickly instead of waiting for the full 5000ms budget.
    """
    endpoint = (
        f"{controller_url}/proxies/{quote(name, safe='')}/delay"
        f"?timeout={MAX_LATENCY_PASS_MS}&url={quote(url, safe='')}"
    )
    try:
        response = requests.get(endpoint, timeout=(MAX_LATENCY_PASS_MS / 1000) + 1)
        if response.status_code != 200:
            return None
        data = response.json()
        latency = int(data.get("delay", 0))
    except Exception:
        return None
    if latency <= 0 or latency > MAX_LATENCY_PASS_MS:
        return None
    return latency


def _probe_round_latency(controller_url: str, name: str) -> int | None:
    """One delay round across all test URLs; None unless every URL passes.

    Requiring all URLs eliminates nodes that whitelist a single domain and are
    thus not broadly usable for the end user.
    """
    latencies = []
    for url in TEST_URLS:
        latency = _probe_latency(controller_url, name, url)
        if latency is None:
            return None
        latencies.append(latency)
    return max(latencies)


def test_single_proxy(controller_url: str, proxy: dict[str, Any]) -> ProxyMetric | None:
    """Probe a proxy over several rounds, keeping only stable nodes.

    Multi-round probing filters out flaky nodes that pass a single-shot check
    but repeatedly time out in the user's client.
    """
    name = str(proxy["name"])
    rounds = []
    for _ in range(PROBE_TIMES):
        latency = _probe_round_latency(controller_url, name)
        if latency is None:
            return None
        rounds.append(latency)
    rounds.sort()
    middle = len(rounds) // 2
    if len(rounds) % 2 == 0:
        latency = (rounds[middle - 1] + rounds[middle]) // 2
    else:
        latency = rounds[middle]
    region = detect_region(name)
    pass_count = len(rounds)
    stability = pass_count / max(PROBE_TIMES, 1)
    score = health_score(name, latency, region, stability)
    return ProxyMetric(
        proxy=proxy,
        latency=latency,
        region=region,
        health_score=score,
        pass_count=pass_count,
        jitter_ms=rounds[-1] - rounds[0],
    )


def detect_region(name: str) -> str:
    text = name.lower()
    patterns = {
        "HK": ("regex:\\bhk\\b", "hong kong", "香港", "\U0001f1ed\U0001f1f0"),
        "JP": ("regex:\\bjp\\b", "japan", "日本", "\U0001f1ef\U0001f1f5"),
        "US": (
            "regex:\\bus\\b",
            "regex:\\busa\\b",
            "united states",
            "america",
            "美国",
            "美國",
            "\U0001f1fa\U0001f1f8",
        ),
        "SG": ("regex:\\bsg\\b", "singapore", "新加坡", "\U0001f1f8\U0001f1ec"),
    }
    for region, tokens in patterns.items():
        for token in tokens:
            if token.startswith("regex:"):
                if re.search(token.removeprefix("regex:"), text):
                    return region
                continue
            if token in text:
                return region
    return "OTHER"


# Bound for the deterministic per-name tie-break. The smallest real score
# difference is the latency step at the high end; the tie-break stays far
# below that so it cannot overturn latency or stability ordering.
TIE_BREAK_SCALE = 1e-9


def health_score(name: str, latency: int, region: str, stability: float = 1.0) -> float:
    """Score a node by latency and probe stability.

    Region is accepted for API compatibility and grouping, but it does not
    affect quality ranking because names come from untrusted upstreams. A
    tiny deterministic per-name tie-break keeps exact ties stable.
    """
    del region
    latency_score = 0.6 * (1000.0 / max(latency, 1))
    tie_break = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16)
    tie_break /= 0xFFFFFFFF
    tie_break *= TIE_BREAK_SCALE
    return latency_score + stability * 0.1 + tie_break


def publishable_metrics(metrics: list[ProxyMetric]) -> list[ProxyMetric]:
    """High-quality live nodes eligible for published subscriptions."""
    selected = [
        metric
        for metric in metrics
        if metric.latency <= PUBLISH_MAX_LATENCY_MS
        and PROBE_TIMES >= PUBLISH_MIN_PROBE_TIMES
        and metric.pass_count == PROBE_TIMES
        and metric.jitter_ms <= PUBLISH_MAX_JITTER_MS
    ]
    return sorted(
        selected,
        key=lambda item: (item.latency, item.jitter_ms, -item.health_score),
    )


def low_latency_pool(metrics: list[ProxyMetric]) -> list[str]:
    if not metrics:
        # Defensive only: build_config always supplies non-empty metrics.
        return ["DIRECT"]
    ordered = sorted(metrics, key=lambda item: (item.latency, -item.health_score))
    size = min(max(3, len(ordered) // 5), 30, len(ordered))
    return [item.proxy["name"] for item in ordered[:size]]


def _top_names(metrics: list[ProxyMetric], limit: int) -> list[str]:
    if limit > 0:
        metrics = metrics[:limit]
    return [item.proxy["name"] for item in metrics]


def names_for_region(metrics: list[ProxyMetric], region: str) -> list[str]:
    names = [item.proxy["name"] for item in metrics if item.region == region]
    if names:
        return names[:REGION_POOL_MAX] if REGION_POOL_MAX > 0 else names
    if metrics:
        return [item.proxy["name"] for item in metrics[: min(5, len(metrics))]]
    return ["DIRECT"]


def build_direct_fallback_metric() -> ProxyMetric:
    """Degraded-mode placeholder metric.

    ``direct`` is not a legal type inside Clash's ``proxies:`` list, so the
    placeholder is a socks5 entry to a closed localhost port: it parses and
    validates, and simply never connects.
    """
    proxy = {
        "name": "DIRECT-FALLBACK",
        "type": "socks5",
        "server": "127.0.0.1",
        "port": 1,
    }
    return ProxyMetric(proxy=proxy, latency=LATENCY_TIMEOUT_MS, region="OTHER", health_score=0.0)


def load_existing_metrics() -> list[ProxyMetric]:
    """Do not treat historical output as quality evidence.

    Historical files contain no per-round measurements, so recovered nodes are
    deliberately marked ineligible for the live publication quality gate.
    """
    if not OUTPUT_PATH.exists():
        return []
    try:
        data = _safe_load_yaml(OUTPUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        return []
    metrics: list[ProxyMetric] = []
    for proxy in data["proxies"]:
        if not isinstance(proxy, dict):
            continue
        name = str(proxy.get("name", ""))
        region = detect_region(name)
        metrics.append(
            ProxyMetric(
                proxy={
                    k: v for k, v in proxy.items() if k not in ("skip-cert-verify", "insecure")
                },
                latency=LATENCY_TIMEOUT_MS,
                region=region,
                health_score=health_score(name, LATENCY_TIMEOUT_MS, region),
                pass_count=0,
                jitter_ms=PUBLISH_MAX_JITTER_MS + 1,
            )
        )
    if metrics and all(item.proxy.get("name") == "DIRECT-FALLBACK" for item in metrics):
        print("[WARN] previous output was itself a DIRECT-FALLBACK degraded config")
    return metrics


def build_config(metrics: list[ProxyMetric]) -> dict[str, Any]:
    if not metrics:
        metrics = [build_direct_fallback_metric()]
    metrics = sorted(metrics, key=lambda item: (item.latency, item.jitter_ms, -item.health_score))

    proxies = [_maybe_inject_skip_cert_verify(item.proxy) for item in metrics]
    auto_fast_names = _top_names(metrics, AUTO_FAST_MAX)
    all_nodes = [item.proxy["name"] for item in metrics]
    hk_names = names_for_region(metrics, "HK")
    jp_names = names_for_region(metrics, "JP")
    us_names = names_for_region(metrics, "US")
    ai_names = low_latency_pool(metrics)

    url_test_groups = []
    for name, names in (
        ("AUTO-FAST", auto_fast_names),
        ("HK-POOL", hk_names),
        ("JP-POOL", jp_names),
        ("US-POOL", us_names),
        ("AI-POOL", ai_names),
    ):
        url_test_groups.append(
            {
                "name": name,
                "type": "url-test",
                "proxies": names,
                "url": TEST_URL,
                "interval": 120,
                "lazy": True,
            }
        )

    return {
        "mixed-port": 7890,
        "allow-lan": _env_flag("FREE_PROXY_AIRPORT_ALLOW_LAN", False),
        "mode": "rule",
        "log-level": "info",
        "ipv6": True,
        "unified-delay": True,
        "tcp-concurrent": True,
        "generated-by": f"free-proxy-airport-{VERSION}",
        "generated-at": os.getenv(
            "FREE_PROXY_AIRPORT_GENERATED_AT",
            datetime.now(UTC).isoformat(),
        ),
        "proxies": proxies,
        "proxy-groups": url_test_groups
        + [
            {
                "name": "ALL",
                "type": "select",
                "proxies": all_nodes,
            },
            {
                "name": "FALLBACK",
                "type": "fallback",
                "proxies": ["AUTO-FAST", "HK-POOL", "JP-POOL", "US-POOL"],
                "url": TEST_URL,
                "interval": 120,
            },
            {
                "name": "PROXY",
                "type": "select",
                "proxies": ["AUTO-FAST", "FALLBACK", "ALL"],
            },
        ],
        "rules": [
            "DOMAIN-SUFFIX,openai.com,AI-POOL",
            "DOMAIN-SUFFIX,chatgpt.com,AI-POOL",
            "DOMAIN-SUFFIX,claude.ai,AI-POOL",
            "DOMAIN-SUFFIX,anthropic.com,AI-POOL",
            "GEOIP,CN,DIRECT",
            "MATCH,PROXY",
        ],
    }


def _atomic_write_text(path: Path, content: str) -> None:
    """Write via a temp file + os.replace so readers never see partial output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        file.write(content)
        temp_name = Path(file.name)
    os.replace(temp_name, path)


def write_config(config: dict[str, Any]) -> None:
    _atomic_write_text(
        OUTPUT_PATH,
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
    )


REQUIRED_RULES = (
    "DOMAIN-SUFFIX,openai.com,AI-POOL",
    "DOMAIN-SUFFIX,chatgpt.com,AI-POOL",
    "DOMAIN-SUFFIX,claude.ai,AI-POOL",
    "DOMAIN-SUFFIX,anthropic.com,AI-POOL",
    "GEOIP,CN,DIRECT",
    "MATCH,PROXY",
)


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config.get("proxies"), list) or not config["proxies"]:
        raise RuntimeError("generated config has no proxies")
    for index, proxy in enumerate(config["proxies"], start=1):
        if not isinstance(proxy, dict) or not proxy.get("name"):
            raise RuntimeError(f"generated config has an invalid proxy entry #{index}")
        proxy_type = str(proxy.get("type", "")).lower().strip()
        if proxy_type not in SUPPORTED_PROXY_TYPES:
            raise RuntimeError(
                f"generated config proxy #{index} has unsupported type: {proxy.get('type')!r}"
            )
        required_type = "hysteria2" if proxy_type == "hy2" else proxy_type
        missing_fields = [
            field
            for field in REQUIRED_FIELDS.get(required_type, ())
            if proxy.get(field) in (None, "")
        ]
        if missing_fields:
            raise RuntimeError(
                f"generated config proxy #{index} is missing required fields: {missing_fields}"
            )
        server = str(proxy.get("server", "")).strip()
        if not server:
            raise RuntimeError(f"generated config proxy #{index} has no server")
        raw_port = proxy.get("port")
        if isinstance(raw_port, bool) or (
            isinstance(raw_port, float) and not raw_port.is_integer()
        ):
            raise RuntimeError(f"generated config proxy #{index} has an invalid port")
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            raise RuntimeError(f"generated config proxy #{index} has an invalid port") from None
        if port <= 0 or port > 65535:
            raise RuntimeError(f"generated config proxy #{index} has an invalid port")
    proxy_names = [str(proxy["name"]) for proxy in config["proxies"]]
    duplicate_proxy_names = sorted(
        name for name in set(proxy_names) if proxy_names.count(name) > 1
    )
    if duplicate_proxy_names:
        raise RuntimeError(f"generated config has duplicate proxy names: {duplicate_proxy_names}")
    reserved_proxy_names = sorted(set(proxy_names) & RESERVED_PROXY_NAMES)
    if reserved_proxy_names:
        raise RuntimeError(f"generated config proxies use reserved names: {reserved_proxy_names}")

    groups = config.get("proxy-groups", [])
    if not isinstance(groups, list) or not all(isinstance(group, dict) for group in groups):
        raise RuntimeError("generated config proxy-groups must be a list of mappings")
    for group in groups:
        references = group.get("proxies")
        if (
            not isinstance(references, list)
            or not references
            or not all(isinstance(reference, str) and reference for reference in references)
        ):
            raise RuntimeError(
                f"generated config group {group.get('name')!r} proxies must be "
                "a non-empty string list"
            )
    group_name_list = [str(group.get("name")) for group in groups if group.get("name")]
    duplicate_group_names = sorted(
        name for name in set(group_name_list) if group_name_list.count(name) > 1
    )
    if duplicate_group_names:
        raise RuntimeError(f"generated config has duplicate group names: {duplicate_group_names}")
    group_names = set(group_name_list)
    missing = [name for name in REQUIRED_GROUPS if name not in group_names]
    if missing:
        raise RuntimeError(f"generated config missing groups: {missing}")
    empty_groups = [
        name
        for name in REQUIRED_GROUPS
        if not next(
            (group.get("proxies") for group in groups if group.get("name") == name),
            None,
        )
    ]
    if empty_groups:
        raise RuntimeError(f"generated config empty groups: {empty_groups}")
    for name, expected_type in REQUIRED_GROUP_TYPES.items():
        group = next(
            (group for group in groups if group.get("name") == name),
            None,
        )
        if group is None:
            continue
        if group.get("type") != expected_type:
            raise RuntimeError(
                f"generated config group {name} has type {group.get('type')!r}; "
                f"expected {expected_type!r}"
            )
    # Every entry referenced by a group must resolve to a known proxy or a
    # known group; a dangling reference silently makes that group unusable.
    # "DIRECT" is a mihomo built-in and a legal group member.
    resolvable = set(proxy_names) | group_names | {"DIRECT"}
    for group in groups:
        for ref in group["proxies"]:
            if ref not in resolvable:
                raise RuntimeError(
                    f"generated config group {group.get('name')!r} references "
                    f"unknown node/group: {ref!r}"
                )
    rules = config.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(rule, str) and rule for rule in rules):
        raise RuntimeError("generated config rules must be a list of non-empty strings")
    for rule in REQUIRED_RULES:
        if rule not in rules:
            raise RuntimeError(f"generated config missing rule: {rule}")
    if not rules or rules[-1] != "MATCH,PROXY":
        raise RuntimeError("generated config must end with MATCH,PROXY")
    if any(rule.startswith("MATCH,") for rule in rules[:-1]):
        raise RuntimeError("generated config has a catch-all MATCH rule before the final rule")


def print_summary(total_nodes: int, candidates: int, metrics: list[ProxyMetric]) -> None:
    hk_count = sum(1 for item in metrics if item.region == "HK")
    jp_count = sum(1 for item in metrics if item.region == "JP")
    us_count = sum(1 for item in metrics if item.region == "US")
    avg_latency = round(sum(item.latency for item in metrics) / len(metrics), 2) if metrics else 0
    print(f"[SUMMARY] total_nodes={total_nodes}")
    print(f"[SUMMARY] legal_candidates={candidates}")
    print(f"[SUMMARY] passed_latency_test={len(metrics)}")
    print(f"[SUMMARY] region_HK={hk_count} region_JP={jp_count} region_US={us_count}")
    print(f"[SUMMARY] avg_latency_ms={avg_latency}")
    print(f"[SUMMARY] output={OUTPUT_PATH}")


def main() -> None:
    total_nodes, candidates = collect_proxies()
    metrics: list[ProxyMetric] = []

    if candidates:
        try:
            metrics = benchmark_proxies(candidates)
        except Exception as exc:
            print(f"[WARN] real latency benchmark unavailable: {exc}")

    if metrics:
        live_count = len(metrics)
        metrics = publishable_metrics(metrics)
        print(
            f"[INFO] quality gate kept={len(metrics)}/{live_count} "
            f"latency<={PUBLISH_MAX_LATENCY_MS}ms "
            f"jitter<={PUBLISH_MAX_JITTER_MS}ms "
            f"pass={PROBE_TIMES}/{PROBE_TIMES}"
        )

    if not metrics and _env_flag("FREE_PROXY_AIRPORT_REQUIRE_LIVE", False):
        raise RuntimeError("no high-quality live nodes passed and live results are required")

    if not metrics:
        metrics = load_existing_metrics()
        if metrics:
            metrics = publishable_metrics(metrics)
            if metrics:
                print(
                    "[WARN] no live nodes passed; reusing previous high-quality "
                    "output as degraded fallback"
                )
            else:
                print("[WARN] previous output has no live quality evidence; ignoring it")

    if not metrics:
        metrics = [build_direct_fallback_metric()]
        print("[WARN] no live or previous nodes; using DIRECT-FALLBACK degraded config")

    metrics.sort(key=lambda item: (item.latency, item.jitter_ms, -item.health_score))

    # Build the Clash config.
    config = build_config(metrics)
    validate_config(config)
    write_config(config)
    print_summary(total_nodes, len(candidates), metrics)

    # Region distribution of nodes.
    region_stats: dict[str, int] = {}
    for m in metrics:
        region_stats[m.region] = region_stats.get(m.region, 0) + 1
    print("\n=== Region distribution ===")
    for region, count in sorted(region_stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {region}: {count}")
    print(f"  Total nodes: {len(metrics)}")


def validate_with_mihomo(config_path: Path) -> None:
    """Ask the real Mihomo binary to validate the final generated config."""
    engine = find_or_install_mihomo()
    try:
        result = subprocess.run(
            [str(engine), "-t", "-f", str(config_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_engine_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to run Mihomo config validation: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Mihomo rejected {config_path}: {detail}")
    print(f"[OK] Mihomo accepted config: {config_path}")


def _load_and_validate_config(config_path: Path) -> None:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read config file {config_path}: {exc}") from exc
    data = _safe_load_yaml(text)
    if not isinstance(data, dict):
        raise RuntimeError(f"config file is empty or not a mapping: {config_path}")
    validate_config(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate free proxy subscriptions.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate a Clash config without fetching or benchmarking.",
    )
    parser.add_argument(
        "--validate-with-mihomo",
        action="store_true",
        help="Validate a Clash config with the real Mihomo binary.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=OUTPUT_PATH,
        help="Config path used by validation modes (default: output/clash.yaml).",
    )
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.validate_only or args.validate_with_mihomo:
        try:
            _load_and_validate_config(args.config)
            if args.validate_with_mihomo:
                validate_with_mihomo(args.config)
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print("[OK] existing config is valid")
    else:
        try:
            main()
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            raise
