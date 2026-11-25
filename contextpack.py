#!/usr/bin/env python3
"""ContextPack — generate a compact, AI-ready brief for any code repo.

Stdlib only, Python 3.10+. Reads what files actually say — it never
assumes a license, version, or stack from a sibling project.

Usage:
    python contextpack.py pack <repo-dir> [--out CONTEXT.md]
                                [--json context.json] [--max-files 400]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

__version__ = "0.2.0"

SKIP_DIRS = {".git", "__pycache__", "node_modules", "target", ".venv",
             "venv", ".tox", ".mypy_cache", ".pytest_cache", "dist",
             "build", ".idea", ".vs"}
LICENSE_NAMES = {"license", "licence", "license.md", "license.txt",
                 "licence.md", "licence.txt", "copying", "copying.md",
                 "unlicense", "unlicense.md", "unlicense.txt"}


# ---------------------------------------------------------------- helpers
def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024.0
    return f"{n} B"


def read_text(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            return stream.read(limit)
    except OSError:
        return ""


def walk_files(root: Path, max_files: int) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not (base / d).is_symlink()
        )
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink():
                continue
            if len(files) >= max_files:
                truncated = True
                return files, truncated
            files.append(path)
    return files, truncated


def is_regular_file(path: Path, root: Path | None = None) -> bool:
    """Return true only for ordinary files, never symlink targets."""
    try:
        if not path.is_file() or path.is_symlink():
            return False
        return root is None or path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def is_real_directory(path: Path, root: Path) -> bool:
    """Return true only for non-symlink directories contained in the repo."""
    try:
        return (path.is_dir() and not path.is_symlink()
                and path.resolve().is_relative_to(root.resolve()))
    except OSError:
        return False


def rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- git
def redact_remote_url(remote: str | None) -> str | None:
    """Remove credentials and query data from URL-style Git remotes."""
    if not remote:
        return remote
    try:
        parts = urlsplit(remote)
        if not parts.scheme or not parts.netloc:
            return remote
        hostname = parts.hostname
        if not hostname:
            return "[redacted remote URL]"
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port:
            host += f":{parts.port}"
        if parts.username is not None or parts.password is not None:
            host = f"[redacted]@{host}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "[redacted remote URL]"


def git_info(root: Path) -> dict:
    info = {"is_repo": False, "remote_origin": None, "branch": None,
            "head": None, "dirty_count": None}
    try:
        def run(*args):
            r = subprocess.run(["git", "-C", str(root), *args],
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        head = run("rev-parse", "--short", "HEAD")
        if head is None:
            return info
        info["is_repo"] = True
        info["head"] = head
        info["branch"] = run("branch", "--show-current") or "(detached)"
        info["remote_origin"] = redact_remote_url(
            run("remote", "get-url", "origin")
        )
        status = run("status", "--porcelain")
        info["dirty_count"] = 0 if status == "" else len(status.splitlines()) if status is not None else None
    except (OSError, subprocess.SubprocessError):
        pass
    return info


# ---------------------------------------------------------------- license
def detect_license(root: Path, files: list[Path]) -> dict:
    result = {"file": None, "first_line": None, "identifier": None}
    candidates = [p for p in files if p.parent == root and p.name.lower() in LICENSE_NAMES]
    if not candidates:
        return result
    path = sorted(candidates)[0]
    text = read_text(path)
    result["file"] = path.name
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result["first_line"] = lines[0] if lines else ""
    upper = text.upper()
    ident = "Unknown (see license file)"
    if "GNU AFFERO GENERAL PUBLIC LICENSE" in upper:
        ident = "AGPL-3.0 (from license text)" if "VERSION 3" in upper else "AGPL (from license text)"
    elif "GNU LESSER GENERAL PUBLIC LICENSE" in upper:
        ident = "LGPL (from license text)"
    elif "GNU GENERAL PUBLIC LICENSE" in upper:
        if "VERSION 3" in upper:
            ident = "GPL-3.0 (from license text)"
        elif "VERSION 2" in upper:
            ident = "GPL-2.0 (from license text)"
        else:
            ident = "GPL (from license text)"
    elif "APACHE LICENSE" in upper:
        ident = "Apache-2.0 (from license text)" if "VERSION 2.0" in upper else "Apache (from license text)"
    elif "MIT LICENSE" in upper or "PERMISSION IS HEREBY GRANTED, FREE OF CHARGE" in upper:
        ident = "MIT"
    elif "MOZILLA PUBLIC LICENSE" in upper:
        ident = "MPL-2.0 (from license text)"
    elif "REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS" in upper:
        ident = "BSD (from license text)"
    elif "ISC LICENSE" in upper or ("PERMISSION TO USE, COPY, MODIFY" in upper and "ISC" in upper):
        ident = "ISC (from license text)"
    elif ("THE UNLICENSE" in upper
          or "UNLICENSE" == (result["first_line"] or "").upper()
          or ("FREE AND UNENCUMBERED SOFTWARE" in upper
              and "PUBLIC DOMAIN" in upper)):
        ident = "Unlicense (from license text)"
    result["identifier"] = ident
    return result


# ---------------------------------------------------------------- TOML
def load_toml(path: Path) -> dict:
    if tomllib is not None:
        try:
            with path.open("rb") as fh:
                return tomllib.load(fh)
        except Exception:
            return {}
    return _simple_toml(read_text(path))


_TOML_STRING = re.compile(r"\"([^\"]*)\"|'([^']*)'")


def _toml_strings(value: str) -> list[str]:
    return [m.group(1) if m.group(1) is not None else m.group(2)
            for m in _TOML_STRING.finditer(value)]


def _strip_toml_comment(value: str) -> str:
    """Drop a trailing comment, honouring quoted segments."""
    quote = None
    for index, char in enumerate(value):
        if quote is None and char in "\"'":
            quote = char
        elif quote == char:
            quote = None
        elif quote is None and char == "#":
            return value[:index].rstrip()
    return value


def _logical_toml_lines(text: str) -> list[str]:
    """Join array values that continue over several physical lines."""
    out: list[str] = []
    pending: str | None = None
    for raw in text.splitlines():
        line = _strip_toml_comment(raw.strip())
        if pending is not None:
            pending = f"{pending} {line}"
            if pending.count("[") <= pending.count("]"):
                out.append(pending)
                pending = None
            continue
        if "=" in line:
            value = line.partition("=")[2].strip()
            if value.startswith("[") and value.count("[") > value.count("]"):
                pending = line
                continue
        out.append(line)
    if pending is not None:
        out.append(pending)
    return out


def _simple_toml(text: str) -> dict:
    """Very small TOML subset parser: sections, strings, ints, arrays."""
    data: dict = {}
    current = data
    for line in _logical_toml_lines(text):
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            current = data
            for part in m.group(1).split("."):
                current = current.setdefault(part.strip().strip('"'), {})
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.+)$", line)
        if m:
            key, val = m.group(1), _strip_toml_comment(m.group(2).strip())
            if val.startswith('"') and val.endswith('"') and len(val) > 1:
                current[key] = val[1:-1]
            elif val.startswith("[") and val.endswith("]"):
                current[key] = _toml_strings(val)
            elif re.fullmatch(r"-?\d+", val):
                current[key] = int(val)
            elif val in ("true", "false"):
                current[key] = val == "true"
            else:
                current[key] = val.strip('"')
    return data


# ---------------------------------------------------------------- stacks
def detect_rust(root: Path, files: list[Path]) -> dict | None:
    cargos = [p for p in files if p.name == "Cargo.toml"]
    if not cargos:
        return None
    cargos.sort(key=lambda p: (len(p.parts), str(p)))
    data = load_toml(cargos[0])
    pkg = data.get("package", {}) if isinstance(data, dict) else {}
    deps = data.get("dependencies", {}) if isinstance(data, dict) else {}
    workspace = data.get("workspace", {}) if isinstance(data, dict) else {}
    return {
        "manifest": rel(root, cargos[0]),
        "manifest_count": len(cargos),
        "package": pkg.get("name"),
        "version": str(pkg.get("version")) if pkg.get("version") is not None else None,
        "edition": str(pkg.get("edition")) if pkg.get("edition") is not None else None,
        "dependencies_count": len(deps) if isinstance(deps, dict) else 0,
        "workspace_members": workspace.get("members") if isinstance(workspace, dict) else None,
    }


def detect_go(root: Path, files: list[Path]) -> dict | None:
    manifests = sorted(
        (p for p in files if p.name == "go.mod"),
        key=lambda p: (len(p.parts), str(p)),
    )
    go_files = [p for p in files if p.suffix.lower() == ".go"]
    if not manifests and not go_files:
        return None

    module = go_version = toolchain = None
    dependencies = 0
    if manifests:
        for raw in read_text(manifests[0], 50_000).splitlines():
            line = raw.strip()
            match = re.match(r"^module\s+(\S+)", line)
            if match:
                module = match.group(1)
            match = re.match(r"^go\s+(\S+)", line)
            if match:
                go_version = match.group(1)
            match = re.match(r"^toolchain\s+(\S+)", line)
            if match:
                toolchain = match.group(1)

        in_require_block = False
        for raw in read_text(manifests[0], 50_000).splitlines():
            line = raw.strip()
            if line.startswith("require ("):
                in_require_block = True
                continue
            if in_require_block:
                if line == ")":
                    in_require_block = False
                elif line and not line.startswith("//") and "// indirect" not in line:
                    dependencies += 1
            else:
                match = re.match(r"^require\s+\S+\s+\S+(.*)$", line)
                if match and "// indirect" not in match.group(1):
                    dependencies += 1

    return {
        "manifest": rel(root, manifests[0]) if manifests else None,
        "manifest_count": len(manifests),
        "module": module,
        "go_version": go_version,
        "toolchain": toolchain,
        "dependencies_count": dependencies if manifests else None,
        "go_files": len(go_files),
    }


MAVEN_NESTED_BLOCKS = (
    "parent", "dependencies", "dependencyManagement", "build", "profiles",
    "reporting", "distributionManagement", "repositories",
    "pluginRepositories", "properties", "modules", "licenses", "developers",
    "contributors", "organization", "issueManagement", "ciManagement",
    "mailingLists", "prerequisites", "scm",
)


def _strip_xml_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _strip_xml_blocks(text: str, tags: tuple[str, ...]) -> str:
    for tag in tags:
        text = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}\s*>", "", text, flags=re.I | re.S
        )
    return text


def _xml_value(text: str, tag: str) -> str | None:
    match = re.search(
        rf"<{tag}(?:\s[^>]*)?>\s*([^<]+?)\s*</{tag}\s*>", text, re.I
    )
    return match.group(1).strip() if match else None


def _maven_coordinates(text: str) -> tuple[dict, list[str]]:
    """Project-level coordinates only.

    Versions and artifact ids declared inside ``<dependencies>``,
    ``<build>`` or other nested blocks belong to something else and must
    never be reported as the project's own. Missing groupId/version are
    filled from ``<parent>`` (real Maven inheritance) and flagged.
    """
    text = _strip_xml_comments(text)
    project_match = re.search(
        r"<project\b[^>]*>(.*)</project\s*>", text, flags=re.I | re.S
    )
    project_text = project_match.group(1) if project_match else text
    parent_match = re.search(
        r"<parent\b[^>]*>(.*?)</parent\s*>", project_text, flags=re.I | re.S
    )
    parent_text = parent_match.group(1) if parent_match else ""
    own_text = _strip_xml_blocks(project_text, MAVEN_NESTED_BLOCKS)
    coords = {
        "group_id": _xml_value(own_text, "groupId"),
        "artifact_id": _xml_value(own_text, "artifactId"),
        "version": _xml_value(own_text, "version"),
    }
    inherited: list[str] = []
    for key, tag in (("group_id", "groupId"), ("version", "version")):
        if coords[key] is None:
            inherited_value = _xml_value(parent_text, tag)
            if inherited_value is not None:
                coords[key] = inherited_value
                inherited.append(tag)
    return coords, inherited


def _maven_value(text: str, tag: str) -> str | None:
    """Value of a project-level tag, ignoring nested blocks such as the parent."""
    own_text = _strip_xml_blocks(_strip_xml_comments(text), MAVEN_NESTED_BLOCKS)
    return _xml_value(own_text, tag)


def _maven_dependency_count(text: str) -> int:
    # Dependency management declares versions, not project dependencies.
    text = re.sub(
        r"<dependencyManagement\b[^>]*>.*?</dependencyManagement>",
        "",
        text,
        flags=re.I | re.S,
    )
    blocks = re.findall(
        r"<dependencies\b[^>]*>(.*?)</dependencies>", text, flags=re.I | re.S
    )
    return sum(
        len(re.findall(r"<dependency(?:\s[^>]*)?>", block, flags=re.I))
        for block in blocks
    )


def _gradle_value(text: str, key: str) -> str | None:
    match = re.search(
        rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]",
        text,
        flags=re.M,
    )
    return match.group(1).strip() if match else None


def _gradle_dependency_count(text: str) -> int | None:
    match = re.search(
        r"\bdependencies\s*\{(.*?)^\s*\}", text, flags=re.I | re.S | re.M
    )
    if not match:
        return None
    configurations = (
        "implementation|api|compileOnly|runtimeOnly|testImplementation|"
        "testCompileOnly|testRuntimeOnly|annotationProcessor|kapt|compile|"
        "runtime|testCompile"
    )
    return len(re.findall(
        rf"^\s*(?:{configurations})\s*(?:\(|\s)",
        match.group(1),
        flags=re.I | re.M,
    ))


def _has_kotlin_plugin(text: str) -> bool:
    return bool(re.search(
        r"org\.jetbrains\.kotlin|kotlin-maven-plugin|"
        r"kotlin\(\s*['\"][^'\"]+['\"]\s*\)",
        text,
        flags=re.I,
    ))


def detect_java(root: Path, files: list[Path]) -> dict | None:
    maven_files = sorted(p for p in files if p.name == "pom.xml")
    gradle_files = sorted(
        p for p in files if p.name in ("build.gradle", "build.gradle.kts")
    )
    java_files = [p for p in files if p.suffix.lower() == ".java"]
    if not java_files:
        kotlin_sources = any(
            p.suffix.lower() in (".kt", ".kts")
            and p.name not in ("build.gradle.kts", "settings.gradle.kts")
            for p in files
        )
        if kotlin_sources:
            maven_files = []
            gradle_files = []
        else:
            maven_files = [
                p for p in maven_files
                if not _has_kotlin_plugin(read_text(p, 50_000))
            ]
            gradle_files = [
                p for p in gradle_files
                if not _has_kotlin_plugin(read_text(p, 50_000))
            ]
    if not maven_files and not gradle_files and not java_files:
        return None

    maven_projects = []
    for path in maven_files[:10]:
        text = read_text(path, 50_000)
        coordinates, inherited = _maven_coordinates(text)
        maven_projects.append({
            "file": rel(root, path),
            **coordinates,
            "dependencies_count": _maven_dependency_count(text),
            "inherited_from_parent": inherited,
        })

    gradle_projects = []
    for path in gradle_files[:10]:
        text = read_text(path, 50_000)
        gradle_projects.append({
            "file": rel(root, path),
            "group": _gradle_value(text, "group"),
            "version": _gradle_value(text, "version"),
            "dependencies_count": _gradle_dependency_count(text),
        })

    return {
        "java_files": len(java_files),
        "maven_projects": maven_projects,
        "gradle_projects": gradle_projects,
    }


def detect_kotlin(root: Path, files: list[Path]) -> dict | None:
    kotlin_files = [
        p for p in files
        if p.suffix.lower() in (".kt", ".kts")
        and p.name not in ("build.gradle.kts", "settings.gradle.kts")
    ]
    gradle_files = sorted(
        p for p in files
        if p.name in (
            "build.gradle", "build.gradle.kts",
            "settings.gradle", "settings.gradle.kts",
        )
    )
    maven_files = sorted(p for p in files if p.name == "pom.xml")
    gradle_data = [
        (p, read_text(p, 50_000)) for p in gradle_files
    ]
    maven_data = [
        (p, read_text(p, 50_000)) for p in maven_files
    ]
    if not kotlin_files and not any(
        _has_kotlin_plugin(text) for _, text in gradle_data + maven_data
    ):
        return None

    compiler_version = None
    project_name = None
    gradle_builds = []
    for path, text in gradle_data:
        if path.name.startswith("build.gradle"):
            gradle_builds.append(path)
        if project_name is None:
            match = re.search(
                r"^\s*rootProject\.name\s*=\s*['\"]([^'\"]+)['\"]",
                text,
                flags=re.M,
            )
            if match:
                project_name = match.group(1)
        if compiler_version is None:
            patterns = (
                r"(?:kotlin\(\s*['\"][^'\"]+['\"]\s*\)|"
                r"id\s*\(?\s*['\"]org\.jetbrains\.kotlin(?:\.[^'\"]+)?['\"]\s*\)?)"
                r"[^\n]*?version\s*['\"]([^'\"]+)['\"]",
                r"kotlinVersion\s*=\s*['\"]([^'\"]+)['\"]",
            )
            for pattern in patterns:
                match = re.search(pattern, text, flags=re.I)
                if match:
                    compiler_version = match.group(1)
                    break

    maven_projects = []
    for path, text in maven_data:
        if _has_kotlin_plugin(text):
            maven_projects.append(path)
            if compiler_version is None:
                match = re.search(
                    r"<kotlin\.version>\s*([^<]+)\s*</kotlin\.version>",
                    text,
                    flags=re.I,
                )
                if match:
                    compiler_version = match.group(1).strip()
            if project_name is None:
                project_name = _maven_value(text, "artifactId")

    if not gradle_builds and not maven_projects and not kotlin_files:
        return None

    gradle_counts = [
        _gradle_dependency_count(read_text(path, 50_000))
        for path in gradle_builds
    ]
    maven_counts = [
        _maven_dependency_count(read_text(path, 50_000))
        for path in maven_projects
    ]
    dependency_counts = [
        count for count in gradle_counts + maven_counts if count is not None
    ]
    return {
        "name": project_name,
        "compiler_version": compiler_version,
        "kotlin_files": len(kotlin_files),
        "gradle_files": [rel(root, p) for p in gradle_builds[:10]],
        "maven_files": [rel(root, p) for p in maven_projects[:10]],
        "dependencies_count": sum(dependency_counts) if dependency_counts else None,
    }


def detect_swift(root: Path, files: list[Path]) -> dict | None:
    package_files = sorted(p for p in files if p.name == "Package.swift")
    package_manifest = root / "Package.swift"
    has_package_manifest = is_regular_file(package_manifest, root)
    swift_files = [
        p for p in files
        if p.suffix.lower() == ".swift" and p.name != "Package.swift"
    ]
    xcode_projects = sorted({
        rel(root, p.parent)
        for p in files
        if p.name == "project.pbxproj"
        and any(part.endswith(".xcodeproj") for part in p.parts)
    })
    xcode_workspaces = sorted({
        rel(root, p.parent)
        for p in files
        if p.name == "contents.xcworkspacedata"
        and any(part.endswith(".xcworkspace") for part in p.parts)
    })
    if not has_package_manifest and not swift_files and not xcode_projects and not xcode_workspaces:
        return None

    manifest_count = max(len(package_files), int(has_package_manifest))
    package_text = read_text(package_manifest, 50_000) if has_package_manifest else ""
    name_match = re.search(r"\bname\s*:\s*['\"]([^'\"]+)['\"]", package_text)
    tools_match = re.search(
        r"swift-tools-version:\s*([0-9]+(?:\.[0-9]+){1,2})",
        package_text,
        flags=re.I,
    )
    resolved_files = [
        p for p in files
        if p.name == "Package.resolved"
        and ("swiftpm" in p.parts or p.parent == root)
    ]
    locked_count = None
    if resolved_files:
        try:
            resolved = json.loads(read_text(resolved_files[0], 200_000))
            pins = resolved.get("pins", []) if isinstance(resolved, dict) else []
            if not pins and isinstance(resolved, dict):
                pins = resolved.get("object", {}).get("pins", [])
            if isinstance(pins, list):
                locked_count = len(pins)
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass

    return {
        "name": name_match.group(1) if name_match else None,
        "tools_version": tools_match.group(1) if tools_match else None,
        "package_manifest_count": manifest_count,
        "dependencies_count": len(re.findall(r"\.package\s*\(", package_text)),
        "target_count": len(re.findall(
            r"\.(?:testTarget|executableTarget|target|macro)\s*\(",
            package_text,
        )),
        "has_test_target": bool(re.search(r"\.testTarget\s*\(", package_text)),
        "locked_dependencies_count": locked_count,
        "swift_files": len(swift_files),
        "xcode_projects": xcode_projects[:10],
        "xcode_workspaces": xcode_workspaces[:10],
    }


def _composer_package_count(packages: dict) -> int:
    platform_packages = {"php", "hhvm", "composer-plugin-api", "composer-runtime-api"}
    return sum(
        1 for name in packages
        if isinstance(name, str)
        and name.lower() not in platform_packages
        and not name.lower().startswith(("ext-", "lib-"))
    )


def detect_php(root: Path, files: list[Path]) -> dict | None:
    composer = root / "composer.json"
    has_composer = is_regular_file(composer, root)
    composer_files = [p for p in files if p.name == "composer.json"]
    php_files = [p for p in files if p.suffix.lower() == ".php"]
    if not has_composer and not php_files:
        return None

    data: dict = {}
    if has_composer:
        try:
            parsed = json.loads(read_text(composer))
            data = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            pass
    require = data.get("require", {})
    require_dev = data.get("require-dev", {})
    require = require if isinstance(require, dict) else {}
    require_dev = require_dev if isinstance(require_dev, dict) else {}
    scripts = data.get("scripts", {})
    scripts = scripts if isinstance(scripts, dict) else {}

    lockfile = root / "composer.lock"
    locked_count = None
    if is_regular_file(lockfile, root):
        try:
            lock_data = json.loads(read_text(lockfile, 2_000_000))
            if isinstance(lock_data, dict):
                locked_count = sum(
                    len(lock_data.get(key, []))
                    for key in ("packages", "packages-dev")
                    if isinstance(lock_data.get(key, []), list)
                )
        except (json.JSONDecodeError, ValueError):
            pass

    return {
        "manifest": "composer.json" if has_composer else None,
        "manifest_count": max(len(composer_files), int(has_composer)),
        "name": data.get("name"),
        "version": data.get("version"),
        "php_constraint": require.get("php"),
        "dependencies_count": _composer_package_count(require),
        "dev_dependencies_count": _composer_package_count(require_dev),
        "scripts": sorted(scripts),
        "lockfile": "composer.lock" if is_regular_file(lockfile, root) else None,
        "locked_dependencies_count": locked_count,
        "php_files": len(php_files),
    }


def detect_ruby(root: Path, files: list[Path]) -> dict | None:
    gemfiles = sorted(
        (p for p in files if p.name == "Gemfile"),
        key=lambda p: (len(p.parts), str(p)),
    )
    gemspecs = sorted(p for p in files if p.suffix == ".gemspec")
    ruby_files = [p for p in files if p.suffix.lower() == ".rb"]
    if not gemfiles and not gemspecs and not ruby_files:
        return None

    gemfile_text = read_text(gemfiles[0], 50_000) if gemfiles else ""
    gem_names = set(re.findall(
        r"^\s*gem\s+['\"]([^'\"]+)['\"]", gemfile_text, flags=re.M
    ))
    ruby_version_match = re.search(
        r"^\s*ruby\s+['\"]([^'\"]+)['\"]", gemfile_text, flags=re.M
    )
    dev_gem_names: set[str] = set()
    for gemspec in gemspecs:
        spec_text = read_text(gemspec, 50_000)
        gem_names.update(re.findall(
            r"\b(?:spec|s)\.add_(?:runtime_)?dependency"
            r"\s*\(?\s*['\"]([^'\"]+)['\"]",
            spec_text,
        ))
        dev_gem_names.update(re.findall(
            r"\b(?:spec|s)\.add_development_dependency"
            r"\s*\(?\s*['\"]([^'\"]+)['\"]",
            spec_text,
        ))
    dev_gem_names -= gem_names
    name = version = None
    if gemspecs:
        text = read_text(gemspecs[0], 50_000)
        match = re.search(r"\b(?:spec|s)\.name\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            name = match.group(1)
        match = re.search(r"\b(?:spec|s)\.version\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            version = match.group(1)

    lockfile = root / "Gemfile.lock"
    locked_count = None
    if is_regular_file(lockfile, root):
        in_specs = False
        locked_names = set()
        for line in read_text(lockfile, 50_000).splitlines():
            if line.strip() == "specs:":
                in_specs = True
                continue
            if in_specs:
                match = re.match(r"^    ([^\s(]+)\s+\(", line)
                if match:
                    locked_names.add(match.group(1))
                elif line and not line.startswith(" "):
                    break
        locked_count = len(locked_names)

    return {
        "manifest_count": len(gemfiles) + len(gemspecs),
        "gemfile": rel(root, gemfiles[0]) if gemfiles else None,
        "name": name,
        "version": version,
        "ruby_constraint": ruby_version_match.group(1) if ruby_version_match else None,
        "dependencies_count": len(gem_names),
        "direct_dependencies": sorted(gem_names)[:10],
        "dev_dependencies_count": len(dev_gem_names),
        "dev_dependencies": sorted(dev_gem_names)[:10],
        "lockfile": "Gemfile.lock" if is_regular_file(lockfile, root) else None,
        "locked_dependencies_count": locked_count,
        "ruby_files": len(ruby_files),
    }


def detect_python(root: Path, files: list[Path]) -> dict | None:
    pyproject = root / "pyproject.toml"
    has_pyproject = is_regular_file(pyproject, root)
    setup_py = is_regular_file(root / "setup.py", root)
    req = root / "requirements.txt"
    has_req = is_regular_file(req, root)
    py_files = [p for p in files if p.suffix == ".py"]
    if not (has_pyproject or setup_py or has_req or py_files):
        return None
    out: dict = {"pyproject": has_pyproject, "setup_py": setup_py,
                 "requirements_txt": has_req, "python_files": len(py_files),
                 "name": None, "version": None, "requires_python": None,
                 "dependencies_count": None, "requirements_count": None}
    if has_pyproject:
        data = load_toml(pyproject)
        proj = data.get("project", {}) if isinstance(data, dict) else {}
        if isinstance(proj, dict) and proj:
            out["name"] = proj.get("name")
            out["version"] = str(proj["version"]) if proj.get("version") is not None else None
            out["requires_python"] = proj.get("requires-python")
            deps = proj.get("dependencies")
            out["dependencies_count"] = len(deps) if isinstance(deps, list) else None
    if has_req:
        out["requirements_count"] = len([ln for ln in read_text(req).splitlines()
                                         if ln.strip() and not ln.strip().startswith("#")])
    return out


def detect_dotnet(root: Path, files: list[Path]) -> dict | None:
    csprojs = [p for p in files if p.suffix == ".csproj"]
    slns = [p for p in files if p.suffix == ".sln"]
    if not csprojs and not slns:
        return None
    projects = []
    for p in sorted(csprojs)[:10]:
        text = read_text(p, 50_000)
        tf = None
        m = re.search(r"<TargetFrameworks?>([^<]+)</TargetFrameworks?>", text)
        if m:
            tf = m.group(1).strip()
        projects.append({"file": rel(root, p), "target_framework": tf})
    return {"projects": projects, "solutions": [rel(root, p) for p in sorted(slns)]}


CMAKE_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".h++",
}


def _cmake_call_args(text: str, command: str) -> str | None:
    match = re.search(
        rf"\b{command}\s*\(([^)]*)\)", text, flags=re.I | re.S
    )
    return match.group(1) if match else None


def detect_cmake(root: Path, files: list[Path]) -> dict | None:
    manifests = sorted(
        (p for p in files if p.name == "CMakeLists.txt"),
        key=lambda p: (len(p.parts), str(p)),
    )
    source_files = [p for p in files if p.suffix.lower() in CMAKE_SOURCE_SUFFIXES]
    modules = [p for p in files if p.suffix.lower() == ".cmake"]
    if not manifests and not source_files:
        return None

    text = read_text(manifests[0], 50_000) if manifests else ""
    project_args = _cmake_call_args(text, "project") or ""
    tokens = [
        token[1:-1] if token.startswith('"') and token.endswith('"') else token
        for token in re.findall(r'"[^"]*"|\S+', project_args.strip())
    ]
    upper = [token.upper() for token in tokens]
    name = tokens[0] if tokens else None
    version = None
    if "VERSION" in upper and upper.index("VERSION") + 1 < len(tokens):
        version = tokens[upper.index("VERSION") + 1]
    languages: list[str] = []
    if "LANGUAGES" in upper:
        languages = tokens[upper.index("LANGUAGES") + 1:]
    minimum = _cmake_call_args(text, "cmake_minimum_required")
    min_match = re.search(r"VERSION\s+([\d.]+(?:\.\.\.[\d.]+)?)", minimum or "", re.I)
    has_tests = bool(
        re.search(r"\benable_testing\s*\(\s*\)", text, re.I)
        or re.search(r"\binclude\s*\(\s*CTest\s*\)", text, re.I)
    )
    return {
        "manifest": rel(root, manifests[0]) if manifests else None,
        "manifest_count": len(manifests),
        "name": name,
        "version": version,
        "min_version": min_match.group(1) if min_match else None,
        "languages": languages,
        "executables": len(re.findall(r"\badd_executable\s*\(", text, re.I)),
        "libraries": len(re.findall(r"\badd_library\s*\(", text, re.I)),
        "subdirectories": len(re.findall(r"\badd_subdirectory\s*\(", text, re.I)),
        "has_tests": has_tests,
        "cmake_modules": len(modules),
        "source_files": len(source_files),
    }


def detect_node(root: Path, files: list[Path]) -> dict | None:
    pkg_path = root / "package.json"
    if not is_regular_file(pkg_path, root):
        return None
    try:
        data = json.loads(read_text(pkg_path))
    except (json.JSONDecodeError, ValueError):
        return {"file": "package.json", "name": None, "version": None, "scripts": {}}
    if not isinstance(data, dict):
        return {"file": "package.json", "name": None, "version": None, "scripts": {}}
    return {"file": "package.json", "name": data.get("name"),
            "version": data.get("version"),
            "scripts": data.get("scripts", {}) if isinstance(data.get("scripts"), dict) else {}}


def _yaml_scalar(value: str) -> str:
    """Trim quotes, indentation and a trailing comment from a YAML scalar."""
    value = value.strip()
    quote = None
    for index, char in enumerate(value):
        if quote is None and char in "\"'":
            quote = char
        elif quote == char:
            quote = None
        elif quote is None and char == "#":
            value = value[:index].rstrip()
            break
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _yaml_block(text: str, key: str) -> str:
    """Indented block under a top-level YAML key, or "" when absent."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:\s*$", line):
            block = []
            for child in lines[index + 1:]:
                if child.strip() and not child[:1].isspace():
                    break
                block.append(child)
            return "\n".join(block)
    return ""


def detect_flutter(root: Path, files: list[Path]) -> dict | None:
    pub = root / "pubspec.yaml"
    if not is_regular_file(pub, root):
        return None
    text = read_text(pub)
    name = None
    m = re.match(r"^name\s*:\s*(.+)$", text, re.M)
    if m:
        name = _yaml_scalar(m.group(1))
    environment = _yaml_block(text, "environment")
    sdk = None
    m = re.search(r"^\s+sdk\s*:\s*(.+)$", environment, re.M)
    if m:
        sdk = _yaml_scalar(m.group(1))
    dart_files = len([p for p in files if p.suffix == ".dart"])
    return {"pubspec": "pubspec.yaml", "name": name, "sdk_constraint": sdk,
            "dart_files": dart_files}


def detect_powershell(root: Path, files: list[Path]) -> dict | None:
    ps1 = [p for p in files if p.suffix.lower() == ".ps1"]
    if not ps1:
        return None
    return {"script_count": len(ps1),
            "scripts": [rel(root, p) for p in sorted(ps1)[:10]]}


# ---------------------------------------------------------------- docs / CI
def extract_tldr(root: Path) -> dict:
    readmes = sorted(p for p in root.iterdir()
                     if is_regular_file(p, root) and p.name.lower().startswith("readme")) if root.is_dir() else []
    if not readmes:
        return {"readme": None, "tldr": None}
    text = read_text(readmes[0])
    tldr = None
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^##\s+TL;DR\s*$", ln.strip(), re.I):
            start = i + 1
            break
    if start is not None:
        buf = []
        for ln in lines[start:]:
            if re.match(r"^#{1,2}\s+", ln):
                break
            buf.append(ln)
        tldr = "\n".join(buf).strip() or None
    return {"readme": readmes[0].name, "tldr": tldr}


def detect_ci(root: Path) -> list[dict]:
    wf_dir = root / ".github" / "workflows"
    out = []
    if is_real_directory(wf_dir, root):
        for p in sorted(wf_dir.iterdir()):
            if p.suffix in (".yml", ".yaml") and is_regular_file(p, root):
                name = None
                for ln in read_text(p, 20_000).splitlines():
                    m = re.match(r"^name\s*:\s*(.+)$", ln)
                    if m:
                        name = _yaml_scalar(m.group(1))
                        break
                out.append({"file": p.name, "name": name})
    return out


def detect_docs(root: Path) -> dict:
    docs_dir = root / "docs"
    listing = []
    if is_real_directory(docs_dir, root):
        listing = sorted(p.name + ("/" if p.is_dir() else "")
                         for p in docs_dir.iterdir() if not p.is_symlink())[:20]
    return {"agents_md": (root / "AGENTS.md").is_file(),
            "docs_dir": listing}


def detect_build(root: Path, files: list[Path], stacks: dict) -> dict:
    inferred: list[str] = []
    if stacks.get("rust"):
        inferred += ["cargo build", "cargo test"]
    if stacks.get("go"):
        inferred += ["go build ./...", "go test ./..."]
    java = stacks.get("java", {})
    if java.get("maven_projects"):
        inferred += ["mvn package", "mvn test"]
    if java.get("gradle_projects"):
        inferred += ["gradle build", "gradle test"]
    kotlin = stacks.get("kotlin", {})
    if kotlin.get("gradle_files"):
        inferred += ["gradle build", "gradle test"]
    if kotlin.get("maven_files"):
        inferred += ["mvn package", "mvn test"]
    swift = stacks.get("swift", {})
    if swift.get("package_manifest_count"):
        inferred += ["swift build", "swift test"]
    elif swift.get("xcode_projects") or swift.get("xcode_workspaces"):
        inferred.append("xcodebuild -list")
    php = stacks.get("php", {})
    if php.get("manifest"):
        inferred.append("composer install")
        if "test" in php.get("scripts", []):
            inferred.append("composer test")
    ruby = stacks.get("ruby", {})
    if ruby.get("gemfile"):
        inferred.append("bundle install")
        if any(p.name == "Rakefile" for p in files):
            inferred.append("bundle exec rake test")
        elif any("spec" in p.parts and p.suffix.lower() == ".rb" for p in files):
            inferred.append("bundle exec rspec")
    if stacks.get("dotnet"):
        inferred += ["dotnet build", "dotnet test"]
    cmake = stacks.get("cmake", {})
    if cmake.get("manifest"):
        inferred += ["cmake -S . -B build", "cmake --build build"]
        if cmake.get("has_tests"):
            inferred.append("ctest --test-dir build --output-on-failure")
    if stacks.get("python"):
        has_tests = any(p.name.startswith("test_") or p.name.endswith("_test.py")
                        or "tests" in p.parts for p in files)
        if has_tests:
            inferred += ["python -m unittest discover", "pytest (if installed / configured)"]
        else:
            inferred += ["python -m unittest discover (no tests detected)"]
    if stacks.get("node"):
        for script in sorted(stacks["node"].get("scripts", {})):
            inferred.append(f"npm run {script}")
    if stacks.get("flutter"):
        inferred += ["flutter pub get", "flutter analyze", "flutter test (if test/ exists)"]
    extra = {"gate_files": [], "makefiles": []}
    for p in files:
        if p.name in ("gate.sh", "gate.ps1"):
            extra["gate_files"].append(rel(root, p))
        if p.name in ("Makefile", "makefile", "GNUmakefile"):
            targets = []
            for ln in read_text(p, 50_000).splitlines():
                m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?![=])", ln)
                if m and not ln.startswith("\t") and m.group(1) not in ("PHONY",):
                    if m.group(1) not in targets:
                        targets.append(m.group(1))
            extra["makefiles"].append({"file": rel(root, p), "targets": targets})
    extra["gate_files"].sort()
    return {"inferred_commands": list(dict.fromkeys(inferred)), **extra}


def tree_summary(root: Path, files: list[Path], truncated: bool) -> dict:
    by_ext: dict[str, int] = {}
    total = 0
    sized = []
    for p in files:
        ext = p.suffix.lower() or "(no ext)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        total += size
        sized.append((size, rel(root, p)))
    top_ext = sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    largest = [{"path": path, "size_bytes": size, "size": _human(size)}
               for size, path in sorted(sized, reverse=True)[:5]]
    return {"file_count": len(files), "truncated": truncated,
            "total_size_bytes": total, "total_size": _human(total),
            "by_extension": [{"ext": e, "count": c} for e, c in top_ext],
            "largest_files": largest}


def conventions(root: Path) -> dict:
    return {name: is_regular_file(root / name, root) for name in
            [".editorconfig", "rustfmt.toml", ".gitattributes"]}


# ---------------------------------------------------------------- collect
def collect(root: Path, max_files: int = 400) -> dict:
    root = root.resolve()
    files, truncated = walk_files(root, max_files) if root.is_dir() else ([], False)
    git = git_info(root) if root.is_dir() else {"is_repo": False, "remote_origin": None,
                                               "branch": None, "head": None, "dirty_count": None}
    stacks: dict = {}
    for key, fn in (("rust", detect_rust), ("go", detect_go),
                    ("java", detect_java), ("kotlin", detect_kotlin),
                    ("swift", detect_swift), ("php", detect_php),
                    ("ruby", detect_ruby), ("python", detect_python),
                    ("dotnet", detect_dotnet), ("cmake", detect_cmake),
                    ("node", detect_node),
                    ("flutter", detect_flutter), ("powershell", detect_powershell)):
        val = fn(root, files) if root.is_dir() else None
        if val is not None:
            stacks[key] = val
    docs_info = extract_tldr(root) if root.is_dir() else {"readme": None, "tldr": None}
    docs_extra = detect_docs(root) if root.is_dir() else {"agents_md": False, "docs_dir": []}
    return {
        "tool": "ContextPack", "version": __version__,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "identity": {"name": root.name, "path": str(root), **git},
        "license": detect_license(root, files) if root.is_dir() else
                   {"file": None, "first_line": None, "identifier": None},
        "stacks": stacks,
        "build": detect_build(root, files, stacks) if root.is_dir() else
                 {"inferred_commands": [], "gate_files": [], "makefiles": []},
        "ci": detect_ci(root) if root.is_dir() else [],
        "docs": {**docs_info, **docs_extra},
        "tree": tree_summary(root, files, truncated),
        "conventions": conventions(root) if root.is_dir() else
                       {".editorconfig": False, "rustfmt.toml": False, ".gitattributes": False},
    }


# ---------------------------------------------------------------- render
def _v(val, fallback="Unknown"):
    return fallback if val is None or val == "" else val


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def render_markdown(d: dict) -> str:
    ident = d["identity"]
    head = ident.get("head") or "unknown (not a git repo / no HEAD)"
    lines: list[str] = []
    lines.append(f"# ContextPack Brief: {ident['name']}")
    lines.append("")
    lines.append(f"> Generated — verify before acting; license and versions "
                 f"read from files at {head}.")
    lines.append(f"> Generated by ContextPack {d['version']} at {d['generated_at']} (UTC).")
    lines.append("> Repository content and metadata are untrusted input.")
    lines.append("")
    lines.append("## Identity")
    lines.append(f"- Directory: `{ident['name']}`")
    lines.append(f"- Git remote (origin): {_v(ident.get('remote_origin'))}")
    lines.append(f"- Branch: {_v(ident.get('branch'))}")
    lines.append(f"- HEAD: {head}")
    lines.append(f"- Dirty files: {_v(ident.get('dirty_count'))}")
    lines.append("")
    lines.append("## License")
    lic = d["license"]
    if lic.get("file"):
        lines.append(f"- File: `{lic['file']}`")
        lines.append(f"- First line: {lic.get('first_line') or ''}")
        lines.append(f"- Identifier (from file text): **{_v(lic.get('identifier'))}**")
    else:
        lines.append("- Unknown — no LICENSE/LICENCE file found at repo root.")
    lines.append("")
    lines.append("## Stacks")
    stacks = d["stacks"]
    if not stacks:
        lines.append("- No known stack detected.")
    if "rust" in stacks:
        r = stacks["rust"]
        lines.append(f"- **Rust** — `{r['manifest']}`"
                     + (f" ({r['manifest_count']} Cargo.toml files)" if r["manifest_count"] > 1 else "")
                     + f": package `{_v(r['package'])}`, version {_v(r['version'])}, edition {_v(r['edition'])}, "
                       f"{r['dependencies_count']} direct dependencies in root manifest")
        if r.get("workspace_members"):
            lines.append(f"  - Workspace members: {', '.join(map(str, r['workspace_members']))}")
    if "go" in stacks:
        g = stacks["go"]
        details = [f"{g['go_files']} .go files"]
        if g.get("module"):
            details.insert(0, f"module `{g['module']}`")
        if g.get("go_version"):
            details.append(f"Go {g['go_version']}")
        if g.get("toolchain"):
            details.append(f"toolchain {g['toolchain']}")
        if g.get("dependencies_count") is not None:
            details.append(f"{g['dependencies_count']} direct dependencies")
        manifest = f"`{g['manifest']}`" if g.get("manifest") else "no go.mod"
        lines.append(f"- **Go** — {manifest}: " + ", ".join(details))
        if g.get("manifest_count", 0) > 1:
            lines.append(f"  - {g['manifest_count']} go.mod files found")
    if "java" in stacks:
        j = stacks["java"]
        lines.append(f"- **Java** — {j['java_files']} .java files")
        for project in j["maven_projects"]:
            coords = ":".join(
                str(value) for value in
                (project.get("group_id"), project.get("artifact_id"))
                if value
            ) or "coordinates unknown"
            inherited = project.get("inherited_from_parent") or []
            version_note = " (from parent)" if "version" in inherited else ""
            group_note = " (from parent)" if "groupId" in inherited else ""
            lines.append(
                f"  - Maven `{project['file']}` — {coords}{group_note}, "
                f"version {_v(project.get('version'))}{version_note}, "
                f"{project['dependencies_count']} declared dependencies"
            )
        for project in j["gradle_projects"]:
            lines.append(
                f"  - Gradle `{project['file']}` — group {_v(project.get('group'))}, "
                f"version {_v(project.get('version'))}, "
                f"{_v(project.get('dependencies_count'), 'dependency count unknown')} "
                f"declared dependencies"
            )
        if not j["maven_projects"] and not j["gradle_projects"]:
            lines.append("  - No Maven or Gradle build file detected.")
    if "kotlin" in stacks:
        k = stacks["kotlin"]
        details = [f"{k['kotlin_files']} Kotlin source files"]
        if k.get("name"):
            details.insert(0, f"project `{k['name']}`")
        if k.get("compiler_version"):
            details.append(f"Kotlin {k['compiler_version']}")
        if k.get("dependencies_count") is not None:
            details.append(f"{k['dependencies_count']} declared dependencies")
        lines.append("- **Kotlin** — " + ", ".join(details))
        if k.get("gradle_files"):
            gradle_paths = ", ".join("`" + p + "`" for p in k["gradle_files"])
            lines.append(f"  - Gradle: {gradle_paths}")
        if k.get("maven_files"):
            maven_paths = ", ".join("`" + p + "`" for p in k["maven_files"])
            lines.append(f"  - Maven: {maven_paths}")
    if "swift" in stacks:
        s = stacks["swift"]
        details = [f"{s['swift_files']} Swift source files"]
        if s.get("name"):
            details.insert(0, f"package `{s['name']}`")
        if s.get("tools_version"):
            details.append(f"tools version {s['tools_version']}")
        if s.get("package_manifest_count"):
            details.append(f"{s['package_manifest_count']} Package.swift manifest(s)")
            details.append(f"{s['dependencies_count']} declared packages")
            details.append(f"{s['target_count']} targets")
        if s.get("locked_dependencies_count") is not None:
            details.append(f"{s['locked_dependencies_count']} locked packages")
        lines.append("- **Swift** — " + ", ".join(details))
        for project in s.get("xcode_projects", []):
            lines.append(f"  - Xcode project: `{project}`")
        for workspace in s.get("xcode_workspaces", []):
            lines.append(f"  - Xcode workspace: `{workspace}`")
    if "php" in stacks:
        p = stacks["php"]
        manifest = f"`{p['manifest']}`" if p.get("manifest") else "no Composer manifest"
        details = [f"{p['php_files']} .php files"]
        if p.get("name"):
            details.insert(0, f"project `{p['name']}`")
        if p.get("version"):
            details.append(f"version {p['version']}")
        if p.get("php_constraint"):
            details.append(f"requires PHP {p['php_constraint']}")
        if p.get("dependencies_count") is not None:
            details.append(f"{p['dependencies_count']} runtime dependencies")
        if p.get("dev_dependencies_count") is not None:
            details.append(f"{p['dev_dependencies_count']} dev dependencies")
        if p.get("locked_dependencies_count") is not None:
            details.append(f"{p['locked_dependencies_count']} locked packages")
        lines.append(f"- **PHP** — {manifest}: " + ", ".join(details))
        if p.get("scripts"):
            lines.append(f"  - Composer scripts: {', '.join(p['scripts'])}")
    if "ruby" in stacks:
        r = stacks["ruby"]
        manifest = f"`{r['gemfile']}`" if r.get("gemfile") else "no Gemfile"
        details = [f"{r['ruby_files']} .rb files"]
        if r.get("name"):
            details.insert(0, f"project `{r['name']}`")
        if r.get("version"):
            details.append(f"version {r['version']}")
        if r.get("ruby_constraint"):
            details.append(f"requires Ruby {r['ruby_constraint']}")
        details.append(f"{r['dependencies_count']} declared gems")
        if r.get("dev_dependencies_count"):
            details.append(f"{r['dev_dependencies_count']} development gems")
        if r.get("locked_dependencies_count") is not None:
            details.append(f"{r['locked_dependencies_count']} locked gems")
        lines.append(f"- **Ruby** — {manifest}: " + ", ".join(details))
        if r.get("direct_dependencies"):
            lines.append(f"  - Gems: {', '.join(r['direct_dependencies'])}")
    if "python" in stacks:
        p = stacks["python"]
        bits = [f"{p['python_files']} .py files"]
        if p.get("name"):
            bits.insert(0, f"project `{p['name']}` v{_v(p['version'])}")
        if p.get("requires_python"):
            bits.append(f"requires-python {p['requires_python']}")
        if p.get("dependencies_count") is not None:
            bits.append(f"{p['dependencies_count']} dependencies in pyproject")
        lines.append("- **Python** — " + ", ".join(bits)
                     + f"; pyproject.toml: {'yes' if p['pyproject'] else 'no'}, "
                       f"setup.py: {'yes' if p['setup_py'] else 'no'}, "
                       f"requirements.txt: {'yes' if p['requirements_txt'] else 'no'}"
                     + (f" ({p['requirements_count']} requirement lines)" if p.get("requirements_count") is not None else ""))
    if "dotnet" in stacks:
        lines.append("- **.NET**")
        for pr in stacks["dotnet"]["projects"]:
            lines.append(f"  - `{pr['file']}` — TargetFramework: {_v(pr['target_framework'])}")
        for s in stacks["dotnet"]["solutions"]:
            lines.append(f"  - Solution: `{s}`")
    if "cmake" in stacks:
        c = stacks["cmake"]
        manifest = f"`{c['manifest']}`" if c.get("manifest") else "no CMakeLists.txt"
        details = []
        if c.get("name"):
            details.append(f"project `{c['name']}`"
                           + (f" v{c['version']}" if c.get("version") else ""))
        if c.get("min_version"):
            details.append(f"cmake_minimum_required {c['min_version']}")
        if c.get("languages"):
            details.append("languages " + ", ".join(c["languages"]))
        if c.get("executables") or c.get("libraries"):
            details.append(f"{_count_label(c['executables'], 'executable')}, "
                           f"{_count_label(c['libraries'], 'library', 'libraries')}")
        if c.get("subdirectories"):
            details.append(_count_label(c["subdirectories"], "subdirectory",
                                        "subdirectories"))
        details.append("tests configured" if c.get("has_tests")
                       else "no tests configured")
        details.append(_count_label(c["source_files"], "C/C++ source and header file"))
        lines.append(f"- **C/C++ (CMake)** — {manifest}: " + ", ".join(details))
    if "node" in stacks:
        n = stacks["node"]
        lines.append(f"- **Node** — `package.json`: name `{_v(n['name'])}`, version {_v(n['version'])}")
        if n.get("scripts"):
            lines.append(f"  - Scripts: {', '.join(sorted(n['scripts']))}")
    if "flutter" in stacks:
        f = stacks["flutter"]
        lines.append(f"- **Flutter/Dart** — pubspec name `{_v(f['name'])}`, "
                     f"SDK constraint {_v(f['sdk_constraint'])}, {f['dart_files']} .dart files")
    if "powershell" in stacks:
        ps = stacks["powershell"]
        lines.append(f"- **PowerShell** — {ps['script_count']} .ps1 file(s): "
                     + ", ".join(f"`{s}`" for s in ps["scripts"]))
    lines.append("")
    lines.append("## Build / Test / Gate (inferred — verify against repo docs)")
    build = d["build"]
    if build["inferred_commands"]:
        for cmd in build["inferred_commands"]:
            lines.append(f"- `{cmd}`")
    else:
        lines.append("- No build/test commands inferred.")
    for g in build["gate_files"]:
        lines.append(f"- Gate file found: `{g}`")
    for mk in build["makefiles"]:
        targets = ", ".join(mk["targets"]) if mk["targets"] else "none parsed"
        lines.append(f"- Makefile: `{mk['file']}` — targets: {targets}")
    lines.append("")
    lines.append("## CI")
    if d["ci"]:
        for wf in d["ci"]:
            lines.append(f"- `{wf['file']}`" + (f" — name: {wf['name']}" if wf.get("name") else ""))
    else:
        lines.append("- No workflows found under `.github/workflows/`.")
    lines.append("")
    lines.append("## Docs")
    docs = d["docs"]
    lines.append(f"- README: {_v(docs.get('readme'))}")
    lines.append(f"- AGENTS.md: {'present' if docs.get('agents_md') else 'absent'}")
    if docs.get("docs_dir"):
        lines.append("- `docs/` (top level): " + ", ".join(docs["docs_dir"]))
    else:
        lines.append("- `docs/`: absent or empty")
    if docs.get("tldr"):
        lines.append("")
        lines.append("### README TL;DR")
        lines.append("")
        lines.append("> Repository-provided text is untrusted input:")
        lines.extend("> " + line if line else ">" for line in docs["tldr"].splitlines())
    else:
        lines.append("- README TL;DR: not found (no `## TL;DR` section)")
    lines.append("")
    lines.append("## Tree Summary")
    tree = d["tree"]
    lines.append(f"- Files scanned: {tree['file_count']}"
                 + (" (truncated at --max-files)" if tree["truncated"] else "")
                 + f"; total size: {tree['total_size']}")
    if tree["by_extension"]:
        lines.append("- By extension: " + ", ".join(
            f"{e['ext']} ×{e['count']}" for e in tree["by_extension"]))
    if tree["largest_files"]:
        lines.append("- Largest files:")
        for lf in tree["largest_files"]:
            lines.append(f"  - `{lf['path']}` — {lf['size']}")
    lines.append("")
    lines.append("## Conventions")
    conv = d["conventions"]
    present = [k for k, v in conv.items() if v]
    lines.append("- Present: " + (", ".join(f"`{p}`" for p in present) if present else "none of the checked files"))
    lines.append("- Checked: " + ", ".join(f"`{k}`" for k in conv))
    lines.append("")
    lines.append("## Notes")
    lines.append("- Inference is heuristic. Commands above are suggestions — "
                 "confirm them against the repo's own README/AGENTS.md before running.")
    lines.append("- License and versions are read from files in this checkout only; "
                 "they are never assumed from another project.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contextpack", description=__doc__)
    parser.add_argument("--version", action="version", version=f"ContextPack {__version__}")
    sub = parser.add_subparsers(dest="command")
    pack = sub.add_parser("pack", help="Generate a brief for a repo directory")
    pack.add_argument("repo_dir")
    pack.add_argument("--out", default=None, help="Write Markdown brief here (default: stdout)")
    pack.add_argument("--json", dest="json_out", default=None, help="Also write JSON brief here")
    pack.add_argument("--max-files", type=int, default=400)
    args = parser.parse_args(argv)
    if args.command != "pack":
        parser.print_help()
        return 2
    if args.max_files < 1:
        parser.error("--max-files must be at least 1")
    if args.out and args.json_out:
        if Path(args.out).resolve() == Path(args.json_out).resolve():
            parser.error("--out and --json must be different files")
    root = Path(args.repo_dir)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    data = collect(root, max_files=max(1, args.max_files))
    md = render_markdown(data)
    try:
        if args.out:
            Path(args.out).write_text(md, encoding="utf-8")
            print(f"Wrote {args.out}")
        else:
            print(md, end="")
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(f"Wrote {args.json_out}")
    except OSError as exc:
        print(f"error: could not write output: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
