# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['E:\\Program Files\\JQ_Replay_Stat\\stats_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'numpy', 'open3d', 'opencv-python', 'opencv-python-headless', 'matplotlib', 'pandas', 'scipy', 'sklearn', 'tensorflow', 'keras', 'transformers', 'tokenizers', 'huggingface-hub', 'safetensors', 'networkx', 'sympy', 'PIL', 'pillow', 'pytest', 'unittest', 'doctest', 'test', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx', 'kivy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='军棋布阵统计',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
