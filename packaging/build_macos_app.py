"""Build a self-contained 'Banana Split Redux.app' for Apple-silicon Macs (macOS 14+).

Runs on macOS or Linux (no compiler needed): it assembles a relocatable CPython build
(python-build-standalone) with the official macOS wheels of PyAV (FFmpeg libraries
included), NumPy, Pillow and PySide6-Essentials and Addons, plus this repository's code.

    python3 packaging/build_macos_app.py [--out DIR] [--cache DIR]

Versions are pinned in packaging/macos-lock.txt. The bundle is not code-signed or
notarized; the native libraries keep the signatures they ship with.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from bsr import ENGINE_VERSION  # noqa: E402

APP_NAME = "Banana Split Redux"
BUNDLE_ID = "local.bananasplitredux.app"
PLATFORM = "macosx_14_0_arm64"
PY_TAG = "3.12"


def read_lock():
    py, pkgs = None, []
    for line in open(os.path.join(HERE, "macos-lock.txt"), encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        if line.startswith("python-build-standalone"):
            _, rel, cpy, triple, flavor = line.split()
            py = (rel, cpy.split("-")[1], triple, flavor)
        else:
            pkgs.append(line)
    return py, pkgs


def fetch(url, dest):
    if os.path.exists(dest):
        return dest
    print("download", url)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)
    return dest


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--cache", default=os.path.join(ROOT, "build", "cache"))
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    (rel, pyver, triple, flavor), pkgs = read_lock()

    # 1. relocatable Python
    pyname = f"cpython-{pyver}+{rel}-{triple}-{flavor}.tar.gz"
    pytar = fetch(f"https://github.com/astral-sh/python-build-standalone/releases/download/{rel}/{pyname}",
                  os.path.join(a.cache, pyname))

    # 2. macOS wheels (binary only, exact pins, no dependency resolution surprises)
    wdir = os.path.join(a.cache, "wheels")
    os.makedirs(wdir, exist_ok=True)
    subprocess.check_call([sys.executable, "-m", "pip", "download", "-q", "--only-binary=:all:", "--no-deps",
                           "--platform", PLATFORM, "--python-version", PY_TAG, "--implementation", "cp",
                           "-d", wdir, *pkgs])
    wheels = sorted(os.path.join(wdir, w) for w in os.listdir(wdir) if w.endswith(".whl"))
    wanted = {p.split("==")[0].lower().replace("-", "_") for p in pkgs}
    wheels = [w for w in wheels if os.path.basename(w).split("-")[0].lower() in wanted]
    if len(wheels) != len(pkgs):
        raise SystemExit(f"expected {len(pkgs)} wheels, found {[os.path.basename(w) for w in wheels]}")

    # 3. assemble the bundle
    app = os.path.join(a.out, APP_NAME + ".app")
    if os.path.exists(app):
        shutil.rmtree(app)
    contents = os.path.join(app, "Contents")
    macos = os.path.join(contents, "MacOS")
    res = os.path.join(contents, "Resources")
    os.makedirs(macos)
    os.makedirs(res)
    with tarfile.open(pytar) as t:
        t.extractall(res)                       # -> Resources/python (trusted, pinned archive)
    pyroot = os.path.join(res, "python")
    site = os.path.join(res, "site-packages")
    os.makedirs(site)
    for w in wheels:
        with zipfile.ZipFile(w) as z:
            for info in z.infolist():
                z.extract(info, site)
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    os.chmod(os.path.join(site, info.filename), mode)
    # strip parts of the Python runtime the app never uses
    for rel_ in ("lib/python3.12/test", "lib/python3.12/idlelib", "lib/python3.12/tkinter",
                 "lib/python3.12/turtledemo", "lib/python3.12/ensurepip", "lib/tcl8.6", "lib/tk8.6",
                 "lib/itcl4.2.4", "lib/tcl8", "share"):
        p = os.path.join(pyroot, rel_)
        if os.path.isdir(p):
            shutil.rmtree(p)
    prune_qt(os.path.join(site, "PySide6"))
    appcode = os.path.join(res, "app")
    for pkg in ("bsr", "bsr_ui"):
        shutil.copytree(os.path.join(ROOT, pkg), os.path.join(appcode, pkg),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for doc in ("README.md", "docs"):
        src = os.path.join(ROOT, doc)
        (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, os.path.join(res, doc))
    subprocess.check_call([sys.executable, os.path.join(HERE, "make_icon.py"),
                           os.path.join(res, "icon.png"), os.path.join(res, "AppIcon.icns")])

    launcher = os.path.join(macos, "BananaSplitRedux")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(LAUNCHER)
    os.chmod(launcher, 0o755)
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump({
            "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME, "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleExecutable": "BananaSplitRedux", "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL", "CFBundleVersion": ENGINE_VERSION,
            "CFBundleShortVersionString": ENGINE_VERSION, "LSMinimumSystemVersion": "14.0",
            "LSArchitecturePriority": ["arm64"], "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.video",
            "CFBundleDocumentTypes": [{"CFBundleTypeName": "Video", "CFBundleTypeRole": "Viewer",
                                       "LSHandlerRank": "Alternate",
                                       "LSItemContentTypes": ["public.movie", "org.matroska.mkv", "public.mpeg-4"]}],
        }, f)
    with open(os.path.join(contents, "PkgInfo"), "w") as f:
        f.write("APPL????")
    manifest = {"engine": ENGINE_VERSION, "python": pyname, "python_sha256": sha256(pytar),
                "wheels": {os.path.basename(w): sha256(w) for w in wheels}}
    with open(os.path.join(res, "build-manifest.txt"), "w") as f:
        for k, v in manifest.items():
            f.write(f"{k}: {v}\n")
    size = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(app) for fn in fns
               if not os.path.islink(os.path.join(dp, fn)))
    print(f"built {app} ({size/1e6:.0f} MB)")


# The app uses Qt Widgets and Qt Multimedia. Keep their runtime dependencies and
# the plugins Qt Widgets loads on macOS; drop QML/Quick, designer tools and the rest.
QT_KEEP_FRAMEWORKS = {"QtCore", "QtGui", "QtWidgets", "QtMultimedia", "QtDBus", "QtPrintSupport", "QtOpenGL",
                      "QtOpenGLWidgets", "QtNetwork", "QtSvg", "QtConcurrent"}
QT_KEEP_PLUGINS = {"platforms", "styles", "multimedia", "imageformats", "iconengines", "platforminputcontexts", "generic",
                   "permissions", "tls", "networkinformation"}


def prune_qt(pyside):
    lib = os.path.join(pyside, "Qt", "lib")
    for name in os.listdir(lib):
        if name.endswith(".framework") and name[:-10] not in QT_KEEP_FRAMEWORKS:
            shutil.rmtree(os.path.join(lib, name))
    plug = os.path.join(pyside, "Qt", "plugins")
    for name in os.listdir(plug):
        if name not in QT_KEEP_PLUGINS:
            shutil.rmtree(os.path.join(plug, name))
    for name in ("qml", "metatypes", "typesystems", "glue", "include", "scripts", "support"):
        p = os.path.join(pyside, name)
        if os.path.isdir(p):
            shutil.rmtree(p)
    keep_mods = {m for m in QT_KEEP_FRAMEWORKS}
    for name in os.listdir(pyside):
        p = os.path.join(pyside, name)
        if name.endswith(".app"):
            shutil.rmtree(p)
        elif name.endswith(".abi3.so") and name.split(".")[0] not in keep_mods:
            os.remove(p)
        elif name.endswith(".pyi") and name.split(".")[0] not in keep_mods:
            os.remove(p)
        elif os.path.isfile(p) and os.access(p, os.X_OK) and "." not in name:
            os.remove(p)   # command-line tools: designer, lupdate, qmlls, uic, rcc, ...


LAUNCHER = r'''#!/bin/bash
# Banana Split Redux launcher: runs the bundled Python with the bundled packages only.
HERE="$(cd "$(dirname "$0")/.." && pwd)"
RES="$HERE/Resources"
export PYTHONPATH="$RES/app:$RES/site-packages"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export BSR_ICON="$RES/icon.png"
unset PYTHONHOME
LOG="$HOME/Library/Logs/BananaSplitRedux"
mkdir -p "$LOG"
"$RES/python/bin/python3" -m bsr_ui "$@" 2>>"$LOG/launch.log"
code=$?
if [ $code -ne 0 ]; then
  /usr/bin/osascript -e "display alert \"Banana Split Redux could not start\" message \"Details: ~/Library/Logs/BananaSplitRedux/launch.log (exit $code)\"" >/dev/null 2>&1
fi
exit $code
'''

if __name__ == "__main__":
    main()
