# PyInstaller build spec (plan section 84, phase 9).
#
#   pyinstaller internet_overwatch.spec
#
# Produces a single windowed executable with the logo baked in as the icon.
# Build the .ico first if it is missing:  python -m app.utils.assets

from pathlib import Path

PROJECT_ROOT = Path(SPECPATH)
ASSETS = PROJECT_ROOT / "assets"
ICON = ASSETS / "icons" / "app_icon.ico"

datas = []
if ASSETS.exists():
    # Keep the layout so app.utils.assets finds files under sys._MEIPASS/assets.
    for path in ASSETS.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            datas.append((str(path), str(Path("assets") / path.parent.relative_to(ASSETS))))

# PyInstaller ships a hook for pyqtgraph, so nothing needs forcing here.
# Collecting its submodules explicitly would drag in the optional scientific
# accelerators (scipy, numba, llvmlite) and triple the bundle size.
hiddenimports = []

# Modules the app never touches. pyqtgraph imports scipy/numba lazily as
# optional accelerators; without them it falls back to numpy, which is all the
# latency graphs need.
excludes = [
    "scipy", "numba", "llvmlite", "matplotlib", "pandas", "IPython", "PIL",
    "tkinter", "pytest", "setuptools", "pip", "numpy.f2py", "numpy.testing",
    "pyqtgraph.examples", "pyqtgraph.opengl", "OpenGL", "h5py",
    "PyQt5", "PyQt6", "PySide2", "shiboken2",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtDataVisualization", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
]

a = Analysis(
    ["app/main.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="InternetOverwatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed app; --headless users can still redirect
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)
