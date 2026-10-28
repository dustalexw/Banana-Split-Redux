"""Make a release app by updating a previous release's bundle with this repository's code.

Releases reuse the pinned native runtime (Python, FFmpeg, Qt) of an earlier, verified
app and replace only the app code, documentation, version and code hashes:

    python3 packaging/update_app_bundle.py --base Banana-Split-Redux-0.4.6-macos-arm64.zip --out dist

Writes Banana-Split-Redux-<version>-macos-arm64.zip, -source.zip (from git) and -SHA256.txt.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from bsr import ENGINE_VERSION  # noqa: E402

APP = "Banana Split Redux.app"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="previous release's macos-arm64 zip")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--note", default="", help="one line for build-manifest.txt")
    a = ap.parse_args()
    version = ENGINE_VERSION
    os.makedirs(a.out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="bsr-release-")
    subprocess.check_call(["unzip", "-q", os.path.abspath(a.base), "-x", "*/._*", "-d", work])
    app = os.path.join(work, APP)
    res = os.path.join(app, "Contents", "Resources")
    hashes_path = os.path.join(res, "app-code-sha256.json")
    old = json.load(open(hashes_path))
    for rel, digest in old.items():          # the base bundle must be the code it claims to be
        if sha256(os.path.join(res, rel)) != digest:
            raise SystemExit(f"base bundle file changed: {rel}")
    code = os.path.join(res, "app")
    for pkg in ("bsr", "bsr_ui"):
        shutil.rmtree(os.path.join(code, pkg))
        shutil.copytree(os.path.join(ROOT, pkg), os.path.join(code, pkg),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for p in (os.path.join(code, pkg) for pkg in ("bsr", "bsr_ui")):
        for dp, _, files in os.walk(p):
            for f in files:
                if f.endswith(".py"):     # the bundle runs Python 3.12
                    ast.parse(open(os.path.join(dp, f), encoding="utf-8").read(), feature_version=(3, 12))
    for doc in ("README.md", "docs"):
        dst = os.path.join(res, doc)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.exists(dst):
            os.remove(dst)
        src = os.path.join(ROOT, doc)
        (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, dst)
    files = sorted(os.path.relpath(os.path.join(dp, f), res) for dp, _, fs in os.walk(code) for f in fs)
    with open(hashes_path, "w") as f:
        json.dump({rel: sha256(os.path.join(res, rel)) for rel in files}, f, indent=2)
        f.write("\n")
    plist = os.path.join(app, "Contents", "Info.plist")
    with open(plist, "rb") as f:
        info = plistlib.load(f)
    previous = info.get("CFBundleShortVersionString")
    info["CFBundleShortVersionString"] = info["CFBundleVersion"] = version
    with open(plist, "wb") as f:
        plistlib.dump(info, f)
    manifest = os.path.join(res, "build-manifest.txt")
    text = open(manifest).read().replace(f"engine: {previous}", f"engine: {version}")
    text = text.rstrip("\n") + f"\n{version}: {a.note or 'App code updated.'} Original native runtime reused (from {previous}).\n"
    open(manifest, "w").write(text)

    stem = f"Banana-Split-Redux-{version}"
    app_zip = os.path.join(os.path.abspath(a.out), f"{stem}-macos-arm64.zip")
    src_zip = os.path.join(os.path.abspath(a.out), f"{stem}-source.zip")
    for p in (app_zip, src_zip):
        if os.path.exists(p):
            os.remove(p)
    subprocess.check_call(["zip", "-qry", "-X", app_zip, APP], cwd=work, env={**os.environ, "TZ": "UTC"})
    subprocess.check_call(["git", "archive", "--format=zip", f"--prefix={stem}/", "-o", src_zip, "HEAD"], cwd=ROOT)
    with open(os.path.join(a.out, f"{stem}-SHA256.txt"), "w") as f:
        for p in (app_zip, src_zip):
            f.write(f"{sha256(p)}  {os.path.basename(p)}\n")
    shutil.rmtree(work)
    print(f"{stem}: {len(files)} app files, {os.path.getsize(app_zip) / 1e6:.0f} MB zip, runtime from {previous}")


if __name__ == "__main__":
    main()
