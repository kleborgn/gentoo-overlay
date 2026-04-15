#!/usr/bin/env python3
"""
Gentoo overlay GitHub release tracker.
Detects new releases and generates ebuilds automatically.

Usage:
    python scripts/track-releases.py

Environment:
    GITHUB_TOKEN  GitHub personal access token (optional but recommended to avoid rate limits)
"""

import os
import re
import sys
import json
import shutil
import tarfile
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        sys.exit("ERROR: Install tomli: pip install tomli  (needed on Python < 3.11)")

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    sys.exit("ERROR: Install jinja2: pip install jinja2")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
OVERLAY_ROOT = SCRIPT_DIR.parent
TEMPLATES_DIR = SCRIPT_DIR / "ebuild-templates"
CONFIG_FILE = OVERLAY_ROOT / "tracked-repos.toml"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    keep_trailing_newline=True,
)

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _github_headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def github_request(url: str) -> dict:
    req = Request(url, headers=_github_headers())
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get_latest_release(repo: str) -> dict | None:
    """Return GitHub release dict for the latest release, or None."""
    try:
        return github_request(f"https://api.github.com/repos/{repo}/releases/latest")
    except HTTPError as e:
        if e.code == 404:
            # Fall back to listing releases and picking the first non-prerelease
            try:
                releases = github_request(f"https://api.github.com/repos/{repo}/releases")
                stable = [r for r in releases if not r.get("prerelease") and not r.get("draft")]
                return stable[0] if stable else None
            except Exception:
                return None
        raise


# ---------------------------------------------------------------------------
# Version / ebuild helpers
# ---------------------------------------------------------------------------

def tag_to_version(tag: str, prefix: str) -> str:
    """Strip tag prefix to obtain a Portage-compatible version string."""
    if prefix and tag.startswith(prefix):
        return tag[len(prefix):]
    return tag


def existing_versions(pkg_dir: Path, name: str) -> set[str]:
    """Return the set of versions that already have an ebuild."""
    if not pkg_dir.exists():
        return set()
    versions: set[str] = set()
    for f in pkg_dir.glob(f"{name}-*.ebuild"):
        m = re.match(rf"^{re.escape(name)}-(.+)\.ebuild$", f.name)
        if m:
            versions.add(m.group(1))
    return versions


def download_file(url: str, dest: Path) -> None:
    req = Request(url, headers=_github_headers())
    with urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


# ---------------------------------------------------------------------------
# Ebuild generators
# ---------------------------------------------------------------------------

def _patch_cargo_ebuild(content: str, pkg: dict) -> str:
    """Post-process pycargoebuild output to fill in GitHub-specific fields."""
    github = pkg["github"]
    tag_prefix = pkg.get("tag_prefix", "v")
    homepage = pkg.get("homepage") or f"https://github.com/{github}"
    description = pkg["description"].replace('"', '\\"')

    # Fill DESCRIPTION if blank
    content = re.sub(
        r'^DESCRIPTION=""',
        f'DESCRIPTION="{description}"',
        content,
        flags=re.MULTILINE,
    )
    # Fill HOMEPAGE if blank
    content = re.sub(
        r'^HOMEPAGE=""',
        f'HOMEPAGE="{homepage}"',
        content,
        flags=re.MULTILINE,
    )
    # Fill LICENSE if blank (pycargoebuild usually fills crate licenses; keep if present)
    license_ = pkg["license"]
    content = re.sub(
        r'^LICENSE=""',
        f'LICENSE="{license_}"',
        content,
        flags=re.MULTILINE,
    )
    # Add the GitHub source tarball to SRC_URI alongside crate URIs
    tarball = (
        f"https://github.com/{github}/archive/refs/tags/"
        f"{tag_prefix}${{PV}}.tar.gz -> ${{P}}.tar.gz"
    )
    content = re.sub(
        r'SRC_URI="\$\{CARGO_CRATE_URIS\}"',
        f'SRC_URI="\n\t{tarball}\n\t${{CARGO_CRATE_URIS}}\n"',
        content,
    )
    # Ensure src_unpack is present
    if "src_unpack" not in content:
        content = re.sub(
            r"(src_install\(\))",
            "src_unpack() {\n\tcargo_src_unpack\n}\n\n\\1",
            content,
            count=1,
        )
    return content


def generate_cargo_ebuild(pkg: dict, version: str, tarball_path: Path) -> str:
    """Use pycargoebuild to generate a Cargo ebuild, then patch it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        with tarfile.open(tarball_path) as tf:
            tf.extractall(tmp)

        # Find the top-level Cargo.toml
        cargo_tomls = sorted(tmp.rglob("Cargo.toml"), key=lambda p: len(p.parts))
        if not cargo_tomls:
            raise RuntimeError("No Cargo.toml found in source tarball")
        cargo_toml = cargo_tomls[0]

        result = subprocess.run(
            ["pycargoebuild", "-i", str(cargo_toml)],
            capture_output=True,
            text=True,
            check=True,
        )
        content = result.stdout

    return _patch_cargo_ebuild(content, pkg)


def generate_template_ebuild(pkg: dict, template_name: str) -> str:
    """Render a Jinja2 template for non-Cargo build systems."""
    github = pkg["github"]
    tag_prefix = pkg.get("tag_prefix", "v")
    homepage = pkg.get("homepage") or f"https://github.com/{github}"

    template = jinja_env.get_template(template_name)
    return template.render(
        github=github,
        name=pkg.get("name") or github.split("/")[-1],
        description=pkg["description"],
        homepage=homepage,
        license=pkg["license"],
        slot=pkg.get("slot", "0"),
        tag_prefix=tag_prefix,
        year=datetime.now().year,
    )


def _asset_ext(filename: str) -> str:
    """Extract the distfile extension from an asset filename, e.g. 'tar.xz'."""
    m = re.search(r'\.(tar\.\w+|\w+)$', filename)
    return m.group(1) if m else "bin"


def generate_prebuilt_ebuild(pkg: dict, version: str) -> str:
    """
    For prebuilt (binary) packages:
    - If an existing ebuild is available, copy it verbatim — content is version-agnostic
      because all version info comes via ${PV}.
    - Otherwise generate a skeleton from the prebuilt template. The skeleton has
      SRC_URI, RDEPEND, etc. pre-filled, but src_install() contains a TODO die()
      that the maintainer must replace before the ebuild is usable.
    """
    name = pkg.get("name") or pkg["github"].split("/")[-1]
    category = pkg["category"]
    pkg_dir = OVERLAY_ROOT / category / name

    # Prefer copying an existing ebuild — install logic rarely changes between versions
    if pkg_dir.exists():
        existing = sorted(pkg_dir.glob(f"{name}-*.ebuild"))
        if existing:
            src = existing[-1]
            print(f"  Copying content from {src.name} (install logic unchanged between versions)")
            return src.read_text()

    # First time: generate a skeleton
    print("  No existing ebuild found — generating skeleton from template.")
    print("  NOTE: You must implement src_install() before this ebuild is installable!")

    github = pkg["github"]
    tag_prefix = pkg.get("tag_prefix", "v")
    homepage = pkg.get("homepage") or f"https://github.com/{github}"
    eclasses = pkg.get("eclasses", [])

    arch_assets = [
        {
            "arch": a["arch"],
            # Replace {PV} placeholder with actual bash variable
            "filename_ebuild": a["filename"].replace("{PV}", "${PV}"),
            "ext": _asset_ext(a["filename"]),
        }
        for a in pkg.get("arch_assets", [])
    ]

    template = jinja_env.get_template("prebuilt.ebuild.j2")
    return template.render(
        github=github,
        name=name,
        description=pkg["description"],
        homepage=homepage,
        license=pkg["license"],
        slot=pkg.get("slot", "0"),
        tag_prefix=tag_prefix,
        keywords=pkg.get("keywords", "~amd64"),
        restrict=pkg.get("restrict", ""),
        qa_prebuilt=pkg.get("qa_prebuilt", ""),
        install_path=pkg.get("install_path", f"opt/{name}"),
        eclasses=eclasses,
        arch_assets=arch_assets,
        rdepend=pkg.get("rdepend", []),
        year=datetime.now().year,
    )


# Map build_system -> template filename
TEMPLATE_MAP = {
    "cmake": "cmake.ebuild.j2",
    "meson": "meson.ebuild.j2",
    "autotools": "autotools.ebuild.j2",
    "python-pep517": "python-pep517.ebuild.j2",
}

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def run_manifest(category_pkg: str) -> None:
    print(f"  Running pkgdev manifest {category_pkg} ...")
    result = subprocess.run(
        ["pkgdev", "manifest", category_pkg],
        cwd=str(OVERLAY_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  WARNING: pkgdev manifest failed:\n{result.stderr}", file=sys.stderr)
    else:
        print("  Manifest updated.")


# ---------------------------------------------------------------------------
# Main per-package logic
# ---------------------------------------------------------------------------

def process_package(pkg: dict) -> bool:
    """Check one package for new releases. Returns True if a new ebuild was written."""
    repo = pkg["github"]
    name = pkg.get("name") or repo.split("/")[-1]
    category = pkg["category"]
    build_system = pkg["build_system"]
    tag_prefix = pkg.get("tag_prefix", "v")

    print(f"\n[{category}/{name}] Checking {repo} ...")

    release = get_latest_release(repo)
    if not release:
        print("  No public releases found.")
        return False

    tag = release["tag_name"]
    version = tag_to_version(tag, tag_prefix)
    print(f"  Latest release: {tag} -> version {version}")

    pkg_dir = OVERLAY_ROOT / category / name
    existing = existing_versions(pkg_dir, name)

    if version in existing:
        print(f"  Ebuild for {version} already exists, skipping.")
        return False

    print(f"  New version {version}! Generating ebuild ...")

    if build_system == "cargo":
        tarball_url = (
            f"https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz"
        )
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            print("  Downloading source tarball ...")
            download_file(tarball_url, tmp_path)
            content = generate_cargo_ebuild(pkg, version, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    elif build_system == "prebuilt":
        content = generate_prebuilt_ebuild(pkg, version)

    elif build_system in TEMPLATE_MAP:
        content = generate_template_ebuild(pkg, TEMPLATE_MAP[build_system])

    else:
        print(f"  ERROR: Unknown build_system '{build_system}'", file=sys.stderr)
        return False

    # Write ebuild
    pkg_dir.mkdir(parents=True, exist_ok=True)
    ebuild_path = pkg_dir / f"{name}-{version}.ebuild"
    ebuild_path.write_text(content)
    print(f"  Wrote {ebuild_path.relative_to(OVERLAY_ROOT)}")

    run_manifest(f"{category}/{name}")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not CONFIG_FILE.exists():
        sys.exit(f"ERROR: Config not found: {CONFIG_FILE}")

    with open(CONFIG_FILE, "rb") as f:
        config = tomllib.load(f)

    packages = config.get("packages", [])
    if not packages:
        print("No packages configured in tracked-repos.toml — nothing to do.")
        return

    updated: list[str] = []
    errors: list[str] = []

    for pkg in packages:
        name = pkg.get("name") or pkg.get("github", "?").split("/")[-1]
        try:
            if process_package(pkg):
                updated.append(f"{pkg['category']}/{name}")
        except Exception as e:
            msg = f"{pkg.get('github', name)}: {e}"
            print(f"  ERROR: {msg}", file=sys.stderr)
            errors.append(msg)

    print("\n" + "=" * 60)
    if updated:
        print(f"New ebuilds generated: {', '.join(updated)}")
    else:
        print("No new releases found.")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
