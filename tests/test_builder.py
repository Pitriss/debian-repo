#!/usr/bin/env python3
"""Offline unit and end-to-end tests; no live GitHub access required."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_repo", ROOT / "scripts" / "build_repo.py")
builder = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


def dummy_asset(name: str, version: str, release_pre=False, ts="2026-11-09T09:00:00Z", size=25):
    return {
        "tag_name": ("v" + version), "prerelease": release_pre, "draft": False,
        "published_at": ts,
        "assets": [{"name": name, "size": size,
                    "browser_download_url": f"https://github.com/Pitriss/reddit2tg/releases/download/v{version}/{name}"}]
    }


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = builder.config_load(ROOT / "config" / "projects.json")
        self.project = next(p for p in self.cfg["projects"] if p["github"] == "Pitriss/reddit2tg")

    def test_debian_version_order(self):
        self.assertGreater(builder.version_cmp("1.0.1", "1.0.0"), 0)
        self.assertGreater(builder.version_cmp("1.0~rc2", "1.0~rc1"), 0)
        self.assertLess(builder.version_cmp("1.0~rc2", "1.0"), 0)
        self.assertGreater(builder.version_cmp("1.0-2", "1.0-1"), 0)

    def test_two_versions_separate_suites_and_architectures(self):
        releases = []
        for version in ("0.3.0", "0.3.1", "0.3.2", "0.4.0-alpha7", "0.4.0-alpha8", "0.4.0-alpha9"):
            for arch in ("amd64", "arm64"):
                releases.append(dummy_asset(f"reddit2tg_{version}_{arch}.deb", version))
            if "alpha" in version:
                releases.append(dummy_asset(f"reddit2tg_{version}_arm64-stretch.deb", version))
        candidates = builder.parse_candidates(self.project, releases, self.cfg)
        selected = builder.choose_latest_two(candidates)
        by_group = {}
        for c in selected:
            by_group.setdefault((c.suite, c.arch), []).append(c.version)
        self.assertEqual(sorted(by_group[("trixie", "amd64")]), ["0.3.1", "0.3.2"])
        self.assertEqual(sorted(by_group[("trixie-testing", "arm64")]), ["0.4.0-alpha8", "0.4.0-alpha9"])
        self.assertEqual(sorted(by_group[("stretch-testing", "arm64")]), ["0.4.0-alpha8", "0.4.0-alpha9"])
        self.assertNotIn(("stretch", "amd64"), by_group)
        self.assertNotIn(("stretch-testing", "amd64"), by_group)

    def test_prerelease_filename_and_control_versions(self):
        self.assertTrue(builder.filename_version_matches_control("0.4.0-alpha8", "0.4.0~alpha8"))
        self.assertTrue(builder.filename_version_matches_control("0.4.0-rc2", "0.4.0~rc2"))
        self.assertTrue(builder.filename_version_matches_control("0.4.0-beta1", "0.4.0~beta1"))
        self.assertTrue(builder.filename_version_matches_control("0.4.0-alpha8", "0.4.0-alpha8"))
        self.assertTrue(builder.filename_version_matches_control("0.1.1-1", "0.1.1-1"))
        self.assertFalse(builder.filename_version_matches_control("0.1.1-1", "0.1.1~1"))
        self.assertFalse(builder.filename_version_matches_control("0.4.0-alpha8", "0.4.0~alpha7"))
        self.assertFalse(builder.filename_version_matches_control("0.4.0-alpha8", "0.4.0+alpha8"))

    def test_only_approved_package_names(self):
        releases = [dummy_asset("unapproved-demo_1.0.0_amd64.deb", "1.0.0")]
        self.assertEqual(builder.parse_candidates(self.project, releases, self.cfg), [])

    def test_prefers_newest_duplicate_release(self):
        name = "reddit2tg_0.4.0-alpha8_arm64.deb"
        old = dummy_asset(name, "0.4.0-alpha8", ts="2026-11-07T00:00:00Z")
        new = dummy_asset(name, "0.4.0-alpha8", ts="2026-11-09T00:00:00Z")
        got = builder.choose_latest_two(builder.parse_candidates(self.project, [old, new], self.cfg))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].published_at, "2026-11-09T00:00:00Z")


class FullBuildTests(unittest.TestCase):
    def create_deb(self, name, version, arch, location):
        src = location / "src" / name / version / arch
        (src / "DEBIAN").mkdir(parents=True, exist_ok=True)
        (src / "DEBIAN" / "control").write_text(
            f"Package: {name}\nVersion: {version}\nArchitecture: {arch}\nMaintainer: Test <test@example.invalid>\nDescription: Offline integration test\n"
        )
        dest = location / f"{name}_{version}_{arch}.deb"
        subprocess.run(["dpkg-deb", "--build", str(src), str(dest)], check=True, capture_output=True)
        return dest

    def test_staging_rejects_different_control_version(self):
        with tempfile.TemporaryDirectory(prefix="test-apt-version-") as td:
            root = Path(td)
            pkg = self.create_deb("reddit2tg", "0.4.0~alpha7", "arm64", root)
            candidate = builder.Candidate(
                "Pitriss/reddit2tg", "reddit2tg", "0.4.0-alpha8", "arm64",
                "trixie", "testing", "reddit2tg_0.4.0-alpha8_arm64.deb",
                "https://github.com/example", pkg.stat().st_size,
                "v0.4.0-alpha8", "2026-11-09T09:00:00Z"
            )
            with self.assertRaisesRegex(builder.RepositoryError, "Asset/control mismatch"):
                builder.stage_packages([candidate], root / "site", lambda c, dest: shutil.copyfile(pkg, dest))

    def test_signed_indexes_and_apt_file_repo(self):
        with tempfile.TemporaryDirectory(prefix="test-apt-") as td:
            root = Path(td)
            home = root / "gnupg"
            home.mkdir(mode=0o700)
            env = dict(os.environ, GNUPGHOME=str(home))
            subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                            "--quick-generate-key", "APT Test Repository", "rsa2048", "sign", "1d"],
                           check=True, capture_output=True, env=env)
            cfg = builder.config_load(ROOT / "config" / "projects.json")
            configs = [("kencoder", "kencoder", "trixie", "stable", "amd64", "0.3.0"),
                       ("encoder", "kencoder", "trixie", "stable", "amd64", "0.2.1"),
                       ("pfx-split", "pfx-split-qt", "trixie", "stable", "amd64", "0.1.1-1"),
                       ("jira2teams", "Jira-2-Teams", "trixie", "stable", "amd64", "0.5.1"),
                       ("reddit2tg", "reddit2tg", "trixie", "stable", "amd64", "0.3.1"),
                       ("reddit2tg", "reddit2tg", "trixie", "stable", "amd64", "0.3.0"),
                       ("reddit2tg", "reddit2tg", "trixie", "testing", "arm64", "0.4.0-alpha8"),
                       ("reddit2tg", "reddit2tg", "stretch", "testing", "arm64", "0.4.0-alpha8")]
            candidates = []
            mapping = {}
            for name, repo, dist, channel, arch, ver in configs:
                # Real reddit2tg alpha assets use '-alpha8' in the filename,
                # but Debian control metadata uses the correctly sorting '~alpha8'.
                control_version = ver.replace("-alpha", "~alpha")
                pkg = self.create_deb(name, control_version, arch, root)
                arch_suffix = arch + ("-stretch" if dist == "stretch" else "")
                file_name = f"{name}_{ver}_{arch_suffix}.deb"
                candidate = builder.Candidate(
                    "Pitriss/" + repo, name, ver, arch, dist, channel, file_name,
                    "https://github.com/example", pkg.stat().st_size, "v" + ver,
                    "2026-11-09T09:00:00Z"
                )
                candidates.append(candidate)
                mapping[(dist, channel, file_name)] = pkg
            oldhome = os.environ.get("GNUPGHOME")
            os.environ["GNUPGHOME"] = str(home)
            try:
                def downloader(c, output):
                    shutil.copyfile(mapping[(c.distribution, c.channel, c.file_name)], output)
                site = root / "site"
                builder.build(cfg, candidates, site, downloader)
                self.assertIn("Version: 0.4.0~alpha8", (site / "dists" / "trixie-testing" / "main" / "binary-arm64" / "Packages").read_text())
                subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_repo.py"), "--site", str(site)],
                               check=True, capture_output=True)
                key = root / "signing-key.asc"
                shutil.copyfile(site / "repo-signing-key.asc", key)
                sources = root / "sources.list"
                sources.write_text(f"deb [signed-by={key}] file:{site} trixie main\n")
                lists = root / "apt-lists"
                (lists / "partial").mkdir(parents=True)
                subprocess.run(["apt-get", "update", "-o", f"Dir::Etc::sourcelist={sources}",
                                "-o", "Dir::Etc::sourceparts=-", "-o", f"Dir::State::lists={lists}",
                                "-o", "APT::Get::List-Cleanup=0"], check=True, capture_output=True)
            finally:
                if oldhome is None:
                    os.environ.pop("GNUPGHOME", None)
                else:
                    os.environ["GNUPGHOME"] = oldhome


if __name__ == "__main__":
    unittest.main()
