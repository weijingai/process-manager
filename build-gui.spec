# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：桌面版（tkinter 原生窗口，无控制台、无浏览器）。

用法：
    pyinstaller build-gui.spec --noconfirm --clean
产物：
    dist/WindowsProcessManager-Desktop.exe
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = collect_submodules("core")

a = Analysis(
    ["gui_app.py"],
    pathex=["."],
    binaries=[],
    datas=[],                      # 桌面版界面由 tkinter 绘制，不需要 web/ 静态资源
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 注意：桌面版必须保留 tkinter；http.server 依赖 http.client / email / xml，也不能排除
    excludes=[
        "unittest", "pydoc", "doctest", "test",
        "lib2to3", "distutils", "numpy", "PIL", "matplotlib",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="WindowsProcessManager-Desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,             # 桌面版不弹控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
