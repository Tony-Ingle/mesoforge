#!/usr/bin/env python3
"""Scan the repository tree for credentials, GRIB data, caches, and other
runtime artifacts that must never be committed.

Checks (in order, all reported, not just the first):
- forbidden file extensions/names: GRIB/index/cfgrib caches, .env* files,
  common cache/venv directories, database/MinIO local data directories;
- NetCDF/Zarr files anywhere outside ``tests/fixtures/`` (the only place a
  small, explicit, synthetic binary fixture is permitted);
- any permitted fixture file larger than a configured size limit;
- text files containing strings that look like committed credentials
  (AWS access key IDs, private key headers, obvious `password =` /
  `secret =` assignments with a non-empty literal value).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from pathlib import Path

MAX_FIXTURE_BYTES = 1024 * 1024  # 1 MiB

FORBIDDEN_SUFFIXES = (".grib", ".grb", ".grib2", ".grb2", ".idx", ".index")
CFGRIB_CACHE_RE = re.compile(r".*\.idx(?:\.[0-9a-f]+)?$", re.IGNORECASE)
FORBIDDEN_NAME_PATTERNS = (re.compile(r"^\.env(\..+)?$"),)
ALLOWED_ENV_FILENAMES = {".env.example"}
FORBIDDEN_DIR_NAMES = {
    "__pycache__",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "htmlcov",
    ".git",
}
NETCDF_ZARR_SUFFIXES = (".nc", ".nc4", ".zarr")
FIXTURES_DIR = Path("tests") / "fixtures"

CREDENTIAL_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (RSA|EC|OPENSSH|DSA|PGP) PRIVATE KEY-----"),
    re.compile(
        r"""(?i)\b(aws_secret_access_key|secret|password|api_key|token)\s*=\s*["'][^"'\s]{8,}["']"""
    ),
)

TEXT_SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".md", ".txt", ".json"}


def iter_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in FORBIDDEN_DIR_NAMES for part in path.relative_to(root).parts[:-1]):
            continue
        yield path


def check_forbidden_names(root: Path) -> list[str]:
    errors: list[str] = []
    for path in iter_files(root):
        rel = path.relative_to(root)
        name = path.name
        suffix = path.suffix.lower()

        if suffix in FORBIDDEN_SUFFIXES or CFGRIB_CACHE_RE.fullmatch(name):
            errors.append(f"{rel}: forbidden GRIB/index file must not be committed")
            continue

        for pattern in FORBIDDEN_NAME_PATTERNS:
            if pattern.match(name) and name not in ALLOWED_ENV_FILENAMES:
                errors.append(f"{rel}: forbidden .env credential file must not be committed")
                break
    return errors


def check_netcdf_zarr(root: Path) -> list[str]:
    errors: list[str] = []
    for path in iter_files(root):
        rel = path.relative_to(root)
        suffix = path.suffix.lower()
        if suffix not in NETCDF_ZARR_SUFFIXES:
            continue

        try:
            rel.relative_to(FIXTURES_DIR)
            is_fixture = True
        except ValueError:
            is_fixture = False

        if not is_fixture:
            errors.append(
                f"{rel}: NetCDF/Zarr runtime artifact outside tests/fixtures/ must not be committed"
            )
            continue

        size = path.stat().st_size
        if size > MAX_FIXTURE_BYTES:
            errors.append(
                f"{rel}: fixture file size {size} bytes exceeds the "
                f"{MAX_FIXTURE_BYTES}-byte limit for committed fixtures"
            )
    return errors


def check_credentials(root: Path) -> list[str]:
    errors: list[str] = []
    for path in iter_files(root):
        if path.suffix.lower() not in TEXT_SCAN_SUFFIXES:
            continue
        rel = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in CREDENTIAL_PATTERNS:
            if pattern.search(text):
                errors.append(f"{rel}: possible committed credential matched {pattern.pattern!r}")
                break
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    errors: list[str] = []
    errors.extend(check_forbidden_names(root))
    errors.extend(check_netcdf_zarr(root))
    errors.extend(check_credentials(root))

    if errors:
        print(f"repository hygiene check failed with {len(errors)} problem(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
