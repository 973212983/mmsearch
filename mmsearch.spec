# PyInstaller 配置：pyinstaller mmsearch.spec（由 build.ps1 调用）
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [("mmsearch/web", "mmsearch/web"), ("assets/mmsearch.ico", "assets"), ("assets/mmsearch.png", "assets")]
datas += collect_data_files("webview")          # WebView2 的 .NET 程序集与注入脚本
binaries = collect_dynamic_libs("webview")

a = Analysis(
    ["main.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=["clr", "pystray._win32", "webview.platforms.winforms", "webview.platforms.edgechromium"],
    excludes=["tkinter", "torch", "transformers", "sentence_transformers", "PySide6", "matplotlib", "scipy",
              "pandas", "IPython", "pytest", "librosa", "numba", "llvmlite", "sklearn"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MMSearch",
    icon="assets/mmsearch.ico",
    console=False,
    version="version_info.txt",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MMSearch", upx=False)
