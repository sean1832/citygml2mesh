# -*- coding: utf-8 -*-
"""
RunAllZones.py - 东京 GIS 数据集全自动多区块批处理启动器
"""

import os
import sys
import json
import subprocess
import time

def find_rhino_path():
    """
    自动检测 Windows 系统上的 Rhino 安装路径
    """
    common_paths = [
        r"C:\Program Files\Rhino 8\System\Rhino.exe",
        r"C:\Program Files\Rhino 7\System\Rhino.exe",
        r"C:\Program Files\Rhino 6\System\Rhino.exe",
    ]
    for path in common_paths:
        if os.path.exists(path):
            return path
            
    # 如果找不到，提示用户手动配置
    print("=" * 60)
    print("错误: 未在默认路径中找到 Rhino.exe！")
    print("请确认您的 Rhino 安装路径，并在此处输入绝对路径 (如 C:\\Program Files\\Rhino 8\\System\\Rhino.exe):")
    print("=" * 60)
    user_path = input("Rhino 路径: ").strip()
    if user_path.startswith('"') and user_path.endswith('"'):
        user_path = user_path[1:-1]
        
    if os.path.exists(user_path):
        return user_path
    else:
        print("输入的路径无效！程序将退出。")
        sys.exit(1)

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    # 1. 寻找 Rhino 路径
    rhino_path = find_rhino_path()
    print("使用 Rhino 安装路径: {}".format(rhino_path))
    
    # 2. 扫描区块子目录
    zones = []
    for item in os.listdir(script_dir):
        if item.lower() in ["output", "temp", "original zip"] or item.startswith("."):
            continue
        item_path = os.path.join(script_dir, item)
        if os.path.isdir(item_path):
            # 必须包含 dem 或 bldg 文件夹才被视为有效的 GIS 区块
            if os.path.exists(os.path.join(item_path, "dem")) or os.path.exists(os.path.join(item_path, "bldg")):
                zones.append(item)
                
    zones.sort()
    
    if not zones:
        print("未在当前目录下找到任何有效的城市区块文件夹（需包含 dem 或 bldg 子目录）。")
        return
        
    print("\n扫描到以下待处理的城市区块 (共 {} 个):".format(len(zones)))
    for idx, zone in enumerate(zones):
        print("  {}. {}".format(idx + 1, zone))
        
    print("\n" + "=" * 50)
    print("准备开始自动处理流程...")
    print("由于是分进程启动，每个区块的 Rhino 进程互不干扰，可彻底规避内存溢出崩溃。")
    print("=" * 50)
    
    # 记录配置文件路径
    config_file = os.path.join(script_dir, ".active_zone.json")
    main_script_path = os.path.join(script_dir, "ProcessTokyoGIS.py")
    
    if not os.path.exists(main_script_path):
        print("错误: 未找到主处理脚本 ProcessTokyoGIS.py！它必须与本启动器在同一文件夹下。")
        return

    # 让用户选择是全部处理还是仅处理单个区块
    print("\n请选择要执行的处理模式:")
    print("  0. [默认] 依次自动处理所有区块 (共 {} 个)".format(len(zones)))
    for idx, zone in enumerate(zones):
        print("  {}. 仅处理区块: {}".format(idx + 1, zone))
        
    choice_input = input("\n请输入要处理的序号（支持多个序号以逗号分隔，如 1,2,3,5,8；或范围，如 1-3）[0]: ").strip()
    if not choice_input:
        target_zones = list(zones)
    else:
        parts = [p.strip() for p in choice_input.split(",") if p.strip()]
        selected_indices = []
        for part in parts:
            if "-" in part:
                try:
                    start_str, end_str = part.split("-")
                    start = int(start_str)
                    end = int(end_str)
                    selected_indices.extend(range(start, end + 1))
                except ValueError:
                    print("范围输入格式无效: {}".format(part))
                    return
            else:
                try:
                    val = int(part)
                    selected_indices.append(val)
                except ValueError:
                    print("输入包含无效序号: {}".format(part))
                    return
                    
        if 0 in selected_indices:
            target_zones = list(zones)
        else:
            target_zones = []
            for idx in selected_indices:
                if idx < 1 or idx > len(zones):
                    print("输入的序号超出范围: {}".format(idx))
                    return
                target_zones.append(zones[idx - 1])
            
            # 去除重复的区块并保留原始相对顺序
            seen = set()
            target_zones = [z for z in target_zones if not (z in seen or seen.add(z))]
        
    start_time = time.time()
    
    for idx, zone in enumerate(target_zones):
        print("\n" + "-" * 50)
        print("[{}/{}] 正在处理区块: {}".format(idx + 1, len(target_zones), zone))
        print("-" * 50)
        
        # 写入任务指示配置文件
        config_data = {
            "target_zone": zone
        }
        with open(config_file, "w") as f:
            json.dump(config_data, f)
            
        # 构建 Rhino 启动命令行
        # Rhino 官方格式: Rhino.exe /nosplash /runscript="_-RunPythonScript (path)"
        # 必须用字符串而非列表传给 Popen，否则 Python 的 list2cmdline 会破坏引号结构
        cmd = '"{}" /nosplash /runscript="_-RunPythonScript ({})"'.format(
            rhino_path, main_script_path
        )
        
        print("启动命令: {}".format(cmd))
        try:
            # 启动并等待退出
            p = subprocess.Popen(cmd)
            p.wait()
            print("Rhino 进程已退出 (返回值: {})。".format(p.returncode))
        except Exception as e:
            print("启动 Rhino 时发生异常: {}".format(e))
            
        # 检查配置文件是否被 Rhino 成功清理
        if os.path.exists(config_file):
            print("警告: 配置文件未被清除，这可能表明 Rhino 被提前手动关闭或执行出错。")
            try:
                os.remove(config_file)
            except:
                pass
            user_choice = input("是否继续处理下一个区块？(y/n): ").strip().lower()
            if user_choice != 'y' and user_choice != 'yes':
                print("处理流程已中止。")
                break
        else:
            print("区块 {} 处理完成。".format(zone))
            
    total_duration = time.time() - start_time
    print("\n" + "=" * 50)
    print("所有区块自动批处理完成！")
    print("总计耗时: {:.1f} 秒".format(total_duration))
    print("请查看 output 目录获取输出的 3dm 文件与瓦片。")
    print("=" * 50)

if __name__ == "__main__":
    main()
