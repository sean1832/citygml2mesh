# -*- coding: utf-8 -*-
"""
DEM 地形网格面数快速导入分析工具 (兼容命令行与 Rhino 环境)
此脚本用于：
1. 自动检测运行环境 (命令行模式 / Rhino 内置 IronPython)；
2. 扫描当前目录下的区块文件夹中的 dem 目录；
3. 统计并打印每个文件及合并（Join）后的面数和顶点数。
"""

from __future__ import print_function
import os
import struct
import json

# 检测是否在 Rhino 环境中运行
try:
    import Rhino
    import Rhino.Geometry as RG
    import rhinoscriptsyntax as rs
    RUNNING_IN_RHINO = True
except ImportError:
    RUNNING_IN_RHINO = False


def parse_glb_stats(filepath):
    """
    免依赖解析 GLB 文件的网格面数与顶点数
    """
    try:
        with open(filepath, 'rb') as f:
            # 1. 读取 GLB 文件头 (12 字节)
            header = f.read(12)
            if len(header) < 12:
                return 0, 0
            magic, version, length = struct.unpack('<III', header)
            if magic != 0x46546C67: # "glTF"
                return 0, 0
                
            # 2. 读取第一个 Chunk (JSON) 头部 (8 字节)
            chunk_header = f.read(8)
            if len(chunk_header) < 8:
                return 0, 0
            chunk_length, chunk_type = struct.unpack('<II', chunk_header)
            if chunk_type != 0x4E4F534A: # "JSON"
                return 0, 0
                
            # 3. 读取 JSON 内容并解析
            json_data = f.read(chunk_length)
            if len(json_data) < chunk_length:
                return 0, 0
                
            try:
                json_str = json_data.decode('utf-8')
            except AttributeError:
                json_str = json_data
                
            gltf = json.loads(json_str)
            
            # 4. 统计顶点与面数
            total_vertices = 0
            total_faces = 0
            
            accessors = gltf.get("accessors", [])
            meshes = gltf.get("meshes", [])
            
            for mesh in meshes:
                for primitive in mesh.get("primitives", []):
                    # 获取顶点数
                    attrs = primitive.get("attributes", {})
                    pos_idx = attrs.get("POSITION")
                    if pos_idx is not None and pos_idx < len(accessors):
                        total_vertices += accessors[pos_idx].get("count", 0)
                        
                    # 获取面数
                    indices_idx = primitive.get("indices")
                    if indices_idx is not None and indices_idx < len(accessors):
                        indices_count = accessors[indices_idx].get("count", 0)
                        # glTF 默认是三角形网格
                        total_faces += indices_count // 3
                    elif pos_idx is not None and pos_idx < len(accessors):
                        total_faces += accessors[pos_idx].get("count", 0) // 3
                        
            return total_faces, total_vertices
    except Exception as e:
        print("解析文件 {} 出错: {}".format(os.path.basename(filepath), e))
        return 0, 0


def main_rhino():
    """
    Rhino 环境下的执行逻辑
    """
    script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
    
    subdirs = [d for d in os.listdir(script_dir) if os.path.isdir(os.path.join(script_dir, d))]
    zones = []
    for d in subdirs:
        if d.lower() in ["output", "temp"] or d.startswith("."):
            continue
        dem_path = os.path.join(script_dir, d, "dem")
        if os.path.exists(dem_path):
            zones.append((d, dem_path))
            
    if not zones:
        print("未在当前目录下找到任何包含 dem 目录的区块。")
        return
        
    zone_names = [z[0] for z in zones]
    options = ["== 分析所有区块 =="] + zone_names
    selected = rs.ListBox(options, "请选择要导入并分析 DEM 面数的区块:", "DEM 面数分析器", default=options[0])
    
    if not selected:
        print("操作已取消。")
        return
        
    if selected == "== 分析所有区块 ==":
        target_zones = zones
    else:
        target_zones = [z for z in zones if z[0] == selected]
        
    doc = Rhino.RhinoDoc.ActiveDoc
    
    for zone_name, dem_path in target_zones:
        print("\n" + "="*50)
        print("正在分析区块: {}".format(zone_name))
        print("DEM 目录: {}".format(dem_path))
        print("="*50)
        
        glb_files = [f for f in os.listdir(dem_path) if f.lower().endswith(".glb")]
        if not glb_files:
            print("该区块的 dem 目录下未找到任何 .glb 文件。")
            continue
            
        print("发现 {} 个 .glb 文件，正在导入与分析...".format(len(glb_files)))
        
        imported_meshes = []
        total_faces = 0
        total_vertices = 0
        
        # 创建临时隔离图层
        temp_layer_name = "temp_dem_check_{}".format(zone_name)
        temp_layer_idx = doc.Layers.FindByFullPath(temp_layer_name, True)
        if temp_layer_idx < 0:
            new_layer = Rhino.DocObjects.Layer()
            new_layer.Name = temp_layer_name
            temp_layer_idx = doc.Layers.Add(new_layer)
            
        doc.Layers.SetCurrentLayerIndex(temp_layer_idx, True)
        
        for idx, filename in enumerate(glb_files):
            filepath = os.path.join(dem_path, filename)
            cmd = u'_-Import "{}" _Enter _Enter'.format(filepath)
            
            before_sub = set([obj.Id for obj in doc.Objects])
            success = Rhino.RhinoApp.RunScript(cmd, False)
            if not success:
                print("  [{}/{}] 导入失败: {}".format(idx+1, len(glb_files), filename))
                continue
                
            after_sub = set([obj.Id for obj in doc.Objects])
            new_sub_ids = after_sub - before_sub
            
            file_faces = 0
            file_vertices = 0
            
            for oid in new_sub_ids:
                obj = doc.Objects.Find(oid)
                if obj and obj.Geometry and obj.Geometry.ObjectType == Rhino.DocObjects.ObjectType.Mesh:
                    mesh = obj.Geometry
                    file_faces += mesh.Faces.Count
                    file_vertices += mesh.Vertices.Count
                    imported_meshes.append(mesh.Duplicate())
                    
            print("  [{}/{}] {}: 面数 = {}, 顶点数 = {}".format(
                idx+1, len(glb_files), filename, file_faces, file_vertices
            ))
            total_faces += file_faces
            total_vertices += file_vertices
            
            # 立即清理导入的对象，防止文档污染
            for oid in new_sub_ids:
                doc.Objects.Delete(oid, True)
                
        # 计算 Join 后的汇总结果
        if len(imported_meshes) > 0:
            joined = RG.Mesh()
            joined.Append(imported_meshes)
            joined.Compact()
            
            print("-" * 50)
            print("汇总结果 ({}):".format(zone_name))
            print("  - 导入文件总数: {}".format(len(glb_files)))
            print("  - 散碎网格累计面数: {}".format(total_faces))
            print("  - 散碎网格累计顶点: {}".format(total_vertices))
            print("  - 合并(Join)后网格面数: {}".format(joined.Faces.Count))
            print("  - 合并(Join)后网格顶点: {}".format(joined.Vertices.Count))
            print("-" * 50)
        else:
            print("未成功导入任何有效的地形网格。")
            
        # 强制清理临时图层
        if temp_layer_idx >= 0:
            doc.Layers.Purge(doc.Layers[temp_layer_idx].Id, True)
            
    print("\n分析完毕！所有临时导入的网格和临时图层已被清理。")
    doc.Views.Redraw()


def main_cli():
    """
    普通命令行环境下的执行逻辑 (无需启动 Rhino，解析速度快)
    """
    script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
    
    subdirs = [d for d in os.listdir(script_dir) if os.path.isdir(os.path.join(script_dir, d))]
    zones = []
    for d in subdirs:
        if d.lower() in ["output", "temp"] or d.startswith("."):
            continue
        dem_path = os.path.join(script_dir, d, "dem")
        if os.path.exists(dem_path):
            zones.append((d, dem_path))
            
    if not zones:
        print("未在当前目录下找到任何包含 dem 目录的区块。")
        return
        
    print("\n请选择要分析 DEM 面数的区块:")
    print("  0. [默认] 分析所有区块")
    for idx, (zone_name, _) in enumerate(zones):
        print("  {}. {}".format(idx + 1, zone_name))
        
    try:
        # 兼容 Python 2.7 的 raw_input 和 Python 3 的 input
        input_func = raw_input if 'raw_input' in globals() else input
        choice_input = input_func("\n请输入序号 [0]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n操作已取消。")
        return
        
    if not choice_input:
        choice = 0
    else:
        try:
            choice = int(choice_input)
            if choice < 0 or choice > len(zones):
                print("输入序号超出范围！")
                return
        except ValueError:
            print("输入无效！")
            return
            
    if choice == 0:
        target_zones = zones
    else:
        target_zones = [zones[choice - 1]]
        
    for zone_name, dem_path in target_zones:
        print("\n" + "="*50)
        print("正在分析区块: {}".format(zone_name))
        print("DEM 目录: {}".format(dem_path))
        print("="*50)
        
        glb_files = [f for f in os.listdir(dem_path) if f.lower().endswith(".glb")]
        if not glb_files:
            print("该区块的 dem 目录下未找到任何 .glb 文件。")
            continue
            
        print("发现 {} 个 .glb 文件，正在解析二进制文件...".format(len(glb_files)))
        
        total_faces = 0
        total_vertices = 0
        
        for idx, filename in enumerate(glb_files):
            filepath = os.path.join(dem_path, filename)
            faces, vertices = parse_glb_stats(filepath)
            print("  [{}/{}] {}: 面数 = {}, 顶点数 = {}".format(
                idx+1, len(glb_files), filename, faces, vertices
            ))
            total_faces += faces
            total_vertices += vertices
            
        print("-" * 50)
        print("汇总结果 ({}):".format(zone_name))
        print("  - 导入文件总数: {}".format(len(glb_files)))
        print("  - 累计网格总面数: {}".format(total_faces))
        print("  - 累计网格总顶点: {}".format(total_vertices))
        print("-" * 50)
        
    print("\n所有分析完成。")


if __name__ == "__main__":
    if RUNNING_IN_RHINO:
        main_rhino()
    else:
        main_cli()
