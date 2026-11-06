# PyInstaller spec - build on each target OS (PyInstaller does not cross-compile).
#   macOS:   packaging/build_mac.sh      -> dist/FortiGate Upgrader.app
#   Windows: packaging\build_windows.ps1 -> dist\FortiGate Upgrader\FortiGate Upgrader.exe
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
datas = [(str(root / "fortiupgrade" / "web"), "fortiupgrade/web"),
         (str(root / "fortiupgrade" / "changelog.json"), "fortiupgrade"),
         (str(root / "fortiupgrade" / "data"), "fortiupgrade/data")] + collect_data_files("reportlab")

a = Analysis([str(root / "run.py")], pathex=[str(root)], datas=datas,
             hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                            "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
                            "keyring.backends.macOS", "keyring.backends.Windows", "multipart",
                            "fortiupgrade.report"] + collect_submodules("reportlab"),
             excludes=["tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FortiGate Upgrader", console=False,
          icon=str(Path(SPECPATH) / "icon.ico") if sys.platform == "win32" else None)
_ver = {}
exec((root / "fortiupgrade" / "__init__.py").read_text(), _ver)
coll = COLLECT(exe, a.binaries, a.datas, name="FortiGate Upgrader")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="FortiGate Upgrader.app", bundle_identifier="local.fortigate-upgrader",
                 icon=str(Path(SPECPATH) / "icon.icns"),
                 version=_ver["__version__"],
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": _ver["__version__"],
                             "CFBundleVersion": str(_ver["__build__"])})
