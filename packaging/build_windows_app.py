"""Build the Windows app (64-bit Windows 10/11) with PyInstaller. Run on Windows.

    py -3.12 -m venv .venv-win && .venv-win\\Scripts\\pip install -r packaging\\windows-lock.txt
    .venv-win\\Scripts\\python packaging\\build_windows_app.py --out dist-win

Produces "Banana Split Redux\\Banana Split Redux.exe" (Python, Qt and FFmpeg included),
Banana-Split-Redux-<version>-windows-x64.zip and, when Inno Setup's ISCC is available,
Banana-Split-Redux-<version>-windows-x64-setup.exe. Nothing is code-signed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from bsr import ENGINE_VERSION  # noqa: E402

APP_NAME = "Banana Split Redux"


def version_file(path: str):
    parts = [int(x) for x in ENGINE_VERSION.split(".")] + [0] * 4
    v = tuple(parts[:4])
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Banana Split Redux'),
    StringStruct('FileDescription', '{APP_NAME}'),
    StringStruct('FileVersion', '{ENGINE_VERSION}'),
    StringStruct('InternalName', 'BananaSplitRedux'),
    StringStruct('OriginalFilename', '{APP_NAME}.exe'),
    StringStruct('ProductName', '{APP_NAME}'),
    StringStruct('ProductVersion', '{ENGINE_VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")


def make_ico(path: str):
    from PIL import Image
    im = Image.open(os.path.join(ROOT, "bsr_ui", "assets", "icon.png")).convert("RGBA")
    im.save(path, format="ICO", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])


def find_iscc():
    for p in (shutil.which("iscc"), shutil.which("ISCC"),
              os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Inno Setup 6", "ISCC.exe"),
              os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Inno Setup 6", "ISCC.exe")):
        if p and os.path.exists(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist-win"))
    ap.add_argument("--no-installer", action="store_true")
    a = ap.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Build the Windows app on Windows (GitHub Actions: .github/workflows/windows.yml).")
    out = os.path.abspath(a.out)
    work = os.path.join(out, "build")
    os.makedirs(work, exist_ok=True)
    ico = os.path.join(work, "BananaSplitRedux.ico")
    make_ico(ico)
    vf = os.path.join(work, "version.txt")
    version_file(vf)
    import PyInstaller.__main__
    PyInstaller.__main__.run([
        os.path.join(HERE, "windows", "launcher.py"), "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", APP_NAME, "--icon", ico, "--version-file", vf,
        "--distpath", out, "--workpath", os.path.join(work, "pyinstaller"), "--specpath", work,
        "--paths", ROOT, "--collect-submodules", "bsr", "--collect-submodules", "bsr_ui",
        "--add-data", f"{os.path.join(ROOT, 'bsr_ui', 'assets')}{os.pathsep}bsr_ui/assets",
        "--add-data", f"{os.path.join(ROOT, 'README.md')}{os.pathsep}.",
        "--exclude-module", "tkinter", "--exclude-module", "PySide6.QtQml", "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.Qt3DCore",
    ])
    app_dir = os.path.join(out, APP_NAME)
    stem = f"Banana-Split-Redux-{ENGINE_VERSION}-windows-x64"
    zip_path = os.path.join(out, f"{stem}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dp, _, files in os.walk(app_dir):
            for f in files:
                full = os.path.join(dp, f)
                z.write(full, os.path.relpath(full, out))
    print("built", zip_path, f"{os.path.getsize(zip_path) / 1e6:.0f} MB")
    iscc = None if a.no_installer else find_iscc()
    if iscc:
        subprocess.check_call([iscc, f"/DAppVersion={ENGINE_VERSION}", f"/DSourceDir={app_dir}",
                               f"/DOutputDir={out}", f"/DOutputName={stem}-setup", f"/DIconFile={ico}",
                               os.path.join(HERE, "windows", "installer.iss")])
        print("built", os.path.join(out, f"{stem}-setup.exe"))
    elif not a.no_installer:
        print("Inno Setup not found: skipped the installer (the zip is complete on its own)")


if __name__ == "__main__":
    main()
