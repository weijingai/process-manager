# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Linux）：把本工具打成单个可执行文件。

用法（在 Linux 上执行）：
    pyinstaller build-linux.spec --noconfirm
产物：
    dist/LinuxProcessManager
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("core")

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    # 前端静态资源必须一起打进可执行文件
    datas=[("web", "web")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "test",
        "lib2to3", "distutils", "numpy", "PIL", "matplotlib",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LinuxProcessManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # 保留控制台输出，用于显示访问地址与提示
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
