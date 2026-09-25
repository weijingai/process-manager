# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把本工具打成单个 exe（无需安装 Python 即可运行）。

用法：
    pyinstaller build.spec --noconfirm --clean
产物：
    dist/WindowsProcessManager.exe
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = collect_submodules("core")

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    # 前端静态资源必须一起打进 exe
    datas=[("web", "web")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 精简体积：只剔除明确用不到的大模块
    # 注意：http.server 依赖 http.client / email / xml，不能排除，否则 exe 启动即报错
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "test",
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
    name="WindowsProcessManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # 保留控制台窗口，用于显示访问地址与日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
