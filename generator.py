from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import platform
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml

VERSION = "v7"
OUTPUT_PATH = Path("output/clash.yaml")
ROCKET_OUTPUT = Path("output/rocket.txt")
V2RAY_OUTPUT = Path("output/v2ray.txt")


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


TOP_N = _env_int("FREE_PROXY_AIRPORT_TOP_N", 20)
TEST_URL = "http://www.gstatic.com/generate_204"
TEST_URLS = (
    TEST_URL,
    "https://cp.cloudflare.com/generate_204",
    "https://connectivitycheck.android.com/generate_204",
)
SOURCE_TIMEOUT = 25
MAX_SOURCE_BYTES = 8 * 1024 * 1024
LATENCY_TIMEOUT_MS = 5000
def _max_latency_pass_ms() -> int:
    """Node survival latency cap; env value 0 falls back to the timeout."""
    value = _env_int("FREE_PROXY_AIRPORT_MAX_LATENCY_MS", 2000)
    return value or LATENCY_TIMEOUT_MS


def _probe_times() -> int:
    """Probe rounds per node; 0 (or negative) is clamped to 1."""
    return max(1, _env_int("FREE_PROXY_AIRPORT_PROBE_TIMES", 3))


def _probe_pass_min() -> int:
    """Minimum passing rounds; 0 (or negative) is clamped to 1."""
    return max(1, _env_int("FREE_PROXY_AIRPORT_PROBE_PASS_MIN", 2))


MAX_LATENCY_PASS_MS = _max_latency_pass_ms()
PROBE_TIMES = _probe_times()
PROBE_PASS_MIN = _probe_pass_min()
MAX_RETRIES = 3
MAX_WORKERS = _env_int("FREE_PROXY_AIRPORT_MAX_WORKERS", 24)
MAX_CANDIDATES = _env_int("FREE_PROXY_AIRPORT_MAX_CANDIDATES", 500)
# Benchmarks run in batches so one malformed node (or a bad mihomo start)
# only poisons its own batch instead of the entire candidate set.
BENCHMARK_BATCH_SIZE = 100
AUTO_FAST_MAX = _env_int("FREE_PROXY_AIRPORT_AUTO_FAST_MAX", 50)
REGION_POOL_MAX = _env_int("FREE_PROXY_AIRPORT_REGION_POOL_MAX", 20)
SKIP_CERT_VERIFY = _env_int("FREE_PROXY_AIRPORT_SKIP_CERT_VERIFY", 0) == 1

SOURCE_GROUPS = [
    {
        "name": "openRunner clash-freenode",
        "primary": "https://raw.githubusercontent.com/openRunner/clash-freenode/main/sub.yaml",
        "fallbacks": [
            "https://raw.githubusercontent.com/openRunner/clash-freenode/main/clash.yaml",
            "https://raw.githubusercontent.com/openrunner/clash-freenode/main/clash.yaml",
        ],
    },
    {
        "name": "snakem982 proxypool",
        "primary": "https://raw.githubusercontent.com/snakem982/proxypool/main/clash.yaml",
        "fallbacks": [
            "https://raw.githubusercontent.com/snakem982/proxypool/main/source/clash-meta-2.yaml",
            "https://raw.githubusercontent.com/snakem982/proxypool/main/source/clash-meta.yaml",
        ],
    },
    {
        "name": "Flikify Free-Node",
        "primary": "https://raw.githubusercontent.com/Flikify/Free-Node/main/clash.yaml",
        "fallbacks": [
            "https://raw.githubusercontent.com/a2470982985/getNode/main/clash.yaml",
            "https://cdn.jsdelivr.net/gh/a2470982985/getNode@main/clash.yaml",
        ],
    },
    {
        "name": "free-clash-v2ray GitHub Pages",
        "primary": "https://free-clash-v2ray.github.io/uploads/latest.yaml",
        "fallbacks": [
            "discover:free-clash-v2ray",
        ],
    },
    {
        "name": "PuddinCat BestClash",
        "primary": (
            "https://raw.githubusercontent.com/PuddinCat/"
            "BestClash/refs/heads/main/proxies.yaml"
        ),
        "fallbacks": [],
    },
    {
        "name": "dongchengjie airport",
        "primary": (
            "https://raw.githubusercontent.com/dongchengjie/airport/"
            "refs/heads/main/subs/merged/tested_within.yaml"
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

# Fields that each proxy type must have before benchmarking / URI conversion.
# Nodes missing any required field are dropped to avoid a single broken node
# canning an entire Mihomo delay run (mihomo may reject the whole config).
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "ss": ("server", "port", "password"),
    "ssr": ("server", "port", "cipher", "password"),
    "vmess": ("server", "port", "uuid"),
    "vless": ("server", "port", "uuid"),
    "trojan": ("server", "port", "password"),
    "hysteria": ("server", "port"),
    "hysteria2": ("server", "port", "password"),
    "tuic": ("server", "port", "uuid"),
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


@dataclass
class ProxyMetric:
    proxy: dict[str, Any]
    latency: int
    region: str
    health_score: float


def fetch_text(url: str, retries: int = MAX_RETRIES) -> str:
    headers = {
        "User-Agent": f"free-proxy-airport/{VERSION} (+https://github.com/)",
        "Accept": "text/plain, text/yaml, application/yaml, */*",
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            # Context manager guarantees the streaming response is closed even
            # when a mid-body error aborts the read.
            with requests.get(
                url, headers=headers, timeout=SOURCE_TIMEOUT, stream=True
            ) as response:
                response.raise_for_status()
                chunks = []
                received = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > MAX_SOURCE_BYTES:
                        raise RuntimeError(
                            f"source response exceeds {MAX_SOURCE_BYTES} bytes"
                        )
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
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


def load_yaml_document(text: str) -> Any:
    try:
        return yaml.safe_load(maybe_base64_decode(text))
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
    for line in lines[start + 1:]:
        if (
            line
            and not line.startswith((" ", "\t", "-"))
            and re.match(r"^[A-Za-z0-9_-]+\s*:", line)
        ):
            break
        block.append(line)

    try:
        parsed = yaml.safe_load("proxies:\n" + "\n".join(block))
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
    for url in expand_source_urls(source):
        try:
            text = fetch_text(url)
            found = extract_proxies(text)
            print(f"[OK] source={source['name']} proxies={len(found)} url={url}")
            if found:
                return found
        except Exception as exc:
            print(f"[WARN] source={source['name']} skipped url={url} error={exc}")
    return []

def collect_proxies() -> tuple[int, list[dict[str, Any]]]:
    collected: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(SOURCE_GROUPS))) as executor:
        for found in executor.map(_fetch_source, SOURCE_GROUPS):
            collected.extend(found)

    sanitized = sanitize_and_deduplicate(collected)
    if MAX_CANDIDATES > 0 and len(sanitized) > MAX_CANDIDATES:
        print(f"[WARN] limiting candidates from {len(sanitized)} to {MAX_CANDIDATES}")
        sanitized = sanitized[:MAX_CANDIDATES]
    return len(collected), sanitized


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
        text = fetch_text(readme_url)
    except Exception as exc:
        print(f"[WARN] free-clash-v2ray discovery failed: {exc}")
        return []
    pattern = r"https://free-clash-v2ray\.github\.io/uploads/\d{4}/\d{2}/[0-9]-\d{8}\.yaml"
    result = unique_ordered(re.findall(pattern, text))[:8]
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


def sanitize_and_deduplicate(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_fingerprints: set[str] = set()
    seen_names: set[str] = set()
    result: list[dict[str, Any]] = []

    for index, raw in enumerate(proxies, start=1):
        proxy = normalize_proxy(raw, index)
        if not proxy:
            continue

        fingerprint = proxy_fingerprint(proxy)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)

        base_name = str(proxy["name"]).strip() or f"node-{index}"
        name = base_name
        suffix = 2
        while name in seen_names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        proxy["name"] = name
        seen_names.add(name)
        result.append(proxy)
    return result


def normalize_proxy(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    proxy = {key: value for key, value in raw.items() if value is not None}
    proxy_type = str(proxy.get("type", "")).lower().strip()
    if proxy_type not in SUPPORTED_PROXY_TYPES:
        return None

    if proxy_type == "hy2":
        proxy_type = "hysteria2"
    proxy["type"] = proxy_type

    missing = [
        field
        for field in REQUIRED_FIELDS.get(proxy_type, ())
        if proxy.get(field) in (None, "")
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
    if isinstance(raw_port, bool):
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
    return proxy


def proxy_fingerprint(proxy: dict[str, Any]) -> str:
    important = {
        "type": proxy.get("type"),
        "server": proxy.get("server"),
        "port": proxy.get("port"),
        "uuid": proxy.get("uuid"),
        "password": proxy.get("password"),
        "cipher": proxy.get("cipher"),
        "network": proxy.get("network"),
        "tls": proxy.get("tls"),
        "sni": proxy.get("sni"),
        "servername": proxy.get("servername"),
        "flow": proxy.get("flow"),
        "alterId": proxy.get("alterId"),
        "host": proxy.get("host"),
        "path": proxy.get("path"),
        "ws-opts": proxy.get("ws-opts"),
        "http-opts": proxy.get("http-opts"),
        "grpc-opts": proxy.get("grpc-opts"),
        "obfs": proxy.get("obfs"),
        "protocol": proxy.get("protocol"),
        "obfs-param": proxy.get("obfs-param"),
        "protocol-param": proxy.get("protocol-param"),
    }
    payload = json.dumps(important, sort_keys=True, ensure_ascii=True, default=str)
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
        if looks_like_binary(binary):
            print(f"[OK] using cached proxy engine: {binary}")
            return binary
        print(f"[WARN] cached proxy engine is corrupt; re-downloading: {binary}")
        binary.unlink()

    url = select_mihomo_asset()
    print(f"[INFO] downloading proxy engine: {url}")
    archive = download_file(url, install_dir)
    extracted = extract_mihomo_binary(archive, install_dir)
    make_executable(extracted)
    if not looks_like_binary(extracted):
        raise RuntimeError(f"extracted proxy engine is not a valid binary: {extracted}")
    if extracted != binary:
        shutil.copy2(extracted, binary)
        make_executable(binary)
    return binary


MIHOMO_REPO = "MetaCubeX/mihomo"
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
        response = requests.get(
            url,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=timeout,
            allow_redirects=True,
        )
        return response.status_code in (200, 206)
    except Exception:
        return False


def select_mihomo_asset() -> str:
    tag: str | None = None
    matched: list[str] = []

    # 1) Preferred: GitHub API for the latest release.
    api_url = f"https://api.github.com/repos/{MIHOMO_REPO}/releases/latest"
    try:
        response = requests.get(
            api_url,
            headers={"User-Agent": "free-proxy-airport"},
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
                    "[WARN] GitHub API returned an unexpected payload; "
                    "retrying via release page"
                )
            else:
                api_tag = str(data.get("tag_name", ""))
                names = [
                    str(asset.get("name", ""))
                    for asset in data.get("assets", [])
                    if isinstance(asset, dict)
                ]
                api_matched = filter_mihomo_assets(names)
                if not re.fullmatch(r"v[\w.+-]+", api_tag):
                    # An unusable tag must fall through to the release-page
                    # path instead of failing at the final guard below.
                    print(
                        "[WARN] GitHub API returned an unexpected release tag; "
                        "retrying via release page"
                    )
                elif not names:
                    print(
                        "[WARN] GitHub API returned an empty asset list; "
                        "retrying via release page"
                    )
                elif not api_matched:
                    print(
                        "[WARN] GitHub API had no matching Mihomo asset; "
                        "retrying via release page"
                    )
                else:
                    tag = api_tag
                    matched = api_matched
    except Exception as exc:
        print(f"[WARN] GitHub API lookup failed ({exc}); retrying via release page")

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
            html = fetch_text(page_url)
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

    for name in matched:
        url = mihomo_asset_url(tag, name)
        if mihomo_asset_available(url):
            return url

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

def download_file(url: str, directory: Path) -> Path:
    target = directory / Path(url.split("?")[0]).name
    attempts = [url] + [f"{mirror}{url}" for mirror in MIHOMO_MIRRORS]
    last_error: Exception | None = None
    for index, attempt in enumerate(attempts):
        try:
            with requests.get(attempt, stream=True, timeout=SOURCE_TIMEOUT) as response:
                response.raise_for_status()
                received = 0
                with target.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            received += len(chunk)
                            if received > MAX_DOWNLOAD_BYTES:
                                raise RuntimeError(
                                    f"download exceeds {MAX_DOWNLOAD_BYTES} bytes"
                                )
                            file.write(chunk)
            if not archive_looks_valid(target):
                raise RuntimeError(
                    f"downloaded content is not a valid {target.suffix} archive"
                )
            return target
        except Exception as exc:
            target.unlink(missing_ok=True)
            last_error = exc
            if index == 0:
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
                    raise RuntimeError(
                        f"decompressed archive exceeds {MAX_DOWNLOAD_BYTES} bytes"
                    )
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
) -> None:
    prepared = [_maybe_inject_skip_cert_verify(proxy) for proxy in proxies]
    names = [str(proxy["name"]) for proxy in prepared]
    config = {
        "mixed-port": find_free_port(),
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
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


def wait_for_controller(controller_url: str, process: subprocess.Popen[str]) -> None:
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError("Mihomo exited before controller became ready")
        try:
            response = requests.get(f"{controller_url}/version", timeout=1)
            if response.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Mihomo controller did not become ready")


def _benchmark_batch(engine: Path, proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    """Benchmark one batch of proxies against a dedicated Mihomo instance."""
    if not proxies:
        return []
    with tempfile.TemporaryDirectory(prefix="free-proxy-airport-") as temp_name:
        temp_dir = Path(temp_name)
        config_path = temp_dir / "benchmark.yaml"
        controller_port = find_free_port()
        controller_url = f"http://127.0.0.1:{controller_port}"
        write_benchmark_config(config_path, proxies, controller_port)

        process = subprocess.Popen(
            [str(engine), "-d", str(temp_dir), "-f", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for_controller(controller_url, process)
            return run_delay_tests(controller_url, proxies)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def benchmark_proxies(proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    """Benchmark candidates in batches so partial failures don't kill all nodes.

    A malformed proxy that makes one Mihomo instance fail to start only
    poisons its own batch; healthy nodes in other batches survive instead of
    silently degrading the whole run to the previous output.
    """
    if not proxies:
        return []
    if PROBE_PASS_MIN > PROBE_TIMES:
        print(
            f"[WARN] PROBE_PASS_MIN={PROBE_PASS_MIN} exceeds PROBE_TIMES={PROBE_TIMES}; "
            "no node can survive probing"
        )

    engine = find_or_install_mihomo()
    metrics: list[ProxyMetric] = []
    for offset in range(0, len(proxies), BENCHMARK_BATCH_SIZE):
        batch = proxies[offset:offset + BENCHMARK_BATCH_SIZE]
        try:
            batch_metrics = _benchmark_batch(engine, batch)
        except Exception as exc:
            print(
                f"[WARN] benchmark batch {offset // BENCHMARK_BATCH_SIZE + 1} "
                f"failed: {exc}"
            )
            batch_metrics = []
        metrics.extend(batch_metrics)
    return metrics


def run_delay_tests(controller_url: str, proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    workers = max(1, min(MAX_WORKERS, len(proxies)))
    metrics: list[ProxyMetric] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(test_single_proxy, controller_url, proxy): proxy
            for proxy in proxies
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
    except (ValueError, TypeError):
        return None
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
        if latency is not None:
            rounds.append(latency)
    if len(rounds) < PROBE_PASS_MIN:
        return None
    rounds.sort()
    middle = len(rounds) // 2
    if len(rounds) % 2 == 0:
        latency = (rounds[middle - 1] + rounds[middle]) // 2
    else:
        latency = rounds[middle]
    region = detect_region(name)
    score = health_score(name, latency, region)
    return ProxyMetric(proxy=proxy, latency=latency, region=region, health_score=score)


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


def region_bonus(region: str) -> int:
    if region in {"HK", "SG", "JP"}:
        return 3
    if region == "US":
        return 2
    return 1


def health_score(name: str, latency: int, region: str) -> float:
    stability_seed = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:12], 16)
    stability = random.Random(stability_seed).random()
    # latency is in milliseconds: normalize to a 0..1-ish score before
    # weighting so it is not swamped by the region bonus and the random
    # stability term.
    latency_score = 0.6 * (1000.0 / max(latency, 1))
    return latency_score + region_bonus(region) * 0.3 + stability * 0.1


def low_latency_pool(metrics: list[ProxyMetric]) -> list[str]:
    if not metrics:
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
    proxy = {"name": "DIRECT-FALLBACK", "type": "direct", "udp": True}
    return ProxyMetric(proxy=proxy, latency=LATENCY_TIMEOUT_MS, region="OTHER", health_score=0.0)


def load_existing_metrics() -> list[ProxyMetric]:
    if not OUTPUT_PATH.exists():
        return []
    try:
        data = yaml.safe_load(OUTPUT_PATH.read_text(encoding="utf-8"))
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
                proxy={k: v for k, v in proxy.items() if k != "skip-cert-verify"},
                latency=LATENCY_TIMEOUT_MS,
                region=region,
                health_score=health_score(name, LATENCY_TIMEOUT_MS, region),
            )
        )
    if metrics and all(item.proxy.get("name") == "DIRECT-FALLBACK" for item in metrics):
        print("[WARN] previous output was itself a DIRECT-FALLBACK degraded config")
    return metrics


def build_config(metrics: list[ProxyMetric]) -> dict[str, Any]:
    if not metrics:
        metrics = [build_direct_fallback_metric()]
    metrics = sorted(metrics, key=lambda item: item.health_score, reverse=True)

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
        "allow-lan": os.getenv("FREE_PROXY_AIRPORT_ALLOW_LAN", "0") == "1",
        "mode": "rule",
        "log-level": "info",
        "ipv6": True,
        "unified-delay": True,
        "tcp-concurrent": True,
        "global-client-fingerprint": "chrome",
        "generated-by": f"free-proxy-airport-{VERSION}",
        "generated-at": os.getenv(
            "FREE_PROXY_AIRPORT_GENERATED_AT",
            datetime.now(UTC).isoformat(),
        ),
        "proxies": proxies,
        "proxy-groups": url_test_groups + [
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
                "proxies": ["AUTO-FAST", "FALLBACK"],
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
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
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
    groups = config.get("proxy-groups", [])
    group_names = {group.get("name") for group in groups if isinstance(group, dict)}
    missing = [name for name in REQUIRED_GROUPS if name not in group_names]
    if missing:
        raise RuntimeError(f"generated config missing groups: {missing}")
    empty_groups = [
        name
        for name in REQUIRED_GROUPS
        if not next(
            (
                group.get("proxies")
                for group in groups
                if isinstance(group, dict) and group.get("name") == name
            ),
            None,
        )
    ]
    if empty_groups:
        raise RuntimeError(f"generated config empty groups: {empty_groups}")
    for name, expected_type in REQUIRED_GROUP_TYPES.items():
        group = next(
            (
                group
                for group in groups
                if isinstance(group, dict) and group.get("name") == name
            ),
            None,
        )
        if group is None:
            continue
        if group.get("type") != expected_type:
            raise RuntimeError(
                f"generated config group {name} has type {group.get('type')!r}; "
                f"expected {expected_type!r}"
            )
    rules = config.get("rules", [])
    for rule in REQUIRED_RULES:
        if rule not in rules:
            raise RuntimeError(f"generated config missing rule: {rule}")


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


# ---------------------------------------------------------------------------
# Proxy-to-URI conversion (Shadowrocket format)
# ---------------------------------------------------------------------------

def _b64url(value: str) -> str:
    """Standard base64 without trailing padding (required by the SSR spec)."""
    return base64.b64encode(value.encode("utf-8")).decode().rstrip("=")


def clean_sni(value: Any) -> str:
    """Clean an SNI: keep only a bare hostname.

    Strips http(s):// prefixes, paths, fragments, all whitespace and a
    trailing ``:port`` (single-colon host:port form only, so IPv6 literals
    are not mangled); drops non-ASCII so the result is safe in a URI query.
    """
    sni = str(value or "")
    sni = sni.replace("https://", "").replace("http://", "")
    sni = sni.split("/")[0].split("#")[0]
    if re.match(r"^[^:]+:\d+$", sni):
        sni = sni.rsplit(":", 1)[0]
    sni = re.sub(r"\s+", "", sni)
    return "".join(c for c in sni if ord(c) < 128)


def alpn_value(value: Any) -> str:
    """Normalize an alpn (string or list) to a comma-separated string."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _normalize_tls(value: Any, default: str, pass_through: bool) -> str:
    """Normalize a Clash ``tls`` value (bool or string) to a URI token.

    ``True`` becomes "tls"; ``False`` and falsy strings become ``default``;
    unknown strings either survive unchanged (``pass_through``) or collapse
    to ``default``.
    """
    if value is True:
        return "tls"
    if value is False:
        return default
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "1", "yes", "tls"):
            return "tls"
        if lowered in ("false", "0", "no", ""):
            return default
        return str(value) if pass_through else default
    return str(value)


def _append_sni_param(params: list[str], value: Any) -> None:
    """Append an encoded ``sni`` query parameter when the SNI is usable."""
    sni = clean_sni(value)
    if sni:
        params.append(f"sni={quote(sni, safe='')}")


def _uri_host(server: str) -> str:
    """Bracket IPv6 literals so ``host:port`` URIs stay parseable."""
    server = str(server).strip("[]")
    return f"[{server}]" if ":" in server else server


def _ws_transport(proxy: dict[str, Any]) -> tuple[str, str]:
    """Return ``(path, host)`` for ws transport.

    Clash YAML nests them under ``ws-opts.path`` / ``ws-opts.headers.Host``;
    top-level ``path`` / ``host`` keys win when present so both layouts
    convert correctly.
    """
    ws_opts = proxy.get("ws-opts")
    ws_opts = ws_opts if isinstance(ws_opts, dict) else {}
    headers = ws_opts.get("headers")
    header_host = str(headers.get("Host", "")) if isinstance(headers, dict) else ""
    path = str(proxy.get("path") or ws_opts.get("path") or "/")
    host = str(proxy.get("host") or header_host)
    return path, host


def _grpc_service_name(proxy: dict[str, Any]) -> str:
    """Return the gRPC service name (top-level key wins over grpc-opts)."""
    service = proxy.get("serviceName")
    if not service:
        grpc_opts = proxy.get("grpc-opts")
        if isinstance(grpc_opts, dict):
            service = grpc_opts.get("grpc-service-name", "")
    return str(service or "")


def _ss_plugin_param(proxy: dict[str, Any]) -> str:
    """Build the SIP002 ``plugin`` query value for ss nodes.

    Covers simple-obfs and v2ray-plugin, which is what virtually all public
    ss sources use. Returns "" for other plugins so callers drop the node
    instead of emitting a link that can never connect.
    """
    plugin = str(proxy.get("plugin", "")).strip().lower()
    if not plugin:
        return ""
    opts = proxy.get("plugin-opts")
    opts = opts if isinstance(opts, dict) else {}

    parts: list[str] = []
    if plugin in ("obfs", "obfs-local", "simple-obfs"):
        parts.append("obfs-local")
        mode = str(opts.get("mode", ""))
        if mode:
            parts.append(f"obfs={mode}")
        host = str(opts.get("host", ""))
        if host:
            parts.append(f"obfs-host={host}")
    elif plugin in ("v2ray-plugin", "v2ray"):
        parts.append("v2ray-plugin")
        mode = str(opts.get("mode", "") or "websocket")
        parts.append(f"mode={mode}")
        if opts.get("tls"):
            parts.append("tls")
        host = str(opts.get("host", ""))
        if host:
            parts.append(f"host={host}")
        path = str(opts.get("path", ""))
        if path:
            parts.append(f"path={path}")
    else:
        return ""
    return ";".join(part for part in parts if part)


def _simple_uri(scheme: str, proxy: dict[str, Any]) -> str:
    """Build a basic userinfo-style URI for http / socks5 proxies."""
    server = _uri_host(proxy.get("server", ""))
    port = proxy.get("port", 0)
    name = str(proxy.get("name", ""))
    username = quote(str(proxy.get("username", "")), safe="")
    password = quote(str(proxy.get("password", "")), safe="")
    auth = f"{username}:{password}@" if (username or password) else ""
    return f"{scheme}://{auth}{server}:{port}#{quote(name, safe='')}"


def proxy_to_uri(proxy: dict[str, Any]) -> str:
    """Convert a proxy node to a Shadowrocket URI."""
    proxy_type = str(proxy.get("type", "")).lower().strip()
    if not str(proxy.get("server", "")).strip():
        return ""
    try:
        port = int(proxy.get("port", 0))
    except Exception:
        return ""
    if port <= 0 or port > 65535:
        return ""

    builder = _URI_BUILDERS.get(proxy_type)
    if builder is None:
        return ""
    return builder(proxy)


def _ss_to_uri(proxy: dict[str, Any]) -> str:
    cipher = str(proxy.get("cipher", "aes-256-gcm"))
    password = str(proxy.get("password", ""))
    server = str(proxy.get("server", ""))
    port = proxy.get("port", 0)
    name = str(proxy.get("name", ""))
    userinfo = base64.b64encode(f"{cipher}:{password}".encode()).decode().rstrip("=")

    query = ""
    if proxy.get("plugin"):
        plugin_value = _ss_plugin_param(proxy)
        if not plugin_value:
            # An unexpressible plugin would produce a dead link; skip instead.
            return ""
        query = f"?plugin={quote(plugin_value, safe='')}"
    return f"ss://{userinfo}@{_uri_host(server)}:{port}{query}#{quote(name, safe='')}"


def _ssr_to_uri(proxy: dict[str, Any]) -> str:
    server = str(proxy.get("server", ""))
    port = proxy.get("port", 0)
    protocol = str(proxy.get("protocol", "origin"))
    method = str(proxy.get("cipher", "aes-256-cfb"))
    obfs = str(proxy.get("obfs", "plain"))
    password = str(proxy.get("password", ""))
    name = str(proxy.get("name", ""))

    pass_b64 = _b64url(password)
    obfs_param = str(proxy.get("obfs-param", ""))
    protocol_param = str(proxy.get("protocol-param", ""))

    # SSR spec: query keys must be obfsparam/protoparam/remarks, all base64.
    query = []
    if obfs_param:
        query.append(f"obfsparam={_b64url(obfs_param)}")
    if protocol_param:
        query.append(f"protoparam={_b64url(protocol_param)}")
    if name:
        query.append(f"remarks={_b64url(name)}")
    query_str = "?" + "&".join(query) if query else ""

    main = f"{_uri_host(server)}:{port}:{protocol}:{method}:{obfs}:{pass_b64}/{query_str}"
    return "ssr://" + base64.b64encode(main.encode()).decode().rstrip("=")


def _vmess_to_uri(proxy: dict[str, Any]) -> str:
    server = str(proxy.get("server", ""))
    port = proxy.get("port", 0)
    uuid = str(proxy.get("uuid", ""))
    name = str(proxy.get("name", ""))

    # Normalize the tls field: Clash YAML may hold a bool or a truthy string.
    # Unlike vless (pass_through=True keeps "reality" and other tokens),
    # vmess collapses unknown tokens to "" on purpose: vmess deployments are
    # effectively tls or none, and a bogus token would yield a broken link.
    tls_val = _normalize_tls(proxy.get("tls", ""), default="", pass_through=False)

    ws_path, ws_host = _ws_transport(proxy)

    # In the vmess:// JSON, "type" is the camouflage type (http/srtp/...),
    # not the proxy type "vmess".
    net_type = "http" if isinstance(proxy.get("http-opts"), dict) else "none"
    grpc_opts = proxy.get("grpc-opts")

    config = {
        "v": "2",
        "ps": name,
        "add": server,
        "port": str(port),
        "id": uuid,
        "aid": str(proxy.get("alterId", 0)),
        "scy": str(proxy.get("cipher", "auto")),
        "net": "grpc" if isinstance(grpc_opts, dict) else str(proxy.get("network", "tcp")),
        "serviceName": _grpc_service_name(proxy),
        "type": net_type,
        "host": ws_host,
        "path": ws_path,
        "tls": str(tls_val),
        "sni": str(proxy.get("sni", proxy.get("servername", ""))),
        "alpn": alpn_value(proxy.get("alpn", "")),
        "fp": str(proxy.get("fp", proxy.get("fingerprint", ""))),
    }
    encoded = base64.b64encode(
        json.dumps(config, separators=(",", ":")).encode()
    ).decode()
    return "vmess://" + encoded


def _vless_to_uri(proxy: dict[str, Any]) -> str:
    uuid = str(proxy.get("uuid", ""))
    server = str(proxy.get("server", ""))
    port = proxy.get("port", 0)
    name = str(proxy.get("name", ""))
    network = str(proxy.get("network", "tcp"))

    # Normalize the tls field: Clash YAML may hold a bool or a truthy string.
    tls_val = _normalize_tls(proxy.get("tls", "none"), default="none", pass_through=True)

    params = []
    params.append(f"type={network}")
    params.append(f"security={tls_val}")
    flow = proxy.get("flow")
    if flow not in (None, ""):
        params.append(f"flow={quote(str(flow), safe='')}")
    if tls_val == "reality":
        for key in ("pbk", "sid", "spx"):
            value = proxy.get(key)
            if value not in (None, ""):
                params.append(f"{key}={quote(str(value), safe='')}")
    if network == "ws":
        ws_path, ws_host = _ws_transport(proxy)
        params.append(f"path={quote(ws_path, safe='')}")
        params.append(f"host={quote(ws_host, safe='')}")
    elif network == "grpc":
        service = _grpc_service_name(proxy)
        if service:
            params.append(f"serviceName={quote(service, safe='')}")
    _append_sni_param(params, proxy.get("sni"))
    params.append(f"encryption={proxy.get('encryption', 'none')}")
    fp = str(proxy.get("fp", proxy.get("fingerprint", "")))
    if fp:
        params.append(f"fp={quote(fp, safe='')}")

    return f"vless://{uuid}@{_uri_host(server)}:{port}?{'&'.join(params)}#{quote(name, safe='')}"


def _trojan_to_uri(proxy: dict[str, Any]) -> str:
    password = str(proxy.get("password", ""))
    server = str(proxy.get("server", ""))
    port = proxy.get("port", 0)
    name = str(proxy.get("name", ""))

    params = []
    _append_sni_param(params, proxy.get("sni"))
    alpn = proxy.get("alpn")
    if alpn:
        params.append(f"alpn={quote(alpn_value(alpn), safe='')}")
    network = str(proxy.get("network", "tcp")).strip().lower()
    if network == "ws":
        # Transport details are appended only for non-tcp networks so plain
        # trojan links stay byte-identical to previous versions.
        params.append("type=ws")
        ws_path, ws_host = _ws_transport(proxy)
        params.append(f"path={quote(ws_path, safe='')}")
        params.append(f"host={quote(ws_host, safe='')}")
    elif network == "grpc":
        params.append("type=grpc")
        service = _grpc_service_name(proxy)
        if service:
            params.append(f"serviceName={quote(service, safe='')}")
    if proxy.get("skip-cert-verify") or SKIP_CERT_VERIFY:
        # Keep cert verification on by default; disable it only when the
        # node (or the global opt-in) explicitly asks for it, mirroring the
        # Clash config policy.
        params.append("allowInsecure=1")

    query = "?" + "&".join(params) if params else ""
    return (
        f"trojan://{quote(str(password), safe='')}@{_uri_host(server)}:{port}"
        f"{query}#{quote(name, safe='')}"
    )


def _hysteria_to_uri(proxy: dict[str, Any]) -> str:
    server = str(proxy.get("server", ""))
    port = proxy.get("port", 0)
    name = str(proxy.get("name", ""))
    # Pick the scheme by proxy type: hysteria and hysteria2 differ.
    scheme = "hysteria" if str(proxy.get("type", "")).lower() == "hysteria" else "hysteria2"

    params = []
    if proxy.get("insecure") or proxy.get("skip-cert-verify") or SKIP_CERT_VERIFY:
        # Honour the Clash field plus the global opt-in, mirroring how the
        # trojan/tuic converters treat certificate verification.
        params.append("insecure=1")
    _append_sni_param(params, proxy.get("sni"))
    for key in ("up", "down"):
        value = proxy.get(key)
        if value is not None:
            params.append(f"{key}={quote(str(value), safe='')}")

    query = "?" + "&".join(params) if params else ""

    auth = proxy.get("auth", proxy.get("password", ""))
    if isinstance(auth, str) and auth:
        return (
            f"{scheme}://{quote(auth, safe='')}@{_uri_host(server)}:{port}"
            f"{query}#{quote(name, safe='')}"
        )
    return f"{scheme}://{_uri_host(server)}:{port}{query}#{quote(name, safe='')}"


def _tuic_to_uri(proxy: dict[str, Any]) -> str:
    uuid = str(proxy.get("uuid", ""))
    password = str(proxy.get("password", ""))
    server = str(proxy.get("server", ""))
    port = proxy.get("port", 0)
    name = str(proxy.get("name", ""))

    params = []
    params.append(f"congestion_control={proxy.get('congestion_control', 'cubic')}")
    params.append(f"alpn={quote(alpn_value(proxy.get('alpn', 'h3')), safe='')}")
    _append_sni_param(params, proxy.get("sni"))
    if proxy.get("skip-cert-verify") or SKIP_CERT_VERIFY:
        # Keep cert verification on by default; disable it only when opted in.
        params.append("allowInsecure=1")

    return (
        f"tuic://{quote(str(uuid), safe='')}:{quote(str(password), safe='')}"
        f"@{_uri_host(server)}:{port}?{'&'.join(params)}#{quote(name, safe='')}"
    )


_URI_BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "ss": _ss_to_uri,
    "ssr": _ssr_to_uri,
    "vmess": _vmess_to_uri,
    "vless": _vless_to_uri,
    "trojan": _trojan_to_uri,
    "tuic": _tuic_to_uri,
    "hysteria": _hysteria_to_uri,
    "hysteria2": _hysteria_to_uri,
    "hy2": _hysteria_to_uri,
    "http": lambda proxy: _simple_uri("http", proxy),
    "socks5": lambda proxy: _simple_uri("socks5", proxy),
}


def _uri_list(proxies: list[dict[str, Any]]) -> list[str]:
    """URIs expressible for the given proxies (non-URI nodes are skipped)."""
    return [uri for uri in (proxy_to_uri(proxy) for proxy in proxies) if uri]


def generate_shadowrocket_sub(proxies: list[dict[str, Any]]) -> str:
    """Generate a Shadowrocket subscription (base64 list of URIs)."""
    plaintext = "\n".join(_uri_list(proxies))
    return base64.b64encode(plaintext.encode("utf-8")).decode()


DEGRADED_SUBSCRIPTION_NOTICE = (
    "# DIRECT-FALLBACK: no live nodes available; "
    "this subscription intentionally contains no proxy URIs"
)


def shadowrocket_subscription_content(proxies: list[dict[str, Any]]) -> str:
    """Build subscription content, never returning an empty string.

    When no proxy can be expressed as a URI (e.g. the DIRECT-FALLBACK degraded
    config), a base64-encoded notice keeps the output files non-empty and the
    CI non-empty / base64 checks satisfiable.
    """
    content = generate_shadowrocket_sub(proxies)
    if content:
        return content
    return base64.b64encode(DEGRADED_SUBSCRIPTION_NOTICE.encode("utf-8")).decode()


def main() -> None:
    total_nodes, candidates = collect_proxies()
    metrics: list[ProxyMetric] = []

    if candidates:
        try:
            metrics = benchmark_proxies(candidates)
        except Exception as exc:
            print(f"[WARN] real latency benchmark unavailable: {exc}")

    if not metrics:
        metrics = load_existing_metrics()
        if metrics:
            print(
                "[WARN] no live nodes passed; reusing previous non-empty "
                "output as degraded fallback"
            )

    if not metrics:
        metrics = [build_direct_fallback_metric()]
        print(
            "[WARN] no live or previous nodes; using "
            "DIRECT-FALLBACK degraded config"
        )

    metrics.sort(key=lambda item: item.health_score, reverse=True)

    # Build the Clash config.
    config = build_config(metrics)
    validate_config(config)
    write_config(config)
    print_summary(total_nodes, len(candidates), metrics)

    # Generate Shadowrocket + V2Ray subscriptions (top N fastest nodes).
    top_proxies = [m.proxy for m in metrics[:TOP_N]] if TOP_N > 0 else [m.proxy for m in metrics]
    rocket_content = shadowrocket_subscription_content(top_proxies)
    _atomic_write_text(ROCKET_OUTPUT, rocket_content)
    _atomic_write_text(V2RAY_OUTPUT, rocket_content)
    uri_count = len(_uri_list(top_proxies))
    if uri_count:
        print(
            f"[OK] Shadowrocket/V2Ray 订阅已生成: {ROCKET_OUTPUT} "
            f"({uri_count} 节点)"
        )
    else:
        print(
            f"[WARN] no URI-convertible nodes; {ROCKET_OUTPUT} and "
            f"{V2RAY_OUTPUT} contain a degraded subscription notice"
        )

    # Region distribution of nodes.
    region_stats: dict[str, int] = {}
    for m in metrics:
        region_stats[m.region] = region_stats.get(m.region, 0) + 1
    avg_latency = round(sum(m.latency for m in metrics) / len(metrics)) if metrics else 0
    print("\n=== 节点地区分布 ===")
    for region, count in sorted(region_stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {region}: {count}")
    print(f"  平均延迟: {avg_latency}ms")
    print(f"  总节点: {len(metrics)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate free proxy subscriptions.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate a Clash config without fetching or benchmarking.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=OUTPUT_PATH,
        help="Config path used with --validate-only (default: output/clash.yaml).",
    )
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.validate_only:
        try:
            validate_config(yaml.safe_load(args.config.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            raise
        print("[OK] existing config is valid")
    else:
        try:
            main()
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            raise
