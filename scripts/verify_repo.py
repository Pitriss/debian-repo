#!/usr/bin/env python3
"""Read-only verification of a GitHub Pages APT repository before deployment."""

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class ValidationError(Exception):
    pass


def must(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(site: Path) -> None:
    must((site / "repo-signing-key.asc").is_file(), "Missing public GPG key")
    suites = ["trixie", "trixie-testing", "stretch", "stretch-testing"]
    with tempfile.TemporaryDirectory(prefix="apt-verify-") as home:
        os.chmod(home, 0o700)
        def gpg(*args: str) -> bytes:
            process = subprocess.run(["gpg", "--batch", "--homedir", home, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            must(process.returncode == 0, f"GPG failed: {' '.join(args)}: {process.stderr.decode(errors='replace')}")
            return process.stdout
        gpg("--import", str(site / "repo-signing-key.asc"))
        for suite in suites:
            root = site / "dists" / suite
            must(root.is_dir(), f"Missing suite {suite}")
            release = root / "Release"
            gpg("--verify", str(root / "Release.gpg"), str(release))
            gpg("--verify", str(root / "InRelease"))
            must(gpg("--decrypt", str(root / "InRelease")) == release.read_bytes(), f"InRelease does not match Release: {suite}")
            content = release.read_text(encoding="utf-8")
            must(f"Suite: {suite}\n" in content, f"Incorrect suite: {suite}")
            sha_section = content.split("\nSHA256:\n", 1)
            must(len(sha_section) == 2, f"Missing SHA256 checksums: {suite}")
            hashes = sha_section[1].splitlines()
            must(hashes, f"No metadata hashes: {suite}")
            for line in hashes:
                parts = line.split()
                must(len(parts) == 3, f"Invalid SHA256 entry: {line}")
                digest_expected, size, name = parts
                must(re.fullmatch(r"main/binary-(amd64|arm64|armhf)/Packages(\.gz)?", name) is not None, f"Unexpected index path {name}")
                path = root / name
                must(path.is_file(), f"Missing index {path}")
                must(path.stat().st_size == int(size), f"Invalid index size {name}")
                must(digest(path, "sha256") == digest_expected, f"Invalid index hash {name}")
            for arch in ("amd64", "arm64", "armhf"):
                index = root / "main" / f"binary-{arch}" / "Packages"
                index_gz = index.with_suffix(".gz")
                must(index.exists() and index_gz.exists(), f"Missing index for {suite}/{arch}")
                must(gzip.decompress(index_gz.read_bytes()) == index.read_bytes(), f"Invalid gzip index {suite}/{arch}")
                for stanza in index.read_text().strip().split("\n\n"):
                    if not stanza.strip():
                        continue
                    fields = {}
                    for line in stanza.splitlines():
                        if line and not line[0].isspace() and ":" in line:
                            k, v = line.split(":", 1)
                            fields[k] = v.strip()
                    for field in ("Package", "Version", "Architecture", "Filename", "Size", "SHA256"):
                        must(field in fields, f"Missing {field} in {suite}/{arch}")
                    must(fields["Architecture"] in (arch, "all"), f"Cross-architecture package in {suite}/{arch}")
                    name = fields["Filename"]
                    must(re.fullmatch(r"pool/[A-Za-z0-9+._/-]+\.deb", name) is not None and ".." not in Path(name).parts, f"Unsafe Filename: {name}")
                    path = site / name
                    must(path.is_file(), f"Missing .deb: {name}")
                    must(path.stat().st_size == int(fields["Size"]), f"Wrong .deb length {name}")
                    must(digest(path, "sha256") == fields["SHA256"], f"Wrong .deb SHA256 {name}")
            print(f"OK {suite}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args.site.resolve())
    except (ValidationError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
