# Changelog

All notable changes to ContextPack are documented here.

## Unreleased

- Detect Go modules and Java Maven/Gradle builds; infer their build and
  test commands and document parsing limits.
- Detect PHP Composer and Ruby Bundler projects, report manifest and lock
  metadata, infer available test commands, and document parser limits.

## 0.2.0 — 2026-11-25

- Redact credentials and query strings from URL-style Git remotes.
- Skip symlinked inputs, quote README TL;DR text as untrusted input, and
  bound text reads to avoid pulling external files or loading huge files.
- Reject invalid output settings and missing repo paths with clear
  non-zero exits; add regression coverage for these cases.
- Detect standard `UNLICENSE` files and document the tree-summary
  extension limit; mark repository metadata as untrusted in briefs.
- Document source installation and smoke-test the installed CLI in CI.

## 0.1.0 — 2026-10-24

- Initial release.
- `pack` command generating a Markdown brief and optional mirrored JSON
  for any repo directory, using only the Python standard library.
- Identity from git (remote, branch, HEAD, dirty count), license read
  from the license file's own text (MIT/GPL/Apache/BSD/MPL and more) —
  never assumed from another project.
- Stack detection for Rust, Python, .NET, Node, Flutter/Dart, and
  PowerShell, with pinned names/versions/frameworks read from manifests.
- Inferred build/test commands per stack, plus discovered
  `gate.sh`/`gate.ps1` files and Makefile targets.
- CI workflow listing, README `## TL;DR` extraction, AGENTS.md and
  `docs/` inventory, tree summary (extensions, largest files, total
  size), and convention-file checks.
- Fixture-based unittest suite: Rust, Python+GPL, empty directory,
  JSON/Markdown agreement, CLI, and multi-stack detection.
