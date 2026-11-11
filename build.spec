# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 —— 扫图去重 v3（onedir 目录版）
依赖：rapidocr 的 onnx 模型 + onnxruntime + cv2（OCR 必需）

为什么用 onedir 而不是单文件：
  本机上新打包的单文件 exe 启动时会弹 "Could not create temporary
  directory!"（PyInstaller 单文件要先解压 237MB 到临时目录，这台机器上
  该步骤失败，程序就卡在一个隐藏的确定对话框上）。onedir 不需要解压，
  启动也更快。
"""
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas, binaries, hiddenimports = [], [], []

for pkg in (
    "rapidocr_onnxruntime",   # 含 models/*.onnx
    "onnxruntime",            # 含 DLL
    "cv2",                    # opencv
    "PIL",
    "send2trash",
    "yaml",
    "shapely",
    "pyclipper",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print(f"[warn] collect_all({pkg}) 失败: {e}")

# 兜底：显式带上 rapidocr 的模型与配置
try:
    datas += collect_data_files("rapidocr_onnxruntime", includes=["**/*.onnx", "**/*.yaml"])
except Exception:
    pass

hiddenimports += [
    "dedup_core", "dedup_ocr",
    "numpy", "tkinter", "tkinter.ttk", "tkinter.filedialog",
    "tkinter.messagebox", "tkinter.font",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "scipy", "pandas", "notebook", "IPython",
        "dedup_stitch", "dedup_stitch_task",   # 拼接功能已从程序中移除
        "onnxruntime.transformers", "onnxruntime.quantization", "onnxruntime.tools",
        "pytest", "setuptools", "pip", "wheel", "paddle", "torch",
        "PyQt5", "PySide2", "PySide6", "wx",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="扫图去重v3",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 不显示黑色控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="扫图去重v3",
)
