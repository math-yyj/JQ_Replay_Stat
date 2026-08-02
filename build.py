#!/usr/bin/env python3
"""
军棋复盘布阵统计工具打包脚本
使用 PyInstaller 打包为 Windows exe，排除不需要的重量级库
"""

import subprocess
import sys
import os
from pathlib import Path

def main():
    # 项目根目录
    project_dir = Path(__file__).parent
    main_script = project_dir / "stats_gui.py"
    
    if not main_script.exists():
        print(f"错误: 找不到主程序 {main_script}")
        return 1
    
    # PyInstaller 参数
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",                    # 打包为单个 exe
        "--windowed",                   # 无控制台窗口（GUI 程序）
        "--name=军棋布阵统计",           # exe 文件名
        "--clean",                      # 清理临时文件
        # 排除不需要的重量级库
        "--exclude-module=torch",
        "--exclude-module=torchvision",
        "--exclude-module=numpy",
        "--exclude-module=open3d",
        "--exclude-module=opencv-python",
        "--exclude-module=opencv-python-headless",
        "--exclude-module=matplotlib",
        "--exclude-module=pandas",
        "--exclude-module=scipy",
        "--exclude-module=sklearn",
        "--exclude-module=tensorflow",
        "--exclude-module=keras",
        "--exclude-module=transformers",
        "--exclude-module=tokenizers",
        "--exclude-module=huggingface-hub",
        "--exclude-module=safetensors",
        "--exclude-module=networkx",
        "--exclude-module=sympy",
        "--exclude-module=PIL",
        "--exclude-module=pillow",
        # 排除测试和开发模块
        "--exclude-module=pytest",
        "--exclude-module=unittest",
        "--exclude-module=doctest",
        "--exclude-module=test",
        # 排除不需要的 GUI 框架
        "--exclude-module=PyQt5",
        "--exclude-module=PyQt6",
        "--exclude-module=PySide2",
        "--exclude-module=PySide6",
        "--exclude-module=wx",
        "--exclude-module=kivy",
        # 设置图标（如果有的话）
        # "--icon=icon.ico",
        # 主程序入口
        str(main_script),
    ]
    
    print("正在打包军棋布阵统计工具...")
    print(f"命令: {' '.join(cmd)}")
    print()
    
    try:
        result = subprocess.run(cmd, cwd=project_dir, check=True)
        print()
        print("打包完成！")
        print(f"输出目录: {project_dir / 'dist'}")
        print(f"exe 文件: {project_dir / 'dist' / '军棋布阵统计.exe'}")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"打包失败: {e}")
        return 1
    except KeyboardInterrupt:
        print("\n打包已取消")
        return 1

if __name__ == "__main__":
    sys.exit(main())
