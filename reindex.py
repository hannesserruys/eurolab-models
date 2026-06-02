#!/usr/bin/env python3
"""Regenerate ``index.json`` for a EUROLAB model repository.

This script is the single source of truth for the model-repo listing. It is
**self-contained and depends only on the Python standard library** so that it
can be copied verbatim into the ``eurolab-models`` repository and run by a
GitHub Action on every push, *and* imported by the desktop app
(``from scripts.reindex import reindex``) — one implementation, no duplication.

It walks ``<repo>/models/**/*.zip``, reads each model bundle's descriptor
(``.eurolab_meta.json`` at the bundle's ``<country>/<leaf>/`` root), and writes
``<repo>/index.json``. The listing is therefore a pure projection of the folder
tree — nothing is hand-maintained.

Layout assumed in the repo
--------------------------
    <repo>/
      index.json                                                 # written here
      models/<python_tag>/<platform_tag>/<country>/<dataset>__<prep_hash>.zip

``index.json`` schema::

    {
      "format_version": 1,
      "generated_at": "<latest bundle version_tag>",
      "bundles": [
        {
          "kind": "model",
          "country": "EE",
          "dataset": "EE_2024_f1_2015_03_e2",
          "prep_hash": "...",
          "prep_signature_hash": "...",
          "python_tag": "cp312",
          "platform_tag": "win_amd64",
          "path": "models/cp312/win_amd64/EE/EE_2024_f1_2015_03_e2__<hash>.zip",
          "size": 12345,
          "sha256": "<whole-zip sha256>",
          "version_tag": "20260218T145400Z"
        },
        ...
      ]
    }

Each bundle's whole-zip ``sha256`` is a fast pre-extraction check; authoritative
integrity is the per-file ``files`` map carried inside each bundle's descriptor
and verified by the client on install.

Usage::

    python reindex.py [REPO_DIR]      # defaults to the current directory
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile

INDEX_FORMAT_VERSION = 1
INDEX_FILENAME = "index.json"
MODELS_DIRNAME = "models"
MODEL_DESCRIPTOR = ".eurolab_meta.json"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_descriptor(zf: zipfile.ZipFile) -> dict:
    """Return the model descriptor (``.eurolab_meta.json``) from a bundle zip.

    Picks the descriptor nearest the zip root (the ``<country>/<leaf>/``
    leaf-root copy), tolerating any stray nested copies.
    """
    candidates = [
        n for n in zf.namelist()
        if n == MODEL_DESCRIPTOR or n.endswith("/" + MODEL_DESCRIPTOR)
    ]
    if not candidates:
        raise ValueError(f"no {MODEL_DESCRIPTOR} found in bundle")
    candidates.sort(key=lambda n: (n.count("/"), len(n)))
    raw = zf.read(candidates[0])
    descriptor = json.loads(raw)
    if not isinstance(descriptor, dict):
        raise ValueError(f"{MODEL_DESCRIPTOR} is not a JSON object")
    return descriptor


def _build_entry(repo_dir: str, zip_path: str, *, log=print) -> dict | None:
    rel_path = os.path.relpath(zip_path, repo_dir).replace(os.sep, "/")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            descriptor = _read_descriptor(zf)
    except Exception as exc:  # noqa: BLE001 - skip unreadable bundles, keep going
        log(f"[reindex] WARNING: skipping {rel_path}: {exc}")
        return None

    country = descriptor.get("country")
    dataset = descriptor.get("dataset")
    prep_hash = descriptor.get("prep_hash")
    python_tag = descriptor.get("python_tag")
    platform_tag = descriptor.get("platform_tag")
    if not (country and dataset and prep_hash and python_tag and platform_tag):
        log(f"[reindex] WARNING: skipping {rel_path}: descriptor is missing "
            f"country/dataset/prep_hash/python_tag/platform_tag")
        return None

    # Belt and suspenders: the folder path encodes the tags too — warn on drift
    # but trust the descriptor (it is what the client verifies against).
    expected_prefix = f"{MODELS_DIRNAME}/{python_tag}/{platform_tag}/"
    if not rel_path.startswith(expected_prefix):
        log(f"[reindex] WARNING: {rel_path} is not under {expected_prefix} "
            f"(descriptor tags {python_tag}/{platform_tag})")

    version_tag = (
        descriptor.get("release_tag")
        or descriptor.get("version_tag")
        or descriptor.get("exported_at")
    )
    return {
        "kind": descriptor.get("bundle_kind") or "model",
        "country": country,
        "dataset": dataset,
        "prep_hash": prep_hash,
        "prep_signature_hash": descriptor.get("prep_signature_hash"),
        "python_tag": python_tag,
        "platform_tag": platform_tag,
        "path": rel_path,
        "size": os.path.getsize(zip_path),
        "sha256": _sha256_file(zip_path),
        "version_tag": version_tag,
    }


def build_index(repo_dir: str, *, log=print) -> dict:
    """Return the index document for *repo_dir* without writing it."""
    models_root = os.path.join(repo_dir, MODELS_DIRNAME)
    zip_paths: list[str] = []
    if os.path.isdir(models_root):
        for root, _dirs, fnames in os.walk(models_root):
            for fname in fnames:
                if fname.lower().endswith(".zip"):
                    zip_paths.append(os.path.join(root, fname))
    zip_paths.sort()

    bundles: list[dict] = []
    for zip_path in zip_paths:
        entry = _build_entry(repo_dir, zip_path, log=log)
        if entry is not None:
            bundles.append(entry)

    bundles.sort(key=lambda e: (
        e["country"], e["dataset"], e["python_tag"],
        e["platform_tag"], e["prep_hash"],
    ))

    # ``generated_at`` is the newest bundle version_tag — a deterministic
    # "as of" stamp (no wall-clock), so re-running without bundle changes
    # reproduces an identical index.json.
    generated_at = max((e["version_tag"] for e in bundles if e["version_tag"]),
                       default=None)
    return {
        "format_version": INDEX_FORMAT_VERSION,
        "generated_at": generated_at,
        "bundles": bundles,
    }


def reindex(repo_dir: str, *, log=print) -> str:
    """Regenerate ``<repo_dir>/index.json`` and return its path."""
    index = build_index(repo_dir, log=log)
    os.makedirs(repo_dir, exist_ok=True)
    index_path = os.path.join(repo_dir, INDEX_FILENAME)
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
        fh.write("\n")
    log(f"[reindex] wrote {index_path} ({len(index['bundles'])} bundle(s))")
    return index_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate index.json for a EUROLAB model repository.",
    )
    parser.add_argument(
        "repo_dir", nargs="?", default=".",
        help="Path to the model repository root (default: current directory).",
    )
    args = parser.parse_args(argv)
    reindex(os.path.abspath(args.repo_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
