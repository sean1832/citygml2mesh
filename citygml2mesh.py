"""
CityGML (Project PLATEAU) to OBJ/GLB mesh converter for CAD tools.

Extracts 3D geometry from PLATEAU CityGML 2.0 datasets (buildings and terrain)
and writes OBJ or GLB meshes in a local metric frame suitable for Rhino, Blender,
and other CAD software. Textures and semantic attributes are intentionally dropped;
this is a geometry-only exporter.

Input coordinates are EPSG:6697 (JGD2011 geographic 3D): latitude, longitude in
degrees and ellipsoidal height in metres, in that axis order. Vertices are converted
geodetic -> ECEF -> local ENU tangent plane about a shared origin, then recentred so
CAD tools keep floating-point precision.

Input may be a PLATEAU .zip (GML entries are streamed directly, textures are never
extracted), a dataset directory, or a single .gml file.

Basic usage:
    python citygml2mesh.py data/13104_shinjuku-ku_pref_2023_citygml_2_op.zip -o out/

Options:
    --format glb|obj|both
    --up z|y          up axis for OBJ; GLB is always glTF-standard Y-up
    --recenter dataset|file
    --themes bldg,dem

Batch usage:
    python citygml2mesh.py --pipeline pipeline.json

The pipeline file is a JSON object with an optional "defaults" block and a "jobs"
array; each job is one conversion (the same work as a single-file run) with per-job
overrides. Relative input/output paths resolve against the pipeline file's own
directory, so the pipeline is portable. See pipeline.example.json.

GLB is written as spec-compliant Y-up: glTF mandates Y-up and importers (Rhino,
Blender, three.js) convert it to their own axis, so a Z-up GLB would import
rotated. OBJ has no up-axis convention, so --up controls it directly (default
Z-up suits Rhino and most CAD tools).
"""

from __future__ import annotations

import argparse
import array
import json
import re
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable

import numpy as np
import trimesh
from lxml import etree
import mapbox_earcut as earcut

# CityGML / GML namespaces used by PLATEAU 2.0.
GML = "http://www.opengis.net/gml"
CORE = "http://www.opengis.net/citygml/2.0"

GML_POLYGON = f"{{{GML}}}Polygon"
GML_TRIANGLE = f"{{{GML}}}Triangle"
GML_ENVELOPE = f"{{{GML}}}Envelope"
GML_POSLIST = f"{{{GML}}}posList"
GML_ID = f"{{{GML}}}id"
GML_LOWER = f"{{{GML}}}lowerCorner"
GML_UPPER = f"{{{GML}}}upperCorner"
CORE_MEMBER = f"{{{CORE}}}cityObjectMember"

# GRS80 ellipsoid (JGD2011). Differences from WGS84 are sub-millimetre, so the
# WGS84 flattening is reused directly.
ELLIPSOID_A = 6378137.0
ELLIPSOID_F = 1.0 / 298.257223563
ELLIPSOID_E2 = ELLIPSOID_F * (2.0 - ELLIPSOID_F)

# Building geometry containers per LOD. LOD2 geometry lives inside boundedBy
# semantic surfaces (WallSurface etc.) as lod2MultiSurface, and sometimes as a
# direct lod2Solid/lod2Geometry. lod0RoofEdge is a flat outline and is ignored.
LOD2_CONTAINERS = {"lod2Solid", "lod2MultiSurface", "lod2Geometry"}
LOD1_CONTAINERS = {"lod1Solid", "lod1MultiSurface"}

Vector3 = tuple[float, float, float]


@dataclass
class Source:
    """One GML input to convert.

    name: logical path used to derive the output filename.
    theme: 'bldg' or 'dem'.
    open_stream: opens a fresh binary stream over the GML content.
    """

    name: str
    theme: str
    open_stream: Callable[[], IO[bytes]]


# --- coordinate transforms (dependency-free math, vectorised with numpy) ---


def geodetic_to_ecef(lat_lon_h: np.ndarray) -> np.ndarray:
    """Convert geodetic coordinates to ECEF.

    lat_lon_h: (N, 3) array of latitude, longitude (degrees) and height (metres).
    Returns an (N, 3) array of ECEF X, Y, Z in metres.
    """
    latitude = np.radians(lat_lon_h[:, 0])
    longitude = np.radians(lat_lon_h[:, 1])
    height = lat_lon_h[:, 2]
    sin_lat = np.sin(latitude)
    cos_lat = np.cos(latitude)
    sin_lon = np.sin(longitude)
    cos_lon = np.cos(longitude)
    prime_vertical = ELLIPSOID_A / np.sqrt(1.0 - ELLIPSOID_E2 * sin_lat * sin_lat)
    x = (prime_vertical + height) * cos_lat * cos_lon
    y = (prime_vertical + height) * cos_lat * sin_lon
    z = (prime_vertical * (1.0 - ELLIPSOID_E2) + height) * sin_lat
    return np.column_stack((x, y, z))


def enu_basis(origin_lat_deg: float, origin_lon_deg: float) -> np.ndarray:
    """Return the ENU rotation whose rows are the East, North, Up unit vectors
    expressed in ECEF at the given origin latitude/longitude (degrees)."""
    latitude = np.radians(origin_lat_deg)
    longitude = np.radians(origin_lon_deg)
    sin_lat = np.sin(latitude)
    cos_lat = np.cos(latitude)
    sin_lon = np.sin(longitude)
    cos_lon = np.cos(longitude)
    east = (-sin_lon, cos_lon, 0.0)
    north = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)
    up = (cos_lat * cos_lon, cos_lat * sin_lon, sin_lat)
    return np.array((east, north, up))


def geodetic_to_enu(lat_lon_h: np.ndarray, origin: Vector3) -> np.ndarray:
    """Convert geodetic coordinates to a local ENU frame about origin.

    lat_lon_h: (N, 3) latitude, longitude (degrees), height (metres).
    origin: (latitude, longitude, height) of the tangent-plane origin.
    Returns (N, 3) East, North, Up in metres (Z-up).
    """
    ecef = geodetic_to_ecef(lat_lon_h)
    origin_ecef = geodetic_to_ecef(np.array([origin], dtype=np.float64))[0]
    rotation = enu_basis(origin[0], origin[1])
    return (ecef - origin_ecef) @ rotation.T


def apply_up_axis(enu: np.ndarray, up: str) -> np.ndarray:
    """Map ENU (Z-up) vertices to the requested up axis.

    'z' keeps East, North, Up. 'y' maps to (East, Up, -North), a proper rotation
    matching the glTF Y-up convention.
    """
    if up == "z":
        return enu
    east = enu[:, 0]
    north = enu[:, 1]
    up_component = enu[:, 2]
    return np.column_stack((east, up_component, -north))


# --- polygon triangulation ---


def newell_normal(points: np.ndarray) -> np.ndarray:
    """Return the (unnormalised) Newell normal of a 3D polygon ring."""
    following = np.roll(points, -1, axis=0)
    nx = np.sum((points[:, 1] - following[:, 1]) * (points[:, 2] + following[:, 2]))
    ny = np.sum((points[:, 2] - following[:, 2]) * (points[:, 0] + following[:, 0]))
    nz = np.sum((points[:, 0] - following[:, 0]) * (points[:, 1] + following[:, 1]))
    return np.array((nx, ny, nz))


def project_to_plane(points: np.ndarray, normal: np.ndarray) -> np.ndarray:
    """Project 3D points onto the polygon plane, returning (N, 2) UV coordinates.

    The in-plane basis (u, v) satisfies u x v = normal, so a counter-clockwise
    winding in UV corresponds to a counter-clockwise winding about the normal and
    polygon orientation is preserved through triangulation.
    """
    unit_normal = normal / np.linalg.norm(normal)
    reference = np.array((1.0, 0.0, 0.0))
    if abs(unit_normal[0]) > 0.9:
        reference = np.array((0.0, 1.0, 0.0))
    u = np.cross(reference, unit_normal)
    u = u / np.linalg.norm(u)
    v = np.cross(unit_normal, u)
    return np.column_stack((points @ u, points @ v))


def triangulate_polygon(exterior: np.ndarray, holes: list[np.ndarray]) -> np.ndarray:
    """Triangulate one planar 3D polygon with optional holes.

    exterior: (E, 3) ring vertices, no closing duplicate.
    holes: list of (H, 3) interior ring vertices.
    Returns (T, 3) triangle indices into the concatenation [exterior, *holes].
    Returns an empty array for degenerate polygons.
    """
    normal = newell_normal(exterior)
    if np.linalg.norm(normal) < 1e-12:
        return np.empty((0, 3), dtype=np.int64)
    rings = [exterior, *holes]
    points = np.vstack(rings)
    uv = np.ascontiguousarray(project_to_plane(points, normal))
    ring_ends = np.cumsum([len(ring) for ring in rings]).astype(np.uint32)
    indices = earcut.triangulate_float64(uv, ring_ends)
    if indices.size == 0:
        return np.empty((0, 3), dtype=np.int64)
    return indices.reshape(-1, 3).astype(np.int64)


# --- GML parsing ---


def parse_pos_list(text: str) -> np.ndarray:
    """Parse a gml:posList into an (N, 3) array of latitude, longitude, height."""
    values = np.fromstring(text, sep=" ")
    if values.size < 3 or values.size % 3 != 0:
        return np.empty((0, 3), dtype=np.float64)
    return values.reshape(-1, 3)


def local_name(tag: object) -> str:
    """Return the local name of an lxml tag, or '' for comments/PIs."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def read_ring(
    ring: etree._Element,
    vertices: list[Vector3],
    vertex_index: dict[Vector3, int],
) -> list[int] | None:
    """Read a LinearRing into global vertex indices, deduplicating exact vertices.

    Drops the closing duplicate vertex. Returns None if the ring has fewer than
    three distinct vertices.
    """
    pos = ring.find(GML_POSLIST)
    if pos is None or not pos.text:
        return None
    coords = parse_pos_list(pos.text)
    if coords.shape[0] >= 2 and np.array_equal(coords[0], coords[-1]):
        coords = coords[:-1]
    if coords.shape[0] < 3:
        return None
    indices: list[int] = []
    for row in coords:
        key = (float(row[0]), float(row[1]), float(row[2]))
        index = vertex_index.get(key)
        if index is None:
            index = len(vertices)
            vertices.append(key)
            vertex_index[key] = index
        indices.append(index)
    return indices


def read_polygon(
    polygon: etree._Element,
    vertices: list[Vector3],
    vertex_index: dict[Vector3, int],
) -> tuple[list[int], list[list[int]]] | None:
    """Read a gml:Polygon into (exterior indices, list of hole indices)."""
    exterior_ring = polygon.find(f"{{{GML}}}exterior/{{{GML}}}LinearRing")
    if exterior_ring is None:
        return None
    exterior = read_ring(exterior_ring, vertices, vertex_index)
    if exterior is None:
        return None
    holes: list[list[int]] = []
    for interior_ring in polygon.findall(f"{{{GML}}}interior/{{{GML}}}LinearRing"):
        hole = read_ring(interior_ring, vertices, vertex_index)
        if hole is not None:
            holes.append(hole)
    return exterior, holes


def select_lod_containers(member: etree._Element) -> list[etree._Element]:
    """Return the geometry container elements for the highest LOD present in a
    building. LOD2 is preferred over LOD1; an empty list means no usable geometry."""
    lod2: list[etree._Element] = []
    lod1: list[etree._Element] = []
    for element in member.iter():
        name = local_name(element.tag)
        if name in LOD2_CONTAINERS:
            lod2.append(element)
        elif name in LOD1_CONTAINERS:
            lod1.append(element)
    if lod2:
        return lod2
    return lod1


def clear_processed(element: etree._Element) -> None:
    """Free an iterparse element and its already-processed previous siblings so the
    document is not retained in memory while streaming large files."""
    element.clear()
    parent = element.getparent()
    if parent is None:
        return
    while element.getprevious() is not None:
        del parent[0]


def extract_buildings(
    stream: IO[bytes],
) -> tuple[list[Vector3], list[tuple[list[int], list[list[int]]]]]:
    """Stream a building GML and return (vertices, polygons).

    vertices are geodetic (lat, lon, height); polygons reference global vertex
    indices as (exterior, holes).
    """
    vertices: list[Vector3] = []
    vertex_index: dict[Vector3, int] = {}
    polygons: list[tuple[list[int], list[list[int]]]] = []
    context = etree.iterparse(
        stream, events=("end",), tag=CORE_MEMBER, huge_tree=True
    )
    for _, member in context:
        # Dedup by the stable gml:id string. lxml element identity (id()) is unsafe
        # here: proxies are created on demand and their addresses are reused after
        # GC, which would intermittently drop polygons.
        seen: set[str] = set()
        for container in select_lod_containers(member):
            for polygon in container.iter(GML_POLYGON):
                polygon_id = polygon.get(GML_ID)
                if polygon_id is not None:
                    if polygon_id in seen:
                        continue
                    seen.add(polygon_id)
                result = read_polygon(polygon, vertices, vertex_index)
                if result is not None:
                    polygons.append(result)
        clear_processed(member)
    return vertices, polygons


def extract_terrain(stream: IO[bytes]) -> tuple[np.ndarray, np.ndarray]:
    """Stream a DEM TIN GML and return (vertices, faces).

    The TIN is already triangulated: each gml:Triangle contributes three vertices.
    Vertices are geodetic (lat, lon, height); faces index them in triangle order
    and are welded downstream.
    """
    coords = array.array("d")
    context = etree.iterparse(
        stream, events=("end",), tag=GML_TRIANGLE, huge_tree=True
    )
    for _, triangle in context:
        pos = triangle.find(f".//{GML_POSLIST}")
        if pos is not None and pos.text:
            values = np.fromstring(pos.text, sep=" ")
            if values.size >= 9:
                coords.frombytes(values[:9].tobytes())
        clear_processed(triangle)
    vertices = np.frombuffer(coords, dtype=np.float64).reshape(-1, 3)
    faces = np.arange(vertices.shape[0], dtype=np.int64).reshape(-1, 3)
    return vertices, faces


def read_envelope(stream: IO[bytes]) -> tuple[np.ndarray, np.ndarray] | None:
    """Read the first gml:Envelope (the CityModel bounds) as (lower, upper)
    corners in geodetic coordinates. Returns None if absent."""
    context = etree.iterparse(
        stream, events=("end",), tag=GML_ENVELOPE, huge_tree=True
    )
    for _, envelope in context:
        lower = envelope.find(GML_LOWER)
        upper = envelope.find(GML_UPPER)
        if lower is not None and lower.text and upper is not None and upper.text:
            low = np.fromstring(lower.text, sep=" ")
            high = np.fromstring(upper.text, sep=" ")
            if low.size >= 3 and high.size >= 3:
                return low[:3], high[:3]
        clear_processed(envelope)
    return None


# --- mesh assembly and export ---


def buildings_to_mesh(
    vertices: list[Vector3],
    polygons: list[tuple[list[int], list[list[int]]]],
    origin: Vector3,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Transform building vertices to the local ENU frame (Z-up) and triangulate
    every polygon. The output axis is applied later, per format, at export.
    Returns (vertices, faces) or None when nothing usable remains."""
    if not vertices or not polygons:
        return None
    local = geodetic_to_enu(np.array(vertices, dtype=np.float64), origin)
    faces: list[np.ndarray] = []
    for exterior, holes in polygons:
        mapping = np.array(exterior + [index for hole in holes for index in hole], dtype=np.int64)
        exterior_points = local[exterior]
        hole_points = [local[hole] for hole in holes]
        triangles = triangulate_polygon(exterior_points, hole_points)
        if triangles.size:
            faces.append(mapping[triangles])
    if not faces:
        return None
    return local, np.vstack(faces)


def terrain_to_mesh(
    vertices: np.ndarray, faces: np.ndarray, origin: Vector3
) -> tuple[np.ndarray, np.ndarray] | None:
    """Transform terrain vertices to the local ENU frame (Z-up). Faces are
    unchanged; the output axis is applied later, per format, at export."""
    if vertices.shape[0] == 0 or faces.shape[0] == 0:
        return None
    local = geodetic_to_enu(vertices, origin)
    return local, faces


def weld_mesh(vertices: np.ndarray, faces: np.ndarray) -> trimesh.Trimesh:
    """Build a mesh and weld duplicate vertices. Face count is unchanged; only
    coincident vertices are merged."""
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.merge_vertices()
    return mesh


def axis_for_format(extension: str, up: str) -> str:
    """Output up axis for a format. glTF/GLB is spec-locked to Y-up and every
    importer converts it to the target application's axis, so GLB is always Y-up
    regardless of --up; OBJ has no up-axis convention, so it honors --up."""
    if extension.lower() == "glb":
        return "y"
    return up


def export_oriented(mesh: trimesh.Trimesh, out_path: Path, up: str) -> None:
    """Orient a Z-up ENU mesh to the format's axis and export it. The extension
    selects the format (.glb or .obj)."""
    axis = axis_for_format(out_path.suffix.lstrip("."), up)
    oriented = trimesh.Trimesh(
        vertices=apply_up_axis(mesh.vertices, axis), faces=mesh.faces, process=False
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    oriented.export(out_path)


# --- input resolution ---


def theme_pattern(themes: list[str]) -> re.Pattern[str]:
    """Regex matching udx/<theme>/<name>.gml entry or file paths."""
    alternation = "|".join(re.escape(theme) for theme in themes)
    return re.compile(rf"(^|/)udx/({alternation})/[^/]+\.gml$", re.IGNORECASE)


def theme_of_single(path: Path, themes: list[str]) -> str | None:
    """Infer the theme of a lone .gml file from its parent directory or filename."""
    if path.parent.name in themes:
        return path.parent.name
    for theme in themes:
        if f"_{theme}_" in path.name:
            return theme
    return None


def resolve_sources(input_path: Path, themes: list[str]) -> list[Source]:
    """Enumerate convertible GML sources from a .zip, a directory, or a .gml file."""
    pattern = theme_pattern(themes)
    if input_path.suffix.lower() == ".zip":
        archive = zipfile.ZipFile(input_path)
        sources: list[Source] = []
        for entry in archive.namelist():
            match = pattern.search(entry)
            if match is None:
                continue
            theme = match.group(2).lower()
            sources.append(
                Source(name=entry, theme=theme, open_stream=lambda entry=entry: archive.open(entry))
            )
        return sources
    if input_path.is_dir():
        sources = []
        for path in sorted(input_path.rglob("*.gml")):
            match = pattern.search(path.as_posix())
            if match is None:
                continue
            theme = match.group(2).lower()
            sources.append(
                Source(name=path.name, theme=theme, open_stream=lambda path=path: path.open("rb"))
            )
        return sources
    if input_path.suffix.lower() == ".gml":
        theme = theme_of_single(input_path, themes)
        if theme is None:
            return []
        return [Source(name=input_path.name, theme=theme, open_stream=lambda: input_path.open("rb"))]
    return []


def compute_dataset_origin(sources: list[Source]) -> Vector3:
    """Compute a shared tangent-plane origin from the union of all source envelopes."""
    lowers: list[np.ndarray] = []
    uppers: list[np.ndarray] = []
    for source in sources:
        with source.open_stream() as stream:
            envelope = read_envelope(stream)
        if envelope is not None:
            lowers.append(envelope[0])
            uppers.append(envelope[1])
    if not lowers:
        raise ValueError("no gml:Envelope found in any source; cannot determine origin")
    low = np.min(np.array(lowers), axis=0)
    high = np.max(np.array(uppers), axis=0)
    midpoint = (low + high) / 2.0
    return (float(midpoint[0]), float(midpoint[1]), float(midpoint[2]))


def source_origin(source: Source) -> Vector3 | None:
    """Read a single source's tangent-plane origin from its envelope."""
    with source.open_stream() as stream:
        envelope = read_envelope(stream)
    if envelope is None:
        return None
    midpoint = (envelope[0] + envelope[1]) / 2.0
    return (float(midpoint[0]), float(midpoint[1]), float(midpoint[2]))


# --- driver ---


def output_paths(out_dir: Path, source: Source, fmt: str) -> list[Path]:
    """Output file path(s) for a source, grouped by theme under the output dir."""
    stem = Path(source.name).stem
    extensions = ["glb", "obj"] if fmt == "both" else [fmt]
    return [out_dir / source.theme / f"{stem}.{extension}" for extension in extensions]


def process_source(source: Source, origin: Vector3) -> tuple[np.ndarray, np.ndarray] | None:
    """Parse and build a Z-up ENU mesh for one source about the given origin."""
    with source.open_stream() as stream:
        if source.theme == "bldg":
            vertices, polygons = extract_buildings(stream)
            return buildings_to_mesh(vertices, polygons, origin)
        vertices, faces = extract_terrain(stream)
        return terrain_to_mesh(vertices, faces, origin)


def convert(input_path: Path, out_dir: Path, fmt: str, up: str, recenter: str, themes: list[str]) -> None:
    """Convert every matching source under input_path and write meshes plus a
    run-level metadata sidecar."""
    sources = resolve_sources(input_path, themes)
    if not sources:
        print(f"no matching GML sources under {input_path}", file=sys.stderr)
        return

    dataset_origin: Vector3 | None = None
    if recenter == "dataset":
        dataset_origin = compute_dataset_origin(sources)
        print(f"dataset origin (lat, lon, h): {dataset_origin}", file=sys.stderr)

    records: list[dict[str, object]] = []
    for source in sources:
        if recenter == "dataset":
            origin = dataset_origin
        else:
            origin = source_origin(source)
        if origin is None:
            print(f"skip {source.name}: no envelope for origin", file=sys.stderr)
            continue

        mesh = process_source(source, origin)
        if mesh is None:
            print(f"skip {source.name}: no geometry", file=sys.stderr)
            continue

        vertices, faces = mesh
        welded = weld_mesh(vertices, faces)
        for out_path in output_paths(out_dir, source, fmt):
            export_oriented(welded, out_path, up)
        print(f"wrote {source.name}: {len(welded.faces)} triangles", file=sys.stderr)
        records.append(
            {
                "name": source.name,
                "theme": source.theme,
                "origin_lat_lon_h": list(origin),
                "vertices": int(len(welded.vertices)),
                "faces": int(len(welded.faces)),
            }
        )

    metadata = {
        "crs": "EPSG:6697",
        "up_obj": up,
        "up_glb": "y",
        "recenter": recenter,
        "dataset_origin_lat_lon_h": list(dataset_origin) if dataset_origin is not None else None,
        "outputs": records,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "citygml2mesh.meta.json").write_text(json.dumps(metadata, indent=2))
    print(f"converted {len(records)} source(s) -> {out_dir}", file=sys.stderr)


# --- pipeline ---


VALID_FORMATS = {"glb", "obj", "both"}
VALID_UP = {"z", "y"}
VALID_RECENTER = {"dataset", "file"}

# Fallbacks for any setting a job (and the pipeline defaults) leave unspecified.
# These mirror the single-file CLI defaults so both entry points behave alike.
DEFAULT_SETTINGS: dict[str, str] = {
    "output": "out",
    "format": "glb",
    "up": "z",
    "recenter": "dataset",
    "themes": "bldg,dem",
}


@dataclass
class PipelineJob:
    """One resolved conversion: the argument set for a single convert() call."""

    input: Path
    output: Path
    format: str
    up: str
    recenter: str
    themes: list[str]


def parse_themes(value: object) -> list[str]:
    """Parse a themes specification into theme names.

    Accepts a comma-separated string (the CLI form) or a JSON list (the pipeline
    form). Blank entries are dropped.
    """
    if isinstance(value, list):
        raw: Sequence[object] = value
    else:
        raw = str(value).split(",")
    themes: list[str] = []
    for theme in raw:
        cleaned = str(theme).strip()
        if cleaned:
            themes.append(cleaned)
    return themes


def resolve_relative(path_value: object, base_dir: Path) -> Path:
    """Resolve a pipeline path against base_dir. Absolute paths pass through, so a
    pipeline can mix portable relative paths with fixed absolute ones."""
    path = Path(str(path_value))
    if path.is_absolute():
        return path
    return base_dir / path


def resolve_job(entry: object, defaults: Mapping[str, object], base_dir: Path, number: int) -> PipelineJob:
    """Merge one job entry over the pipeline defaults and validate it.

    entry: a job object from the 'jobs' array; must contain 'input'.
    defaults: shared settings applied to every job before its own overrides.
    base_dir: directory the pipeline file lives in, for resolving relative paths.
    number: 1-based job position, used only in error messages.
    Raises ValueError/TypeError with a job-scoped message on malformed input.
    """
    if not isinstance(entry, dict):
        raise TypeError(f"job {number} must be a JSON object, got {type(entry).__name__}")
    if "input" not in entry:
        raise ValueError(f"job {number} is missing required 'input'")
    merged: dict[str, object] = {**DEFAULT_SETTINGS, **defaults, **entry}

    fmt = str(merged["format"])
    if fmt not in VALID_FORMATS:
        raise ValueError(f"job {number} has invalid format {fmt!r}; expected one of {sorted(VALID_FORMATS)}")
    up = str(merged["up"])
    if up not in VALID_UP:
        raise ValueError(f"job {number} has invalid up {up!r}; expected one of {sorted(VALID_UP)}")
    recenter = str(merged["recenter"])
    if recenter not in VALID_RECENTER:
        raise ValueError(f"job {number} has invalid recenter {recenter!r}; expected one of {sorted(VALID_RECENTER)}")

    return PipelineJob(
        input=resolve_relative(merged["input"], base_dir),
        output=resolve_relative(merged["output"], base_dir),
        format=fmt,
        up=up,
        recenter=recenter,
        themes=parse_themes(merged["themes"]),
    )


def run_pipeline(pipeline_path: Path) -> None:
    """Run every job in a JSON pipeline file.

    The file is an object with an optional 'defaults' object and a 'jobs' array;
    each job is one convert() call with per-job overrides. A malformed or failing
    job is reported and skipped so one bad dataset never aborts the batch.
    """
    try:
        text = pipeline_path.read_text()
    except FileNotFoundError:
        print(f"pipeline file not found: {pipeline_path}", file=sys.stderr)
        return
    try:
        config = json.loads(text)
    except json.JSONDecodeError as error:
        print(f"pipeline {pipeline_path} is not valid JSON: {error}", file=sys.stderr)
        return
    if not isinstance(config, dict):
        print(f"pipeline {pipeline_path} must be a JSON object with a 'jobs' array", file=sys.stderr)
        return

    defaults = config.get("defaults", {})
    jobs = config.get("jobs", [])
    if not isinstance(defaults, dict):
        print(f"pipeline {pipeline_path}: 'defaults' must be an object", file=sys.stderr)
        return
    if not isinstance(jobs, list) or not jobs:
        print(f"pipeline {pipeline_path}: 'jobs' must be a non-empty array", file=sys.stderr)
        return

    base_dir = pipeline_path.resolve().parent
    succeeded = 0
    for index, entry in enumerate(jobs):
        number = index + 1
        try:
            job = resolve_job(entry, defaults, base_dir, number)
        except (ValueError, TypeError) as error:
            print(f"skip {error}", file=sys.stderr)
            continue
        print(f"[job {number}/{len(jobs)}] {job.input} -> {job.output}", file=sys.stderr)
        try:
            convert(job.input, job.output, job.format, job.up, job.recenter, job.themes)
            succeeded += 1
        except Exception as error:
            # Per-job isolation boundary: one bad dataset must not abort the batch.
            print(f"job {number} failed for {job.input}: {error!r}", file=sys.stderr)
    print(f"pipeline complete: {succeeded}/{len(jobs)} job(s) succeeded", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PLATEAU CityGML to OBJ/GLB meshes.")
    parser.add_argument(
        "input",
        type=Path,
        nargs="?",
        help="PLATEAU .zip, dataset directory, or .gml file (omit when using --pipeline)",
    )
    parser.add_argument("-o", "--output", type=Path, default=Path("out"), help="output directory")
    parser.add_argument("--format", choices=["glb", "obj", "both"], default="glb")
    parser.add_argument(
        "--up",
        choices=["z", "y"],
        default="z",
        help="up axis for OBJ output; GLB is always glTF-standard Y-up",
    )
    parser.add_argument(
        "--recenter",
        choices=["dataset", "file"],
        default="dataset",
        help="share one origin across all files, or recenter each file independently",
    )
    parser.add_argument("--themes", default="bldg,dem", help="comma-separated themes to convert")
    parser.add_argument(
        "--pipeline",
        type=Path,
        default=None,
        help="run a batch of conversions from a JSON pipeline file; ignores the single-file arguments",
    )
    args = parser.parse_args()

    if args.pipeline is not None:
        if args.input is not None:
            parser.error("provide either an input path or --pipeline, not both")
        run_pipeline(args.pipeline)
        return

    if args.input is None:
        parser.error("input is required unless --pipeline is given")

    themes = parse_themes(args.themes)
    convert(args.input, args.output, args.format, args.up, args.recenter, themes)


if __name__ == "__main__":
    main()
