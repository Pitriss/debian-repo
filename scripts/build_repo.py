#!/usr/bin/env python3
"""Build a signed, atomic, allowlisted Debian repository from public GitHub Releases.

No source repository is modified. The output is a self-contained static Pages tree.
"""

import argparse
import contextlib
import datetime as dt
import functools
import gzip
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
ASSET_RE = re.compile(r"^([a-z0-9][a-z0-9+.-]+)_([^_]+)_(amd64|arm64|armhf|all)(-stretch)?\.deb$")
PRE_RELEASE_RE = re.compile(r"(?:^|[.\-~_])(alpha|beta|rc|pre|dev)[.\-~_0-9]*", re.I)
USER_AGENT = "Pitriss-Debian-APT-Repository/1.0"


class RepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    github: str
    package: str
    version: str
    arch: str
    distribution: str
    channel: str
    file_name: str
    download_url: str
    expected_size: int
    release_tag: str
    published_at: str

    @property
    def suite(self) -> str:
        return self.distribution + ("-testing" if self.channel == "testing" else "")


@dataclass(frozen=True)
class StagedPackage:
    candidate: Candidate
    path: Path
    relative_path: str
    control: str
    sha256: str
    size: int


def run(*args: str, input_data: bytes | None = None, env: dict | None = None) -> bytes:
    proc = subprocess.run(args, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if proc.returncode:
        raise RepositoryError(f"Command failed ({proc.returncode}): {args[0]} {args[1:]}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout


def config_load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("keep_versions") != 2:
        raise RepositoryError("This repository must retain exactly 2 versions")
    if data.get("architectures") != ["amd64", "arm64", "armhf"]:
        raise RepositoryError("Unsupported architecture configuration")
    if data.get("distributions") != ["trixie", "stretch"]:
        raise RepositoryError("Unsupported distribution configuration")
    if not data.get("projects"):
        raise RepositoryError("No approved projects configured")
    names: set[str] = set()
    packages: set[str] = set()
    for project in data["projects"]:
        repo = project["github"]
        if not re.fullmatch(r"Pitriss/[A-Za-z0-9_.-]+", repo):
            raise RepositoryError(f"Unapproved GitHub namespace: {repo}")
        if repo in names:
            raise RepositoryError(f"Duplicate GitHub repository: {repo}")
        names.add(repo)
        if not project["distributions"] or set(project["distributions"]) - set(data["distributions"]):
            raise RepositoryError(f"Invalid distributions for {repo}")
        for package in project["packages"]:
            if not PACKAGE_RE.fullmatch(package):
                raise RepositoryError(f"Invalid package name: {package}")
            if package in packages:
                raise RepositoryError(f"Ambiguous package in multiple repositories: {package}")
            packages.add(package)
    return data


def request_bytes(url: str, token: str | None = None, limit: int = 10_000_000) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("api.github.com", "github.com"):
        raise RepositoryError("Refused non-GitHub HTTPS URL: " + url)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token and parsed.hostname == "api.github.com":
        headers["Authorization"] = "Bearer " + token
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as resp:
                content = resp.read(limit + 1)
                if len(content) > limit:
                    raise RepositoryError(f"GitHub API response too large: {url}")
                return content
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            if attempt == 2 or isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise RepositoryError(f"GitHub API request failed: {url}: {exc}") from exc
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def releases_from_github(repo: str, token: str | None, max_pages: int) -> list[dict]:
    all_releases: list[dict] = []
    for page in range(1, max_pages + 1):
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        response = json.loads(request_bytes(url, token=token).decode("utf-8"))
        if not isinstance(response, list):
            raise RepositoryError(f"Unexpected releases response for {repo}: {response!r}")
        all_releases.extend(response)
        if len(response) < 100:
            return all_releases
    raise RepositoryError(f"Release list exceeds configured pagination limit for {repo}")


def parse_candidates(project: dict, releases: list[dict], config: dict) -> list[Candidate]:
    candidates: list[Candidate] = []
    for release in releases:
        if release.get("draft") or not release.get("published_at"):
            continue
        tag = str(release.get("tag_name") or "")
        is_pre = bool(release.get("prerelease")) or bool(PRE_RELEASE_RE.search(tag))
        for asset in release.get("assets") or []:
            name = asset.get("name", "")
            match = ASSET_RE.fullmatch(name)
            if not match:
                continue
            package, version, arch, stretch_marker = match.groups()
            if package not in project["packages"] or arch not in (*config["architectures"], "all"):
                continue
            distribution = "stretch" if stretch_marker else "trixie"
            if distribution not in project["distributions"]:
                continue
            asset_pre = is_pre or bool(PRE_RELEASE_RE.search(version))
            channel = "testing" if asset_pre else "stable"
            size = asset.get("size")
            url = asset.get("browser_download_url", "")
            if not isinstance(size, int) or not 0 < size <= config["max_asset_bytes"]:
                raise RepositoryError(f"Unsupported asset size: {project['github']}/{name}: {size}")
            expected_prefix = f"https://github.com/{project['github']}/releases/download/"
            if not isinstance(url, str) or not url.startswith(expected_prefix):
                raise RepositoryError(f"Unexpected asset URL: {url}")
            candidates.append(Candidate(
                github=project["github"], package=package, version=version,
                arch=arch, distribution=distribution, channel=channel,
                file_name=name, download_url=url, expected_size=size,
                release_tag=tag, published_at=release["published_at"]
            ))
    return candidates


@functools.lru_cache(maxsize=8192)
def version_cmp(a: str, b: str) -> int:
    if a == b:
        return 0
    if subprocess.run(["dpkg", "--compare-versions", a, "gt", b], check=False).returncode == 0:
        return 1
    if subprocess.run(["dpkg", "--compare-versions", a, "lt", b], check=False).returncode == 0:
        return -1
    raise RepositoryError(f"Cannot compare Debian versions {a!r} and {b!r}")


def choose_latest_two(candidates: list[Candidate]) -> list[Candidate]:
    grouped: dict[tuple[str, str, str], dict[str, Candidate]] = {}
    for candidate in candidates:
        key = (candidate.suite, candidate.package, candidate.arch)
        versions = grouped.setdefault(key, {})
        original = versions.get(candidate.version)
        # Duplicate version in different releases: prefer the most recent release.
        if original is None or candidate.published_at > original.published_at:
            versions[candidate.version] = candidate
    selected: list[Candidate] = []
    for key in sorted(grouped):
        versions = sorted(grouped[key], key=functools.cmp_to_key(version_cmp), reverse=True)
        selected.extend(grouped[key][v] for v in versions[:2])
    return selected


def download_public_asset(candidate: Candidate, destination: Path) -> None:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    req = urllib.request.Request(candidate.download_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, destination.open("wb") as out:
            if urllib.parse.urlparse(resp.url).scheme != "https":
                raise RepositoryError(f"Download was redirected away from HTTPS: {candidate.file_name}")
            remaining = candidate.expected_size
            while True:
                chunk = resp.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                remaining -= len(chunk)
                if remaining < 0:
                    raise RepositoryError(f"Oversized download: {candidate.file_name}")
                out.write(chunk)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RepositoryError(f"Cannot download {candidate.file_name}: {exc}") from exc
    if destination.stat().st_size != candidate.expected_size:
        raise RepositoryError(f"Incomplete asset: {candidate.file_name}")


def parse_control(path: Path) -> tuple[dict[str, str], str]:
    raw = run("dpkg-deb", "--field", str(path)).decode("utf-8")
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if line.startswith(" ") or line.startswith("\t"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k] = v.strip()
    for field in ("Package", "Version", "Architecture", "Description"):
        if not fields.get(field):
            raise RepositoryError(f"Missing Debian control field {field} in {path}")
    if not PACKAGE_RE.fullmatch(fields["Package"]):
        raise RepositoryError(f"Invalid Debian package name {fields['Package']!r}")
    return fields, raw.rstrip("\n")


def filename_version_matches_control(asset_version: str, control_version: str) -> bool:
    """Allow Debian's '~' spelling of a prerelease in a GitHub .deb filename.

    Example: reddit2tg_0.4.0-alpha8_arm64.deb carries Version: 0.4.0~alpha8.
    Leave ordinary Debian revisions (e.g. 0.1.1-1) unchanged.
    """
    if control_version == asset_version:
        return True
    normalized = re.sub(
        r"[.-](alpha|beta|rc|pre|dev)(?=[0-9]|$)",
        r"~\1", asset_version, count=1, flags=re.IGNORECASE
    )
    return control_version == normalized


def stage_packages(selected: list[Candidate], output: Path, download) -> list[StagedPackage]:
    result: list[StagedPackage] = []
    for candidate in selected:
        relative = f"pool/{candidate.suite}/main/{candidate.package}/{candidate.file_name}"
        dest = output / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"STAGE {candidate.suite:<16} {candidate.file_name}", flush=True)
        download(candidate, dest)
        fields, control = parse_control(dest)
        if (fields["Package"] != candidate.package
                or fields["Architecture"] != candidate.arch
                or not filename_version_matches_control(candidate.version, fields["Version"])):
            raise RepositoryError(
                f"Asset/control mismatch in {candidate.file_name}: "
                f"Package={fields['Package']} Version={fields['Version']} Architecture={fields['Architecture']}"
            )
        with dest.open("rb") as stream:
            archive_magic = stream.read(8)
        if archive_magic != b"!<arch>\n":
            raise RepositoryError(f"Invalid .deb archive: {candidate.file_name}")
        digest = hashlib.sha256()
        with dest.open("rb") as stream:
            for piece in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(piece)
        sha = digest.hexdigest()
        result.append(StagedPackage(candidate, dest, relative, control, sha, dest.stat().st_size))
    return result


def digest_file(path: Path) -> dict[str, str]:
    data = path.read_bytes()
    return {algo: hashlib.new(algo, data).hexdigest() for algo in ("md5", "sha256")}


def packages_index(packages: list[StagedPackage]) -> bytes:
    def package_sort(a: StagedPackage, b: StagedPackage) -> int:
        ka = (a.candidate.package, a.candidate.arch)
        kb = (b.candidate.package, b.candidate.arch)
        if ka != kb:
            return -1 if ka < kb else 1
        return -version_cmp(a.candidate.version, b.candidate.version)

    entries: list[str] = []
    for package in sorted(packages, key=functools.cmp_to_key(package_sort)):
        checks = digest_file(package.path)
        entries.append("\n".join((
            package.control,
            f"Filename: {package.relative_path}",
            f"Size: {package.size}",
            f"MD5sum: {checks['md5']}",
            f"SHA256: {package.sha256}"
        )))
    return ("\n\n".join(entries) + ("\n\n" if entries else "")).encode("utf-8")


def write_metadata(output: Path, config: dict, packages: list[StagedPackage]) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    date = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    for dist in config["distributions"]:
        for suffix in ("", "-testing"):
            suite = dist + suffix
            root = output / "dists" / suite
            root.mkdir(parents=True, exist_ok=True)
            hashes: list[tuple[str, int, dict[str, str]]] = []
            for arch in config["architectures"]:
                target = root / "main" / f"binary-{arch}"
                target.mkdir(parents=True, exist_ok=True)
                arch_packages = [p for p in packages if p.candidate.suite == suite and p.candidate.arch in (arch, "all")]
                raw = packages_index(arch_packages)
                packages_file = target / "Packages"
                packages_file.write_bytes(raw)
                gz_file = target / "Packages.gz"
                with gz_file.open("wb") as f:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0, compresslevel=9) as gz:
                        gz.write(raw)
                for path in (packages_file, gz_file):
                    relative = path.relative_to(root).as_posix()
                    hashes.append((relative, path.stat().st_size, digest_file(path)))
            lines = [
                "Origin: Pitriss", "Label: Pitriss Debian Repository",
                f"Suite: {suite}", f"Codename: {suite}",
                f"Date: {date}", "Architectures: " + " ".join(config["architectures"]),
                "Components: main", "Description: Pitriss signed Debian packages",
            ]
            for algo, label in (("md5", "MD5Sum"), ("sha256", "SHA256")):
                lines.append(label + ":")
                for path, length, checks in sorted(hashes):
                    lines.append(f" {checks[algo]} {length:16d} {path}")
            (root / "Release").write_text("\n".join(lines) + "\n", encoding="utf-8")
            run("gpg", "--batch", "--yes", "--digest-algo", "SHA256", "--output", str(root / "Release.gpg"), "--detach-sign", str(root / "Release"))
            run("gpg", "--batch", "--yes", "--digest-algo", "SHA256", "--output", str(root / "InRelease"), "--clearsign", str(root / "Release"))
            print(f"INDEX {suite}: {sum(p.candidate.suite == suite for p in packages)} .deb files", flush=True)


def write_site(output: Path, config: dict, selected: list[Candidate]) -> None:
    (output / ".nojekyll").touch()
    public_key = run("gpg", "--batch", "--armor", "--export")
    if b"BEGIN PGP PUBLIC KEY BLOCK" not in public_key:
        raise RepositoryError("No public GPG key available")
    fingerprints = run("gpg", "--batch", "--with-colons", "--list-secret-keys").decode("utf-8")
    if sum(line.startswith("sec:") for line in fingerprints.splitlines()) != 1:
        raise RepositoryError("Expected exactly one private GPG signing key in GNUPGHOME")
    output.joinpath("repo-signing-key.asc").write_bytes(public_key)
    by_suite = {s: [] for d in config["distributions"] for s in (d, d + "-testing")}
    for candidate in selected:
        by_suite[candidate.suite].append(candidate)
    cards = []
    for suite, items in by_suite.items():
        plist = ", ".join(sorted({f"{x.package} ({x.version})" for x in items})) or "No packages currently published"
        cards.append(f"<section><h2>{html.escape(suite)}</h2><p>{html.escape(plist)}</p></section>")
    site = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>Pitriss Debian Repository</title><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:850px;margin:3rem auto;padding:0 1.5rem;background:#f8fafc;color:#1e293b}h1,h2{color:#0f172a}section{padding:.5rem 1rem;margin:1rem 0;border:1px solid #cbd5e1;border-radius:.6rem}code,pre{background:#e2e8f0;padding:.15rem .35rem;border-radius:.2rem;overflow:auto}a{color:#075985}</style>
<h1>Pitriss Debian APT repository</h1><p>Packages from approved public GitHub Releases. Two newest Debian package versions per package, suite and architecture.</p>
<p>Signed repository key: <a href="repo-signing-key.asc">repo-signing-key.asc</a></p>
<p>For Debian 13 use <code>trixie</code>. Prereleases are opt-in via <code>trixie-testing</code>. Older Stretch builds are separate and limited to packages explicitly built for Stretch.</p>
""" + "\n".join(cards) + "\n<p>Sources and installation instructions: <a href=\"https://github.com/Pitriss/debian-repo\">GitHub</a></p></html>\n"
    (output / "index.html").write_text(site, encoding="utf-8")


def build(config: dict, selected: list[Candidate], output: Path, downloader=download_public_asset) -> None:
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise RepositoryError(f"Refusing to overwrite nonempty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if not selected:
        raise RepositoryError("No package assets selected: refusing empty deployment")
    packages = stage_packages(selected, output, downloader)
    seen = {p.candidate.package for p in packages}
    expected = {name for proj in config["projects"] for name in proj["packages"]}
    missing = expected - seen
    if missing:
        raise RepositoryError(f"No package assets found for approved packages: {sorted(missing)}")
    write_metadata(output, config, packages)
    write_site(output, config, selected)
    total = sum(f.stat().st_size for f in output.rglob("*") if f.is_file())
    if total > config["max_site_bytes"]:
        raise RepositoryError(f"GitHub Pages output too large: {total} bytes")
    print(f"DONE packages={len(packages)} bytes={total} suites=4", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = config_load(args.config)
        candidates: list[Candidate] = []
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        for project in config["projects"]:
            print("DISCOVER " + project["github"], flush=True)
            releases = releases_from_github(project["github"], token, config["max_release_pages"])
            found = parse_candidates(project, releases, config)
            if not found:
                raise RepositoryError(f"No matching .deb releases for {project['github']}")
            candidates.extend(found)
        selected = choose_latest_two(candidates)
        build(config, selected, args.output)
    except (RepositoryError, ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
