# -*- coding: utf-8 -*-
"""
东京 GIS 数据集 500x500m 瓦片自动导入、重构与切分导出脚本
运行环境: Rhino 7 / Rhino 8 (Windows) 中的 Python 编辑器 (EditPythonScript)
"""

from __future__ import print_function
import os
import math
import Rhino
import Rhino.Geometry as RG
import rhinoscriptsyntax as rs
import System

# ================= 配置区域 =================
# 重构地形的网格采样间距（米）。越小地形细节越精细，但面数越多。推荐 5.0 - 10.0 米。
GRID_SPACING = 10.0

# 瓦片尺寸（米）。东京 GIS 标准要求 500x500 米。
TILE_SIZE = 500.0

# 是否只导出完整的 500x500 瓦片。如果为 True，处于边界外、不完整的瓦片将被过滤。
ONLY_COMPLETE_FILES = False

# 导出格式，可选 ".glb" 或 ".obj"
EXPORT_FORMAT = ".glb"

# 是否在切分完成后，将切分结果保留并保存为 3dm 文件（默认为 True）
SAVE_3DM = True

# 是否同时导出各瓦片的独立 .glb/.obj 格式文件（默认为 False，即只保存 3dm 不输出零散文件）
EXPORT_TILES = False

# 是否在保存到 3dm 时，将同一个瓦片内的所有建筑合并为一个 Mesh
JOIN_BUILDINGS_PER_TILE = True

# 是否启用 QuadRemesh 算法重构 DEM 地形
USE_QUAD_REMESH = True

# 默认的全局 DEM 网格面数缩减比例（0.5 表示目标面数设为原始的 50%）
QUAD_REMESH_DEFAULT_RATIO = 0.5

# 各个区块单独分配比例的字典。支持输入数字前缀的完整文件夹名，或者英文简称（不区分大小写）
# 如果在字典中指定了比例（且不为 None），则优先采用字典中的固定比例，不再进行动态调整。
# 若设置为 None 或未包含在该字典中，则自动按网格面数启用下面的动态降级规则。
QUAD_REMESH_RATIO_OVERRIDES = {
    "01-bunkyo-ku": None,
    "02-chiyoda-ku": None,
    "03-chuo-ku": None,
    "04-meguro-ku": None,
    "05-nakano-ku": None,
    "06-ota-ku": None,
    "07-setagaya-ku": None,
    "08-shibuya-ku": None,
    "09-shinagawa-ku": None,
    "10-shinjiku-ku": None,
    "11-taito-ku": None,
    "12-toshima-ku": None,
}

# --- 动态比例控制参数（仅当未在字典中匹配到固定比例时生效） ---
# 大网格判定阈值（原始面数超过此值时，触发动态比例降低）
QUAD_REMESH_LARGE_MESH_THRESHOLD = 2000000

# 大网格的目标重构面数（即在大网格阈值以上时，以该面数为目标，默认 100 万面左右）
QUAD_REMESH_LARGE_MESH_TARGET = 1000000

# 重构目标面数的硬下限（即使原网格较大，重构目标面数亦不低于此值，默认 80 万面）
QUAD_REMESH_MIN_TARGET_FACES = 800000

# QuadRemesh 自适应大小 (AdaptiveSize, 0-100)，越小分布越均匀，越大越倾向于边缘和弯曲处
QUAD_REMESH_ADAPTIVE_SIZE = 50

# QuadRemesh 是否检测并保留硬边 (DetectHardEdges)
QUAD_REMESH_DETECT_HARD_EDGES = False
# ============================================

def purge_empty_layers():
    """
    清理文档中所有不包含任何对象且没有子图层的空图层（保留 Default 图层）。
    """
    doc = Rhino.RhinoDoc.ActiveDoc
    # 从后往前遍历，因为删除图层会改变索引
    for i in range(doc.Layers.Count - 1, -1, -1):
        if i >= doc.Layers.Count:
            continue
        layer = doc.Layers[i]
        if layer.IsDeleted:
            continue
        if layer.Name.lower() != "default":
            # 检查是否有子图层
            children = layer.GetChildren()
            if not children or len(children) == 0:
                # 检查该图层内是否有物体
                objs = doc.Objects.FindByLayer(layer)
                if not objs or len(objs) == 0:
                    doc.Layers.Purge(layer.Id, True)

def get_or_create_layer(layer_path):
    """
    递归创建并返回指定图层的 Index。
    layer_path 格式: "world::bunkyo-ku::dem"
    """
    doc = Rhino.RhinoDoc.ActiveDoc
    parts = layer_path.split("::")
    parent_id = System.Guid.Empty
    current_path = ""
    last_layer_index = -1
    
    for part in parts:
        if current_path:
            current_path += "::" + part
        else:
            current_path = part
        
        layer_index = doc.Layers.FindByFullPath(current_path, True)
        if layer_index < 0:
            new_layer = Rhino.DocObjects.Layer()
            new_layer.Name = part
            if parent_id != System.Guid.Empty:
                new_layer.ParentLayerId = parent_id
            
            layer_index = doc.Layers.Add(new_layer)
            if layer_index < 0:
                raise ValueError("无法创建图层: {}".format(current_path))
            layer = doc.Layers[layer_index]
            parent_id = layer.Id
            last_layer_index = layer_index
        else:
            layer = doc.Layers[layer_index]
            parent_id = layer.Id
            last_layer_index = layer_index
            
    return last_layer_index

def create_grid_mesh(points, cols, rows):
    """
    从规则网格点阵直接构建 Mesh（四边面）。
    points: 平铺的点列表，排列顺序为 u(col) 外循环, v(row) 内循环
    cols:   列数 (grid_n + 1)
    rows:   行数 (grid_n + 1)
    """
    mesh = RG.Mesh()
    for pt in points:
        mesh.Vertices.Add(pt)
    
    for u in range(cols - 1):
        for v in range(rows - 1):
            i00 = u * rows + v
            i10 = (u + 1) * rows + v
            i11 = (u + 1) * rows + (v + 1)
            i01 = u * rows + (v + 1)
            mesh.Faces.AddFace(i00, i10, i11, i01)
    
    mesh.Normals.ComputeNormals()
    mesh.Compact()
    return mesh

def import_glb_to_layer(glb_path, layer_index):
    """
    将指定的 GLB 文件导入到指定的图层中。
    使用 ID 变化追踪技术，确保导入的对象被百分之百归类到目标图层。
    """
    doc = Rhino.RhinoDoc.ActiveDoc
    # 记录导入前的所有对象 ID
    before_ids = set([obj.Id for obj in doc.Objects])
    
    # 设为当前图层（双保险）
    doc.Layers.SetCurrentLayerIndex(layer_index, True)
    
    # 运行无头导入命令
    cmd = u'_-Import "{}" _Enter _Enter'.format(glb_path)
    success = Rhino.RhinoApp.RunScript(cmd, False)
    if not success:
        print("导入失败: {}".format(glb_path))
        return []
        
    # 找出导入后新增的对象
    after_ids = set([obj.Id for obj in doc.Objects])
    new_ids = after_ids - before_ids
    
    mesh_objects = []
    for oid in new_ids:
        obj = doc.Objects.Find(oid)
        if obj:
            if obj.Geometry.ObjectType == Rhino.DocObjects.ObjectType.Mesh:
                # 强制修正图层属性
                obj.Attributes.LayerIndex = layer_index
                obj.CommitChanges()
                mesh_objects.append(obj)
            else:
                # 如果导入了非 Mesh 对象，也移动到对应图层
                obj.Attributes.LayerIndex = layer_index
                obj.CommitChanges()
                
    return mesh_objects

def split_mesh_by_face_count(mesh, max_faces=1200000):
    """
    递归分割一个地形网格，直到子网格的三角形/四边形面数小于等于 max_faces。
    使用 bounding box 的长边垂直中面进行切分。
    """
    if mesh.Faces.Count <= max_faces:
        return [mesh]
        
    bbox = mesh.GetBoundingBox(True)
    if not bbox.IsValid:
        return [mesh]
        
    size_x = bbox.Max.X - bbox.Min.X
    size_y = bbox.Max.Y - bbox.Min.Y
    
    # 哪边长就在哪边切，确保切出来的子块长宽比合理
    if size_x > size_y:
        mid_x = (bbox.Min.X + bbox.Max.X) / 2.0
        plane = RG.Plane(RG.Point3d(mid_x, 0.0, 0.0), RG.Vector3d.XAxis)
    else:
        mid_y = (bbox.Min.Y + bbox.Max.Y) / 2.0
        plane = RG.Plane(RG.Point3d(0.0, mid_y, 0.0), RG.Vector3d.YAxis)
        
    split_parts = mesh.Split(plane)
    if not split_parts or len(split_parts) <= 1:
        # 如果切分失败或未切开，直接返回原网格，防止无限递归
        return [mesh]
        
    result = []
    for piece in split_parts:
        piece.Compact()
        result.extend(split_mesh_by_face_count(piece, max_faces))
    return result

def quad_remesh_mesh(mesh, target_count=2000, adaptive_size=50, detect_hard=False):
    """
    使用 QuadRemesh 算法重构 Mesh。
    优先使用 RhinoCommon API，如果不可用则退化为 RunScript。
    """
    try:
        params = RG.QuadRemeshParameters()
        params.TargetQuadCount = target_count
        params.AdaptiveSize = adaptive_size
        params.DetectHardEdges = detect_hard
        
        new_mesh = mesh.QuadRemesh(params)
        if new_mesh:
            return new_mesh
    except Exception as e:
        print("RhinoCommon QuadRemesh API 无法使用，尝试命令行宏: {}".format(e))
        
    # 备用方案: RunScript 命令宏
    doc = Rhino.RhinoDoc.ActiveDoc
    # 临时加入文档
    temp_id = doc.Objects.AddMesh(mesh)
    rs.UnselectAllObjects()
    rs.SelectObject(temp_id)
    
    before_ids = set([obj.Id for obj in doc.Objects])
    cmd = u'_-QuadRemesh _TargetQuadCount={} _AdaptiveSize={} _DetectHardEdges={} _Enter'.format(
        target_count,
        adaptive_size,
        "_Yes" if detect_hard else "_No"
    )
    success = Rhino.RhinoApp.RunScript(cmd, False)
    
    new_mesh = None
    if success:
        after_ids = set([obj.Id for obj in doc.Objects])
        new_ids = after_ids - before_ids
        for oid in new_ids:
            obj = doc.Objects.Find(oid)
            if obj and obj.Geometry.ObjectType == Rhino.DocObjects.ObjectType.Mesh:
                new_mesh = obj.Geometry.Duplicate()
                doc.Objects.Delete(oid, True)
                break
                
    doc.Objects.Delete(temp_id, True)
    return new_mesh

def clip_mesh_to_bounds(mesh, min_x, max_x, min_y, max_y):
    """
    将网格物理剪裁到指定的二维 X-Y 边界内。
    使用垂直平面逐步切割。
    """
    bbox = mesh.GetBoundingBox(True)
    if not bbox.IsValid:
        return None
        
    # 快速排查：完全在外部
    if bbox.Max.X < min_x or bbox.Min.X > max_x or bbox.Max.Y < min_y or bbox.Min.Y > max_y:
        return None
        
    # 快速排查：完全在内部
    if bbox.Min.X >= min_x and bbox.Max.X <= max_x and bbox.Min.Y >= min_y and bbox.Max.Y <= max_y:
        return mesh.Duplicate()
        
    current_meshes = [mesh.Duplicate()]
    
    # 定义四个切割边界
    # (plane, axis, limit_val, is_lower_bound)
    planes_info = [
        # x >= min_x
        (RG.Plane(RG.Point3d(min_x, 0.0, 0.0), RG.Vector3d.XAxis), "x", min_x, True),
        # x <= max_x
        (RG.Plane(RG.Point3d(max_x, 0.0, 0.0), RG.Vector3d(-1.0, 0.0, 0.0)), "x", max_x, False),
        # y >= min_y
        (RG.Plane(RG.Point3d(0.0, min_y, 0.0), RG.Vector3d.YAxis), "y", min_y, True),
        # y <= max_y
        (RG.Plane(RG.Point3d(0.0, max_y, 0.0), RG.Vector3d(0.0, -1.0, 0.0)), "y", max_y, False)
    ]
    
    for plane, axis, limit, is_lower in planes_info:
        next_meshes = []
        for m in current_meshes:
            m_bbox = m.GetBoundingBox(True)
            if not m_bbox.IsValid:
                continue
                
            if axis == "x":
                min_val, max_val = m_bbox.Min.X, m_bbox.Max.X
            else:
                min_val, max_val = m_bbox.Min.Y, m_bbox.Max.Y
                
            # 判断与边界的位置关系
            if is_lower:
                # 需保留 >= limit 的部分
                if min_val >= limit - 1e-5:
                    next_meshes.append(m)
                    continue
                if max_val <= limit + 1e-5:
                    continue
            else:
                # 需保留 <= limit 的部分
                if max_val <= limit + 1e-5:
                    next_meshes.append(m)
                    continue
                if min_val >= limit - 1e-5:
                    continue
                    
            # 跨越边界，物理切割
            split_parts = m.Split(plane)
            if split_parts:
                has_parts = False
                for part in split_parts:
                    has_parts = True
                    p_bbox = part.GetBoundingBox(True)
                    if p_bbox.IsValid:
                        center_val = p_bbox.Center.X if axis == "x" else p_bbox.Center.Y
                        if is_lower:
                            if center_val >= limit - 1e-5:
                                next_meshes.append(part)
                        else:
                            if center_val <= limit + 1e-5:
                                next_meshes.append(part)
                if not has_parts:
                    center_val = m_bbox.Center.X if axis == "x" else m_bbox.Center.Y
                    if is_lower:
                        if center_val >= limit:
                            next_meshes.append(m)
                    else:
                        if center_val <= limit:
                            next_meshes.append(m)
            else:
                center_val = m_bbox.Center.X if axis == "x" else m_bbox.Center.Y
                if is_lower:
                    if center_val >= limit:
                        next_meshes.append(m)
                else:
                    if center_val <= limit:
                        next_meshes.append(m)
                        
        current_meshes = next_meshes
        if not current_meshes:
            break
            
    if not current_meshes:
        return None
        
    final_mesh = RG.Mesh()
    for m in current_meshes:
        final_mesh.Append(m)
    final_mesh.Compact()
    
    if final_mesh.Vertices.Count == 0:
        return None
        
    # 对裁剪出的建筑网格进行封口处理，填补切割产生的截面开口
    final_mesh.FillHoles()
    
    return final_mesh

class TeeLogger(object):
    def __init__(self, log_file, original_stream):
        self.terminal = original_stream
        self.log = log_file
        
    def write(self, message):
        self.terminal.write(message)
        if self.log:
            try:
                self.log.write(message)
                self.log.flush()
            except:
                pass
                
    def flush(self):
        self.terminal.flush()
        if self.log:
            try:
                self.log.flush()
            except:
                pass

def process_zone(zone_dir, zone_name, import_data=True, delete_after_process=False):
    """
    包装器：重定向 stdout/stderr 到独立的文件日志，然后再执行实际的区块处理。
    """
    import os
    import sys
    
    script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
    output_dir = os.path.join(script_dir, "output", zone_name)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    log_file_path = os.path.join(output_dir, "{}_process.log".format(zone_name))
    import io
    log_file = io.open(log_file_path, "w", encoding="utf-8")
    
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    
    sys.stdout = TeeLogger(log_file, original_stdout)
    sys.stderr = TeeLogger(log_file, original_stderr)
    
    try:
        _process_zone_internal(zone_dir, zone_name, import_data, delete_after_process)
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        try:
            log_file.close()
        except:
            pass

def _process_zone_internal(zone_dir, zone_name, import_data=True, delete_after_process=False):
    """
    处理单个城市区块（如 bunkyo-ku）
    1. 扫描子目录，导入 dem 和 bldg 的 glb 文件，或者从已有图层读取
    2. 对 dem 进行 join，然后进行 QuadRemesh 重构
    3. 对 bldg 直接进行 join，然后按 500m * 500m 拆分
    4. 分类建筑与地形，存入对应的 tile 图层
    5. 保存为 3dm 文件并进行清理
    """
    print("\n" + "="*50)
    print("开始处理区块: {}".format(zone_name))
    print("="*50)
    
    doc = Rhino.RhinoDoc.ActiveDoc
    
    # 0. 预先通过 CheckDEMFaces 模块在磁盘上分析 GLB 地形文件以决定 QuadRemesh 的比率，避免内存超载
    pre_calculated_faces = 0
    dem_files_info = []
    if import_data:
        dem_dir = os.path.join(zone_dir, "dem")
        if os.path.exists(dem_dir):
            try:
                import sys
                script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
                if script_dir not in sys.path:
                    sys.path.append(script_dir)
                import CheckDEMFaces
                
                dem_files = [f for f in os.listdir(dem_dir) if f.lower().endswith(".glb")]
                for filename in dem_files:
                    filepath = os.path.join(dem_dir, filename)
                    faces, _ = CheckDEMFaces.parse_glb_stats(filepath)
                    pre_calculated_faces += faces
                    dem_files_info.append((filename, faces))
                    
                if pre_calculated_faces > 0:
                    print("预分析：磁盘上 DEM 地形网格总面数为: {}".format(pre_calculated_faces))
            except Exception as e:
                print("预分析警告: 无法通过 CheckDEMFaces 预加载评估面数: {}".format(e))
                
    dem_layer_path = "world::{}::dem".format(zone_name)
    bldg_layer_path = "world::{}::bldg".format(zone_name)
    
    dem_layer_idx = -1
    bldg_layer_idx = -1
    
    dem_meshes_in_doc = []
    bldg_meshes_in_doc = []
    
    use_batched_remesh = (import_data and USE_QUAD_REMESH and pre_calculated_faces > QUAD_REMESH_LARGE_MESH_THRESHOLD)
    joined_dem_mesh = None
    
    if import_data:
        # 1. 建立图层结构
        dem_layer_idx = get_or_create_layer(dem_layer_path)
        bldg_layer_idx = get_or_create_layer(bldg_layer_path)
        
        dem_dir = os.path.join(zone_dir, "dem")
        bldg_dir = os.path.join(zone_dir, "bldg")
        
        if use_batched_remesh:
            print("检测到地形总面数较大 ({})，启动两阶段分批次 QuadRemesh...".format(pre_calculated_faces))
            
            # 1. 划分批次
            batches = []
            current_batch = []
            current_batch_faces = 0
            batch_limit = 1500000  # 150 万面为一个批次限制
            
            for filename, faces in dem_files_info:
                if current_batch_faces + faces > batch_limit and len(current_batch) > 0:
                    batches.append((current_batch, current_batch_faces))
                    current_batch = [filename]
                    current_batch_faces = faces
                else:
                    current_batch.append(filename)
                    current_batch_faces += faces
            if current_batch:
                batches.append((current_batch, current_batch_faces))
                
            print("共划分为 {} 个批次进行预重构。".format(len(batches)))
            
            batch_remeshed_meshes = []
            
            # 2. 逐批次导入、合并并预重构
            for b_idx, (b_files, b_faces) in enumerate(batches):
                print("\n--- 正在处理地形批次 [{}/{}] (包含 {} 个文件，累计面数: {}) ---".format(
                    b_idx + 1, len(batches), len(b_files), b_faces
                ))
                
                # 创建临时图层
                temp_batch_layer_name = "temp_batch_{}".format(b_idx)
                temp_batch_layer_idx = get_or_create_layer("world::{}::{}".format(zone_name, temp_batch_layer_name))
                
                # 导入该批次文件
                batch_imported_objs = []
                for filename in b_files:
                    filepath = os.path.join(dem_dir, filename)
                    print("  导入: {}".format(filename))
                    imported = import_glb_to_layer(filepath, temp_batch_layer_idx)
                    batch_imported_objs.extend(imported)
                    
                # 合并该批次
                joined_batch_mesh = RG.Mesh()
                for obj in batch_imported_objs:
                    if obj.Geometry:
                        joined_batch_mesh.Append(obj.Geometry)
                joined_batch_mesh.Compact()
                
                # 清理导入的临时对象
                for obj in batch_imported_objs:
                    doc.Objects.Delete(obj.Id, True)
                    
                # 执行第一阶段 QuadRemesh（目标为原面数的 50%）
                # 为了防范单一大网格（即使在批次中也是单个大 GLB 文件）导致 QuadRemesh 卡住，如果面数超过 1,500,000 则物理切割后分块进行重构
                if joined_batch_mesh.Faces.Count > 1500000:
                    print("  当前批次网格面数较大 ({})，在第一阶段前进行几何切割以降低单次重构压力...".format(joined_batch_mesh.Faces.Count))
                    pieces = split_mesh_by_face_count(joined_batch_mesh, 1200000)
                    print("  网格成功分割为 {} 个子网格块。".format(len(pieces)))
                    
                    piece_remeshed = []
                    for p_idx, piece in enumerate(pieces):
                        piece_faces = piece.Faces.Count
                        piece_target = int(piece_faces * 0.5)
                        if piece_target < 200000:
                            piece_target = 200000
                        if piece_target > piece_faces:
                            piece_target = piece_faces
                            
                        print("    [子块 {}/{}] 面数: {}, 目标面数: {}...".format(
                            p_idx + 1, len(pieces), piece_faces, piece_target
                        ))
                        
                        quad_piece = quad_remesh_mesh(
                            piece,
                            piece_target,
                            QUAD_REMESH_ADAPTIVE_SIZE,
                            QUAD_REMESH_DETECT_HARD_EDGES
                        )
                        if quad_piece:
                            piece_remeshed.append(quad_piece)
                            print("    [子块 {}/{}] 预重构成功！".format(p_idx + 1, len(pieces)))
                        else:
                            piece_remeshed.append(piece)
                            print("    [子块 {}/{}] 预重构失败，保留原几何。".format(p_idx + 1, len(pieces)))
                            
                    quad_batch = RG.Mesh()
                    for pm in piece_remeshed:
                        quad_batch.Append(pm)
                    quad_batch.Compact()
                else:
                    batch_target = int(b_faces * 0.5)
                    if batch_target < 200000:
                        batch_target = 200000
                    if batch_target > b_faces:
                        batch_target = b_faces
                        
                    print("  对该批次进行第一阶段 QuadRemesh (目标面数: {})...".format(batch_target))
                    quad_batch = quad_remesh_mesh(
                        joined_batch_mesh,
                        batch_target,
                        QUAD_REMESH_ADAPTIVE_SIZE,
                        QUAD_REMESH_DETECT_HARD_EDGES
                    )
                
                # 清理临时图层
                doc.Layers.Purge(doc.Layers[temp_batch_layer_idx].Id, True)
                
                if quad_batch and quad_batch.Faces.Count > 0:
                    batch_remeshed_meshes.append(quad_batch)
                    print("  批次预重构成功！")
                else:
                    batch_remeshed_meshes.append(joined_batch_mesh)
                    print("  批次预重构失败，使用原始合并网格。")
                    
            # 3. 合并所有批次重构后的网格到 joined_dem_mesh 中
            joined_dem_mesh = RG.Mesh()
            for m in batch_remeshed_meshes:
                joined_dem_mesh.Append(m)
            joined_dem_mesh.Compact()
            print("\n所有批次第一阶段预重构完成。合并后地形网格面数为: {}".format(joined_dem_mesh.Faces.Count))
        else:
            # 2. 导入地形 (dem)
            if os.path.exists(dem_dir):
                dem_files = [f for f in os.listdir(dem_dir) if f.lower().endswith(".glb")]
                print("发现 {} 个地形 (dem) 文件，开始导入...".format(len(dem_files)))
                for idx, filename in enumerate(dem_files):
                    filepath = os.path.join(dem_dir, filename)
                    print("  [{}/{}] 导入: {}".format(idx+1, len(dem_files), filename))
                    imported = import_glb_to_layer(filepath, dem_layer_idx)
                    dem_meshes_in_doc.extend(imported)
            else:
                print("未找到 dem 目录: {}".format(dem_dir))
                
        # 3. 导入建筑 (bldg)
        if os.path.exists(bldg_dir):
            bldg_files = [f for f in os.listdir(bldg_dir) if f.lower().endswith(".glb")]
            print("发现 {} 个建筑 (bldg) 文件，开始导入...".format(len(bldg_files)))
            for idx, filename in enumerate(bldg_files):
                filepath = os.path.join(bldg_dir, filename)
                print("  [{}/{}] 导入: {}".format(idx+1, len(bldg_files), filename))
                imported = import_glb_to_layer(filepath, bldg_layer_idx)
                bldg_meshes_in_doc.extend(imported)
        else:
            print("未找到 bldg 目录: {}".format(bldg_dir))
    else:
        # 从已有图层收集
        dem_layer_idx = doc.Layers.FindByFullPath(dem_layer_path, True)
        bldg_layer_idx = doc.Layers.FindByFullPath(bldg_layer_path, True)
        
        if dem_layer_idx >= 0:
            objs = doc.Objects.FindByLayer(doc.Layers[dem_layer_idx])
            dem_meshes_in_doc = [o for o in objs if o.Geometry and o.Geometry.ObjectType == Rhino.DocObjects.ObjectType.Mesh]
        if bldg_layer_idx >= 0:
            objs = doc.Objects.FindByLayer(doc.Layers[bldg_layer_idx])
            bldg_meshes_in_doc = [o for o in objs if o.Geometry and o.Geometry.ObjectType == Rhino.DocObjects.ObjectType.Mesh]

    # 刷新文档，让物体在视口中显示
    doc.Views.Redraw()
    
    # 4. 数据整理与合并
    print("正在进行 Mesh Join 与整理...")
    
    # 4.1 合并地形为唯一的大地形 Mesh
    if not use_batched_remesh:
        joined_dem_mesh = RG.Mesh()
        for obj in dem_meshes_in_doc:
            if obj.Geometry:
                joined_dem_mesh.Append(obj.Geometry)
        joined_dem_mesh.Compact()
        
        # 清理零散地形对象
        for obj in dem_meshes_in_doc:
            doc.Objects.Delete(obj.Id, True)
            
        if USE_QUAD_REMESH and joined_dem_mesh.Faces.Count > 1500000:
            print("  检测到未重构地形网格面数较大 ({})，启动第一阶段几何分割预重构以防止内存溢出...".format(joined_dem_mesh.Faces.Count))
            pieces = split_mesh_by_face_count(joined_dem_mesh, 1200000)
            print("  网格分割为 {} 个子块。".format(len(pieces)))
            
            piece_remeshed = []
            for p_idx, piece in enumerate(pieces):
                piece_faces = piece.Faces.Count
                piece_target = int(piece_faces * 0.5)
                if piece_target < 200000:
                    piece_target = 200000
                if piece_target > piece_faces:
                    piece_target = piece_faces
                    
                print("    [子块 {}/{}] 面数: {}, 目标面数: {}...".format(
                    p_idx + 1, len(pieces), piece_faces, piece_target
                ))
                
                quad_piece = quad_remesh_mesh(
                    piece,
                    piece_target,
                    QUAD_REMESH_ADAPTIVE_SIZE,
                    QUAD_REMESH_DETECT_HARD_EDGES
                )
                if quad_piece:
                    piece_remeshed.append(quad_piece)
                else:
                    piece_remeshed.append(piece)
                    
            joined_dem_mesh = RG.Mesh()
            for pm in piece_remeshed:
                joined_dem_mesh.Append(pm)
            joined_dem_mesh.Compact()
            print("  第一阶段预重构完成。合并后地形网格面数为: {}".format(joined_dem_mesh.Faces.Count))
            
    if joined_dem_mesh.Vertices.Count == 0:
        print("错误: 区块 {} 没有有效的地形数据，跳过切分。".format(zone_name))
        return
        
    # 对地形进行 QuadRemesh 重构
    if USE_QUAD_REMESH:
        orig_faces = pre_calculated_faces if pre_calculated_faces > 0 else joined_dem_mesh.Faces.Count
        
        # 1. 查找手动配置的比例
        manual_ratio = None
        for key, val in QUAD_REMESH_RATIO_OVERRIDES.items():
            if key.lower() == zone_name.lower() or key.lower() in zone_name.lower() or zone_name.lower() in key.lower():
                if val is not None:
                    manual_ratio = val
                break
                
        # 2. 计算目标面数
        if manual_ratio is not None:
            # 优先使用字典手动配置的比例
            ratio = manual_ratio
            target_count = int(orig_faces * ratio)
            print("正在对地形进行 QuadRemesh 重构 (区块: {}, 使用手动比例: {}, 原始面数: {}, 目标面数: {})...".format(
                zone_name, ratio, orig_faces, target_count
            ))
        else:
            # 启用动态降级比例
            if orig_faces > QUAD_REMESH_LARGE_MESH_THRESHOLD:
                # 超过阈值（如 200w），目标定在 100w，但限制在硬下限与原始面数之间
                target_count = QUAD_REMESH_LARGE_MESH_TARGET
                if target_count < QUAD_REMESH_MIN_TARGET_FACES:
                    target_count = QUAD_REMESH_MIN_TARGET_FACES
                if target_count > orig_faces:
                    target_count = orig_faces
                ratio = float(target_count) / orig_faces
                print("正在对地形进行 QuadRemesh 重构 (区块: {}, 触发大网格动态调整, 实际缩减比: {:.2%}, 原始面数: {}, 目标面数: {})...".format(
                    zone_name, ratio, orig_faces, target_count
                ))
            else:
                # 未超阈值，采用全局默认比例 (50%)
                ratio = QUAD_REMESH_DEFAULT_RATIO
                target_count = int(orig_faces * ratio)
                
                # 如果是较大网格缩减后的面数低于硬下限 80w，且原始面数本来就高于 80w，应当保留在 80w（提升精度）
                if orig_faces >= QUAD_REMESH_MIN_TARGET_FACES:
                    if target_count < QUAD_REMESH_MIN_TARGET_FACES:
                        target_count = QUAD_REMESH_MIN_TARGET_FACES
                        ratio = float(target_count) / orig_faces
                        
                print("正在对地形进行 QuadRemesh 重构 (区块: {}, 使用默认比例: {}, 原始面数: {}, 目标面数: {})...".format(
                    zone_name, ratio, orig_faces, target_count
                ))
                
        if target_count < 100:
            target_count = 100
            
        quad_dem_mesh = quad_remesh_mesh(
            joined_dem_mesh,
            target_count,
            QUAD_REMESH_ADAPTIVE_SIZE,
            QUAD_REMESH_DETECT_HARD_EDGES
        )
        if quad_dem_mesh:
            print("QuadRemesh 重构成功！")
            quad_dem_mesh.Compact()
            # 熔合所有顶点以确保网格完全 join 缝合在一起
            quad_dem_mesh.Weld(Rhino.RhinoMath.ToRadians(180))
            terrain_mesh = quad_dem_mesh
        else:
            print("错误: QuadRemesh 重构第二阶段失败！停止后续所有步骤。")
            return
    else:
        terrain_mesh = joined_dem_mesh
        
    # 将重构/合并后的地形放回文档的 dem 图层（临时）
    if dem_layer_idx < 0:
        dem_layer_idx = get_or_create_layer(dem_layer_path)
    joined_dem_id = doc.Objects.AddMesh(terrain_mesh)
    joined_dem_obj = doc.Objects.Find(joined_dem_id)
    joined_dem_obj.Attributes.LayerIndex = dem_layer_idx
    joined_dem_obj.CommitChanges()
    
    # 4.2 收集并拆分建筑 Mesh 为单体，避免大网格在后续物理分割时卡死
    bldg_pieces = []
    print("正在拆分建筑为单体...")
    for obj in bldg_meshes_in_doc:
        if obj.Geometry and obj.Geometry.ObjectType == Rhino.DocObjects.ObjectType.Mesh:
            pieces = obj.Geometry.SplitDisjointPieces()
            if pieces:
                bldg_pieces.extend(pieces)
            else:
                bldg_pieces.append(obj.Geometry.Duplicate())
    
    # 清理零散建筑对象
    for obj in bldg_meshes_in_doc:
        doc.Objects.Delete(obj.Id, True)
        
    # 将合并后的建筑放回文档的 bldg 图层（临时，用于 3dm 保存参考）
    joined_bldg_id = None
    if len(bldg_pieces) > 0:
        if bldg_layer_idx < 0:
            bldg_layer_idx = get_or_create_layer(bldg_layer_path)
        # 创建一个合并的临时对象放回图层（仅用于保存参考，不参与切分计算）
        joined_bldg_mesh = RG.Mesh()
        joined_bldg_mesh.Append(bldg_pieces)
        joined_bldg_mesh.Compact()
        joined_bldg_id = doc.Objects.AddMesh(joined_bldg_mesh)
        joined_bldg_obj = doc.Objects.Find(joined_bldg_id)
        joined_bldg_obj.Attributes.LayerIndex = bldg_layer_idx
        joined_bldg_obj.CommitChanges()
        
        print("建筑收集与单体拆分完成，共 {} 个独立单体建筑 Mesh 对象。".format(len(bldg_pieces)))
    else:
        print("未发现有效建筑数据。")
        
    # 提前计算所有单体建筑的 BoundingBox，避免在瓦片循环中重复计算
    bldg_pieces_with_bbox = []
    for bmesh in bldg_pieces:
        bbox = bmesh.GetBoundingBox(True)
        if bbox.IsValid:
            bldg_pieces_with_bbox.append((bmesh, bbox))
        
    # 5. 划分 500x500m 瓦片
    print("开始进行瓦片划分与重构 (瓦片大小: {}x{}米, 采样间距: {}米)...".format(TILE_SIZE, TILE_SIZE, GRID_SPACING))
    
    bbox = terrain_mesh.GetBoundingBox(True)
    min_x, max_x = bbox.Min.X, bbox.Max.X
    min_y, max_y = bbox.Min.Y, bbox.Max.Y
    max_z = bbox.Max.Z
    
    # 全局对齐 500 米网格边界
    min_i = int(math.floor(min_x / TILE_SIZE))
    max_i = int(math.ceil(max_x / TILE_SIZE))
    min_j = int(math.floor(min_y / TILE_SIZE))
    max_j = int(math.ceil(max_y / TILE_SIZE))
    
    total_tiles = (max_i - min_i) * (max_j - min_j)
    processed_count = 0
    exported_count = 0
    
    # 创建输出目录
    script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
    output_dir = os.path.join(script_dir, "output", zone_name)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    for i in range(min_i, max_i):
        for j in range(min_j, max_j):
            processed_count += 1
            
            # 该瓦片 500x500 范围
            tile_min_x = i * TILE_SIZE
            tile_max_x = (i + 1) * TILE_SIZE
            tile_min_y = j * TILE_SIZE
            tile_max_y = (j + 1) * TILE_SIZE
            
            # 5.1 完整性判定 (5x5 采样点)
            is_complete = True
            sample_count = 5
            for u in range(sample_count):
                for v in range(sample_count):
                    sx = tile_min_x + (u / float(sample_count - 1)) * TILE_SIZE
                    sy = tile_min_y + (v / float(sample_count - 1)) * TILE_SIZE
                    
                    # 垂直向下投射射线
                    ray_start = RG.Point3d(sx, sy, max_z + 50.0)
                    ray = RG.Ray3d(ray_start, RG.Vector3d(0, 0, -1))
                    t = RG.Intersect.Intersection.MeshRay(terrain_mesh, ray)
                    if t < 0.0:
                        is_complete = False
                        break
                if not is_complete:
                    break
                    
            if ONLY_COMPLETE_FILES and not is_complete:
                # 处于边缘或外部，不完整，直接忽略
                continue
                
            # 5.2 规则格网采样 + Delaunay 地形重构
            grid_n = int(TILE_SIZE / GRID_SPACING)
            pts_list = []
            is_covered = True
            grid_hits = 0
            for u in range(grid_n + 1):
                for v in range(grid_n + 1):
                    gx = tile_min_x + (u / float(grid_n)) * TILE_SIZE
                    gy = tile_min_y + (v / float(grid_n)) * TILE_SIZE
                    
                    # 获取地形高度
                    ray_start = RG.Point3d(gx, gy, max_z + 50.0)
                    ray = RG.Ray3d(ray_start, RG.Vector3d(0, 0, -1))
                    t = RG.Intersect.Intersection.MeshRay(terrain_mesh, ray)
                    if t >= 0.0:
                        gz = ray_start.Z - t
                        grid_hits += 1
                    else:
                        if ONLY_COMPLETE_FILES:
                            is_covered = False
                            break
                        # 如果不需要过滤，则使用最近点高度兜底
                        mesh_point = terrain_mesh.ClosestPoint(RG.Point3d(gx, gy, 0.0))
                        gz = mesh_point.Z if mesh_point.IsValid else 0.0
                    pts_list.append(RG.Point3d(gx, gy, gz))
                if not is_covered:
                    break
            
            if ONLY_COMPLETE_FILES and not is_covered:
                # 地形没有完全铺满 500x500，说明属于边缘，忽略
                continue
                
            # 规则网格直接构建 Mesh
            grid_cols = grid_n + 1
            grid_rows = grid_n + 1
            tile_dem_mesh = create_grid_mesh(pts_list, grid_cols, grid_rows)
            if not tile_dem_mesh or tile_dem_mesh.Vertices.Count == 0:
                continue
                
            # 5.3 搜集并物理切分落在该瓦片内的建筑 Mesh
            tile_bldg_meshes = []
            for bmesh, bbox in bldg_pieces_with_bbox:
                # 快速排查：完全在外部
                if bbox.Max.X < tile_min_x or bbox.Min.X > tile_max_x or bbox.Max.Y < tile_min_y or bbox.Min.Y > tile_max_y:
                    continue
                # 快速排查：完全在内部
                if bbox.Min.X >= tile_min_x and bbox.Max.X <= tile_max_x and bbox.Min.Y >= tile_min_y and bbox.Max.Y <= tile_max_y:
                    tile_bldg_meshes.append(bmesh.Duplicate())
                    continue
                # 部分相交，调用物理切割
                clipped = clip_mesh_to_bounds(bmesh, tile_min_x, tile_max_x, tile_min_y, tile_max_y)
                if clipped and clipped.Vertices.Count > 0:
                    tile_bldg_meshes.append(clipped)
                    
            # 如果该瓦片既没有任何地形采样点命中，也没有任何建筑物落入，说明属于无效外部，直接忽略
            if grid_hits == 0 and len(tile_bldg_meshes) == 0:
                continue
                
            # 5.4 创建该瓦片的专有图层结构，并存入网格
            tile_layer_name = "tile_{:.0f}_{:.0f}".format(tile_min_x, tile_min_y)
            tile_dem_layer_path = "world::{}::{}::dem".format(zone_name, tile_layer_name)
            tile_bldg_layer_path = "world::{}::{}::bldg".format(zone_name, tile_layer_name)
            
            tile_dem_layer_idx = get_or_create_layer(tile_dem_layer_path)
            tile_bldg_layer_idx = get_or_create_layer(tile_bldg_layer_path)
            
            # 添加地形到对应图层
            tile_dem_id = doc.Objects.AddMesh(tile_dem_mesh)
            tile_dem_obj = doc.Objects.Find(tile_dem_id)
            if tile_dem_obj:
                tile_dem_obj.Attributes.LayerIndex = tile_dem_layer_idx
                tile_dem_obj.CommitChanges()
                
            # 添加建筑到对应图层
            tile_bldg_ids = []
            if len(tile_bldg_meshes) > 0:
                if JOIN_BUILDINGS_PER_TILE:
                    # 合并成单个 Mesh
                    tiled_joined_bldg = RG.Mesh()
                    tiled_joined_bldg.Append(tile_bldg_meshes)
                    tiled_joined_bldg.Compact()
                    if tiled_joined_bldg.Vertices.Count > 0:
                        bid = doc.Objects.AddMesh(tiled_joined_bldg)
                        tile_bldg_ids.append(bid)
                else:
                    # 保持独立单体
                    for bm in tile_bldg_meshes:
                        if bm.Vertices.Count > 0:
                            bid = doc.Objects.AddMesh(bm)
                            tile_bldg_ids.append(bid)
                            
                for bid in tile_bldg_ids:
                    bobj = doc.Objects.Find(bid)
                    if bobj:
                        bobj.Attributes.LayerIndex = tile_bldg_layer_idx
                        bobj.CommitChanges()
            
            # 5.5 如果配置需要导出 GLB/OBJ
            if EXPORT_TILES:
                rs.UnselectAllObjects()
                rs.SelectObject(tile_dem_id)
                if tile_bldg_ids:
                    rs.SelectObjects(tile_bldg_ids)
                    
                filename = "{}_tile_{:.0f}_{:.0f}{}".format(zone_name, tile_min_x, tile_min_y, EXPORT_FORMAT)
                filepath = os.path.join(output_dir, filename)
                cmd = u'_-Export "{}" _Enter _Enter'.format(filepath)
                Rhino.RhinoApp.RunScript(cmd, False)
                
            exported_count += 1
            if exported_count % 5 == 0:
                # 频繁垃圾回收，防止内存波峰过高
                System.GC.Collect()
                System.GC.WaitForPendingFinalizers()
            if exported_count % 10 == 0:
                print("  已完成 {} 个瓦片切分...".format(exported_count))
                
    # 5.6 清理大尺寸的未切割源 Mesh，防止污染保存的 3dm 文件
    if joined_dem_id:
        doc.Objects.Delete(joined_dem_id, True)
    if joined_bldg_id:
        doc.Objects.Delete(joined_bldg_id, True)
        
    # 5.7 保存为 3dm 文件，文件名为区块名称
    if SAVE_3DM:
        output_3dm_path = os.path.join(script_dir, "output", "{}.3dm".format(zone_name))
        parent_dir = os.path.dirname(output_3dm_path)
        if not os.path.exists(parent_dir):
            os.makedirs(parent_dir)
            
        print("正在保存区块 3dm 文件到: {}".format(output_3dm_path))
        cmd = u'_-SaveAs "{}" _Enter'.format(output_3dm_path)
        success = Rhino.RhinoApp.RunScript(cmd, False)
        if success:
            print("区块 3dm 文件保存成功！")
        else:
            print("警告: 3dm 文件保存可能失败。")
            
    # 5.8 批处理后清理当前文档，释放内存和空间
    if delete_after_process:
        print("正在清理当前文档物体，准备处理下一个区块...")
        for obj in list(doc.Objects):
            doc.Objects.Delete(obj.Id, True)
        purge_empty_layers()
        doc.Modified = False
        
    # 显式触发垃圾回收，释放内存
    System.GC.Collect()
    System.GC.WaitForPendingFinalizers()
    
    # 清理多余空图层
    purge_empty_layers()
    
    doc.Views.Redraw()
    print("区块 {} 处理完成！共扫描 {} 个格网，成功生成 {} 个完整瓦片。".format(
        zone_name, total_tiles, exported_count))

def main():
    # 获取脚本主目录
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except:
        script_dir = None
        
    if not script_dir:
        script_dir = os.getcwd()

    # 优先检测环境变量或当前区块配置文件，从而支持外部 Python 脚本自动调用
    target_zone = os.environ.get("TARGET_ZONE")
    is_auto = False
    
    config_file = os.path.join(script_dir, ".active_zone.json")
    if not target_zone and os.path.exists(config_file):
        try:
            import json
            with open(config_file, "r") as f:
                config_data = json.load(f)
                target_zone = config_data.get("target_zone")
        except Exception as e:
            print("读取自动配置文件失败:", e)

    if target_zone:
        is_auto = True
        print("检测到自动运行模式。目标区块为: {}".format(target_zone))

    doc = Rhino.RhinoDoc.ActiveDoc

    # 检查文档单位并自动转换为米
    if doc.ModelUnitSystem != Rhino.UnitSystem.Meters:
        print("警告: 当前 Rhino 文档单位不是 '米' (Meters)。")
        if is_auto:
            doc.AdjustModelUnitSystem(Rhino.UnitSystem.Meters, True)
            print("已自动将文档单位修改为: 米 (Meters)")
        else:
            response = rs.MessageBox(
                "当前文档单位不是 '米' (Meters)，是否自动修改文档单位为 '米'？\n这对于 500 米瓦片的正确切分至关重要。", 
                4 + 32, # YesNo + Question
                "单位不匹配"
            )
            if response == 6: # Yes
                doc.AdjustModelUnitSystem(Rhino.UnitSystem.Meters, True)
                print("已自动将文档单位修改为: 米 (Meters)")
            else:
                print("未修改文档单位，切分结果可能会偏离实际尺寸！")

    # 如果是手动模式且没有识别到区块子文件夹，弹窗询问主目录
    has_zones = False
    if os.path.exists(script_dir):
        for d in os.listdir(script_dir):
            if d.lower() == "output" or d.lower() == "temp" or d.startswith("."):
                continue
            d_path = os.path.join(script_dir, d)
            if os.path.isdir(d_path):
                if os.path.exists(os.path.join(d_path, "dem")) or os.path.exists(os.path.join(d_path, "bldg")):
                    has_zones = True
                    break
                    
    if not is_auto and not has_zones:
        # 弹窗询问主目录
        script_dir = rs.BrowseForFolder(None, "请选择包含城市区块文件夹（如 01-bunkyo-ku）的主目录", "选择主目录")
        
    if not script_dir or not os.path.exists(script_dir):
        print("无效的目录，程序退出。")
        return
        
    # 扫描包含 dem / bldg 的子文件夹
    subdirs = [d for d in os.listdir(script_dir) if os.path.isdir(os.path.join(script_dir, d))]
    zones = []
    for d in subdirs:
        if d.lower() == "output" or d.lower() == "temp" or d.startswith("."):
            continue
        d_path = os.path.join(script_dir, d)
        if os.path.exists(os.path.join(d_path, "dem")) or os.path.exists(os.path.join(d_path, "bldg")):
            zones.append((d_path, d))
            
    if not zones:
        print("在主目录 {} 下未找到符合结构的城市区块文件夹 (需包含 dem 或 bldg 目录)。".format(script_dir))
        return
        
    # 选择操作模式
    opts = ["全部导入并切分导出", "仅导入整理(不切分)", "仅对已导入的图层进行切分导出"]
    
    if is_auto:
        mode = opts[0]
        # 过滤出 target_zone
        zones = [z for z in zones if z[1] == target_zone]
        if not zones:
            print("错误: 在主目录下未找到指定的区块: {}".format(target_zone))
            return
    else:
        print("扫描到以下待处理的城市区块: ")
        for idx, (_, name) in enumerate(zones):
            print("  {}. {}".format(idx+1, name))
            
        mode = rs.ListBox(opts, "请选择执行模式: ", "模式选择", default=opts[0])
        
        if not mode:
            print("取消操作。")
            return
            
        # 如果是涉及导入的分区模式，允许用户选择处理全部或特定单一分区
        if mode in [opts[0], opts[1]]:
            zone_names = [z[1] for z in zones]
            select_opts = ["== 全部处理 =="] + zone_names
            selected_zone = rs.ListBox(select_opts, "请选择要处理的区块: ", "选择区块", default=select_opts[0])
            
            if not selected_zone:
                print("取消操作。")
                return
                
            if selected_zone != "== 全部处理 ==":
                zones = [z for z in zones if z[1] == selected_zone]
        
    # 关闭重绘提升海量 Mesh 操作性能
    doc.Views.RedrawEnabled = False
    
    try:
        if mode == opts[0]: # 全部导入并切分导出
            for zone_dir, zone_name in zones:
                process_zone(zone_dir, zone_name, import_data=True, delete_after_process=True)
                
        elif mode == opts[1]: # 仅导入整理
            for zone_dir, zone_name in zones:
                dem_layer_path = "world::{}::dem".format(zone_name)
                bldg_layer_path = "world::{}::bldg".format(zone_name)
                dem_layer_idx = get_or_create_layer(dem_layer_path)
                bldg_layer_idx = get_or_create_layer(bldg_layer_path)
                
                # 仅导入 dem
                dem_dir = os.path.join(zone_dir, "dem")
                if os.path.exists(dem_dir):
                    dem_files = [f for f in os.listdir(dem_dir) if f.lower().endswith(".glb")]
                    for f in dem_files:
                        import_glb_to_layer(os.path.join(dem_dir, f), dem_layer_idx)
                
                # 仅导入 bldg
                bldg_dir = os.path.join(zone_dir, "bldg")
                if os.path.exists(bldg_dir):
                    bldg_files = [f for f in os.listdir(bldg_dir) if f.lower().endswith(".glb")]
                    for f in bldg_files:
                        import_glb_to_layer(os.path.join(bldg_dir, f), bldg_layer_idx)
            print("所有数据已成功导入并分类到相应图层！")
            
        elif mode == opts[2]: # 仅对已导入的图层进行切分导出
            # 遍历 Rhino 里的 world 下子图层
            world_layer_idx = doc.Layers.FindByFullPath("world", True)
            if world_layer_idx < 0:
                print("未在当前文档中找到名为 'world' 的根图层！请先导入数据。")
                return
                
            world_layer = doc.Layers[world_layer_idx]
            children = world_layer.GetChildren()
            if not children:
                print("根图层 'world' 下没有子图层！")
                return
                
            # 允许用户选择要切分的已导入图层区块
            zone_names = [child.Name for child in children]
            select_opts = ["== 全部处理 =="] + zone_names
            selected_zone = rs.ListBox(select_opts, "请选择要切分的已导入区块: ", "选择已导入图层", default=select_opts[0])
            
            if not selected_zone:
                print("取消已导入切分操作。")
                return
                
            if selected_zone != "== 全部处理 ==":
                children = [child for child in children if child.Name == selected_zone]
                
            for child in children:
                zone_name = child.Name
                zone_dir = os.path.join(script_dir, zone_name)
                process_zone(zone_dir, zone_name, import_data=False, delete_after_process=False)
                
    finally:
        # 无论如何，清理多余空图层并恢复视口重绘
        purge_empty_layers()
        doc.Views.RedrawEnabled = True
        doc.Views.Redraw()
        rs.UnselectAllObjects()
        print("\n所有任务执行完毕！")
        
        # 自动模式下，处理完毕后自动退出 Rhino，避免弹出保存提示
        if is_auto:
            # 清理配置文件
            if config_file and os.path.exists(config_file):
                try:
                    os.remove(config_file)
                except:
                    pass
            doc.Modified = False
            print("正在退出 Rhino...")
            Rhino.RhinoApp.RunScript("-_Exit", False)

main()
