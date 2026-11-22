# -*- coding: utf-8 -*-
"""
Export3dmToMesh.py - 遍历 3dm 图层结构, 按 tile 逐个导出 OBJ / GLB 网格文件的独立脚本

图层结构假设为: world::<region>::tile_xxx::{dem, bldg}
其中 dem / bldg 图层承载实际几何。脚本按 tile 分组导出, 空 tile (dem/bldg 均无几何) 自动跳过。
"""

import os
import sys
import argparse
import subprocess

try:
    import Rhino
    import rhinoscriptsyntax as rs
    import scriptcontext as sc
    import System
    IN_RHINO = True
except ImportError:
    IN_RHINO = False

# 外部进程通过环境变量把参数传给 Rhino 子进程 (_-RunPythonScript 无法直接接收 argv)
ENV_INPUT_DIR = "B3DM_INPUT_DIR"
ENV_OUTPUT_DIR = "B3DM_OUTPUT_DIR"
ENV_FORMAT = "B3DM_FORMAT"
ENV_TILE_PREFIX = "B3DM_TILE_PREFIX"
ENV_ZIP = "B3DM_ZIP"
ENV_SEVENZIP_PATH = "B3DM_7Z_PATH"

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

    print("=" * 60)
    print("错误: 未在默认路径中找到 Rhino.exe！")
    print("请确认您的 Rhino 安装路径，并输入绝对路径 (如 C:\\Program Files\\Rhino 8\\System\\Rhino.exe):")
    print("=" * 60)
    user_path = input("Rhino 路径: ").strip()
    if user_path.startswith('"') and user_path.endswith('"'):
        user_path = user_path[1:-1]

    if os.path.exists(user_path):
        return user_path
    else:
        print("输入的路径无效！程序将退出。")
        sys.exit(1)

def find_7zip_path():
    """
    自动检测 Windows 系统上的 7-Zip 安装路径
    """
    common_paths = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for path in common_paths:
        if os.path.exists(path):
            return path

    print("=" * 60)
    print("错误: 未在默认路径中找到 7z.exe！")
    print("请确认您的 7-Zip 安装路径，并输入绝对路径 (如 C:\\Program Files\\7-Zip\\7z.exe):")
    print("=" * 60)
    user_path = input("7-Zip 路径: ").strip()
    if user_path.startswith('"') and user_path.endswith('"'):
        user_path = user_path[1:-1]

    if os.path.exists(user_path):
        return user_path
    else:
        print("输入的路径无效！程序将退出。")
        sys.exit(1)

def group_meshes_by_tile(mesh_ids, tile_prefix):
    """
    按 tile 级祖先图层对 Mesh 对象分组。

    对每个 Mesh, 取其完整图层路径 (world::region::tile_xxx::dem), 找到第一个以
    tile_prefix 开头的路径段作为 tile, 把对象归入该 tile。没有匹配 tile 段的对象被忽略。
    只有真正含几何的 tile 才会出现在结果中, 因此空 tile 自动被排除。

    mesh_ids: Mesh 对象的 Guid 列表
    tile_prefix: 识别 tile 图层的名称前缀
    返回: 保序的 tile 列表, 每项为 {"parts": [路径段...], "ids": [Guid...]}
    """
    tiles = []
    index_by_path = {}
    for obj_id in mesh_ids:
        layer_path = rs.ObjectLayer(obj_id)
        parts = layer_path.split("::")

        tile_pos = None
        for i in range(len(parts)):
            if parts[i].startswith(tile_prefix):
                tile_pos = i
                break
        if tile_pos is None:
            continue

        tile_parts = parts[:tile_pos + 1]
        tile_full_path = "::".join(tile_parts)
        if tile_full_path in index_by_path:
            tiles[index_by_path[tile_full_path]]["ids"].append(obj_id)
        else:
            index_by_path[tile_full_path] = len(tiles)
            tiles.append({"parts": tile_parts, "ids": [obj_id]})

    return tiles

def export_tile(tile_ids, output_dir, tile_name, export_obj, export_glb):
    """
    选中给定对象, 三角化后按需导出为 OBJ / GLB。文件直接写入 output_dir (扁平结构)。

    tile_ids: 该 tile 下的 Mesh 对象 Guid 列表
    output_dir: 导出目录, 所有文件平铺其下
    tile_name: 导出文件名 (不含扩展名)
    export_obj / export_glb: 是否导出对应格式
    """
    rs.UnselectAllObjects()
    rs.SelectObjects(tile_ids)

    # 导出前三角化, 确保不含四边面等非三角面
    Rhino.RhinoApp.RunScript(u'_-TriangulateMesh _Enter', False)

    if export_obj:
        obj_file_path = os.path.join(output_dir, "{}.obj".format(tile_name))
        print("    导出 OBJ: {}".format(obj_file_path))
        # 仅几何: 关闭材质定义 (不生成 .mtl) 与贴图坐标
        export_obj_cmd = (
            u'_-Export "{}" _Enter '
            u'_ExportMaterialDefinitions=_No '
            u'_ExportMeshTextureCoordinates=_No '
            u'_Enter'
        ).format(obj_file_path)
        if not Rhino.RhinoApp.RunScript(export_obj_cmd, False):
            print("    警告: OBJ 导出可能失败。")

    if export_glb:
        glb_file_path = os.path.join(output_dir, "{}.glb".format(tile_name))
        print("    导出 GLB: {}".format(glb_file_path))
        export_glb_cmd = u'_-Export "{}" _Enter _Enter'.format(glb_file_path)
        if not Rhino.RhinoApp.RunScript(export_glb_cmd, False):
            print("    警告: GLB 导出可能失败。")

    rs.UnselectAllObjects()

def zip_region_files(region_dir, region_name, extension, output_dir, seven_zip_path):
    """
    调用 7-Zip 把 region_dir 下所有指定扩展名的文件打包为 <region_name>_<extension>.zip,
    放在 output_dir 下, 然后删除已打包的源文件; 若 region_dir 打包后为空则一并删除。

    region_dir: 区域文件夹路径 (存放该区域所有 tile 的导出文件)
    region_name: 区域名称, 用作 zip 文件名前缀
    extension: 要打包的文件扩展名 (不含点, 如 "obj" 或 "glb")
    seven_zip_path: 7z.exe 的绝对路径
    输出: <output_dir>/<region_name>_<extension>.zip
    """
    matching_files = [f for f in os.listdir(region_dir) if f.lower().endswith("." + extension)]
    if not matching_files:
        return

    zip_path = os.path.join(output_dir, "{}_{}.zip".format(region_name, extension))
    print("  打包 {}: {} 个文件 -> {}".format(extension, len(matching_files), zip_path))

    # -tzip: zip 格式; -mm=Deflate -mx=5: Deflate 压缩, 等级 5; -mmt=on: 多线程
    cmd = [seven_zip_path, "a", "-tzip", "-mm=Deflate", "-mx=5", "-mmt=on", zip_path]
    cmd.extend(os.path.join(region_dir, filename) for filename in matching_files)

    result = subprocess.call(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result != 0:
        print("  警告: 7-Zip 打包失败 (退出码 {}), 保留源文件。".format(result))
        return

    for filename in matching_files:
        os.remove(os.path.join(region_dir, filename))

    if not os.listdir(region_dir):
        os.rmdir(region_dir)

def run_in_rhino():
    """
    在 Rhino 环境内执行的导出逻辑
    """
    print("\n" + "=" * 50)
    print("开始在 Rhino 中执行 3dm 逐 tile 导出网格任务...")
    print("=" * 50)

    input_dir = os.environ.get(ENV_INPUT_DIR)
    output_dir = os.environ.get(ENV_OUTPUT_DIR)
    export_format = os.environ.get(ENV_FORMAT, "both")
    tile_prefix = os.environ.get(ENV_TILE_PREFIX, "tile_")
    zip_regions = os.environ.get(ENV_ZIP) == "1"
    seven_zip_path = os.environ.get(ENV_SEVENZIP_PATH)

    if not input_dir:
        print("未通过环境变量收到输入文件夹路径，请从外部 Python 启动本脚本并传入参数。")
        return
    if not output_dir:
        output_dir = input_dir

    export_obj = export_format in ("obj", "both")
    export_glb = export_format in ("glb", "both")

    if not os.path.isdir(input_dir):
        print("未找到输入文件夹: {}".format(input_dir))
        return

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    print("输入文件夹: {}".format(input_dir))
    print("输出文件夹: {}".format(output_dir))
    print("导出格式: {}".format(export_format))
    print("tile 图层前缀: {}".format(tile_prefix))
    print("按 region 打包: {}".format("是" if zip_regions else "否"))

    # 扫描输入目录下的所有 3dm 文件
    files = [f for f in os.listdir(input_dir) if f.lower().endswith(".3dm")]
    if not files:
        print("在输入文件夹中未找到任何 .3dm 文件。")
        return

    print("扫描到以下 3dm 文件 (共 {} 个):".format(len(files)))
    for f in files:
        print("  - {}".format(f))

    # region 可能横跨多个 3dm 文件, 因此先收集所有涉及的 region, 待全部导出完成后再统一打包
    regions_touched = set()

    for idx, filename in enumerate(files):
        file_path = os.path.join(input_dir, filename)

        print("\n" + "-" * 50)
        print("[{}/{}] 正在处理: {}".format(idx + 1, len(files), filename))
        print("-" * 50)

        # 1. 确保当前文档未修改提示被忽略，然后打开 3dm 文件
        sc.doc.Modified = False
        open_cmd = u'_-Open "{}" _Enter'.format(file_path)
        success = Rhino.RhinoApp.RunScript(open_cmd, False)
        if not success:
            print("打开文件 {} 失败！".format(file_path))
            continue

        # 2. 显示并解锁所有图层和物体，确保能被选中
        for layer in sc.doc.Layers:
            if not layer.IsDeleted:
                layer.IsVisible = True
                layer.IsLocked = False

        for obj in sc.doc.Objects:
            try:
                if obj.Geometry.ObjectType == Rhino.DocObjects.ObjectType.Mesh:
                    sc.doc.Objects.Show(obj.Id, True)
                    sc.doc.Objects.Unlock(obj.Id, True)
            except:
                pass

        # 3. 收集所有 Mesh 并按 tile 分组; 空 tile 不会成组, 自动跳过
        rs.UnselectAllObjects()
        mesh_ids = rs.ObjectsByType(32) # 32 是 Mesh
        if not mesh_ids:
            print("跳过: {} 中没有任何 Mesh 几何。".format(filename))
            continue

        tiles = group_meshes_by_tile(mesh_ids, tile_prefix)
        if not tiles:
            print("跳过: {} 中未找到匹配前缀 '{}' 的 tile 图层。".format(filename, tile_prefix))
            continue

        print("在 {} 中找到 {} 个含几何的 tile。".format(filename, len(tiles)))

        # 4. 逐 tile 导出, 按 region (tile 的上一级图层) 分组: <output>/<region>/<tile>.obj
        for tile in tiles:
            tile_parts = tile["parts"]
            tile_ids = tile["ids"]
            tile_name = tile_parts[-1]

            if len(tile_parts) >= 2:
                region = tile_parts[-2]
                tile_output_dir = os.path.join(output_dir, region)
                regions_touched.add(region)
            else:
                tile_output_dir = output_dir

            if not os.path.exists(tile_output_dir):
                os.makedirs(tile_output_dir)

            print("  Tile: {} ({} 个对象)".format("::".join(tile_parts), len(tile_ids)))
            export_tile(tile_ids, tile_output_dir, tile_name, export_obj, export_glb)

        # 垃圾回收
        System.GC.Collect()
        System.GC.WaitForPendingFinalizers()

    print("\n" + "=" * 50)
    print("所有 3dm 文件导出完成！")
    print("=" * 50)

    # 5. 按 region 打包: <output>/<region>_obj.zip, <output>/<region>_glb.zip (需 --zip 显式开启)
    if zip_regions and regions_touched:
        print("\n" + "=" * 50)
        print("开始按 region 打包压缩文件...")
        print("=" * 50)

        for region in sorted(regions_touched):
            region_dir = os.path.join(output_dir, region)
            if not os.path.isdir(region_dir):
                continue

            print("Region: {}".format(region))
            if export_obj:
                zip_region_files(region_dir, region, "obj", output_dir, seven_zip_path)
            if export_glb:
                zip_region_files(region_dir, region, "glb", output_dir, seven_zip_path)

        print("\n" + "=" * 50)
        print("打包完成！")
        print("=" * 50)

def parse_args(argv):
    """
    解析外部命令行参数。

    argv: 不含程序名的参数列表
    返回: argparse.Namespace, 含 input_dir / output_dir / export_format / tile_prefix
    """
    parser = argparse.ArgumentParser(
        description="遍历 3dm 图层结构, 按 tile 逐个导出 OBJ / GLB 网格文件"
    )
    parser.add_argument(
        "input_dir",
        help="包含 .3dm 文件的输入文件夹路径"
    )
    parser.add_argument(
        "-o", "--output",
        dest="output_dir",
        default=None,
        help="导出文件的输出文件夹 (默认: 与输入文件夹相同)"
    )
    parser.add_argument(
        "-f", "--format",
        dest="export_format",
        choices=["obj", "glb", "both"],
        default="both",
        help="导出格式: obj, glb 或 both (默认: both)"
    )
    parser.add_argument(
        "-t", "--tile-prefix",
        dest="tile_prefix",
        default="tile_",
        help="识别 tile 图层的名称前缀 (默认: tile_)"
    )
    parser.add_argument(
        "--zip",
        dest="zip_regions",
        action="store_true",
        help="将每个 region 的导出文件按格式打包为 zip (<region>_obj.zip / <region>_glb.zip), 默认关闭"
    )
    return parser.parse_args(argv)

def main():
    if IN_RHINO:
        run_in_rhino()
        return

    # 外部 Python 环境, 解析参数后启动 Rhino 进程执行自身
    args = parse_args(sys.argv[1:])

    input_dir = os.path.abspath(args.input_dir)
    if not os.path.isdir(input_dir):
        print("错误: 输入文件夹不存在: {}".format(input_dir))
        sys.exit(1)

    output_dir = os.path.abspath(args.output_dir) if args.output_dir else input_dir

    rhino_path = find_rhino_path()
    # 提前解析 7z.exe 路径: --zip 未开启时无需 7-Zip, 避免不必要的检测/提示
    seven_zip_path = find_7zip_path() if args.zip_regions else None

    script_path = os.path.abspath(__file__)
    print("使用 Rhino 安装路径: {}".format(rhino_path))
    print("输入文件夹: {}".format(input_dir))
    print("输出文件夹: {}".format(output_dir))
    print("导出格式: {}".format(args.export_format))
    print("tile 图层前缀: {}".format(args.tile_prefix))
    print("按 region 打包: {}".format("是" if args.zip_regions else "否"))
    if seven_zip_path:
        print("使用 7-Zip 路径: {}".format(seven_zip_path))
    print("准备启动 Rhino 进程...")

    # 通过环境变量把参数传给 Rhino 子进程
    env = os.environ.copy()
    env[ENV_INPUT_DIR] = input_dir
    env[ENV_OUTPUT_DIR] = output_dir
    env[ENV_FORMAT] = args.export_format
    env[ENV_TILE_PREFIX] = args.tile_prefix
    env[ENV_ZIP] = "1" if args.zip_regions else "0"
    env[ENV_SEVENZIP_PATH] = seven_zip_path if seven_zip_path else ""

    # 构建 Rhino 命令行, 让其以 headless 模式启动并运行自身
    cmd = '"{}" /nosplash /runscript="_-RunPythonScript ({})"'.format(
        rhino_path, script_path
    )
    print("启动命令: {}".format(cmd))

    try:
        p = subprocess.Popen(cmd, env=env)
        p.wait()
        print("Rhino 进程已退出。")
    except Exception as e:
        print("启动 Rhino 时发生异常: {}".format(e))

if __name__ == "__main__":
    main()
