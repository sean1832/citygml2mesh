# b3dm2glb

Convert PLATEAU CityGML 2.0 datasets into CAD-friendly meshes. The converter reads buildings and terrain, converts geodetic coordinates to a local metric frame, and writes OBJ or GLB output. Textures and semantic attributes are intentionally ignored.

## Features

- Supports PLATEAU `.zip` archives, dataset folders, and single `.gml` files
- Exports `.glb`, `.obj`, or both
- Handles building and DEM geometry
- Supports single-run and JSON pipeline batch processing

## Install

```bash
pip install -r requirements.txt
```

## Usage

Convert one dataset:

```bash
python citygml2mesh.py data/13104_shinjuku-ku_pref_2023_citygml_2_op.zip -o out/
```

Command options:

```text
usage: citygml2mesh.py [-h] [-o OUTPUT] [--format {glb,obj,both}] [--up {z,y}] [--recenter {dataset,file}] [--themes THEMES] [--pipeline PIPELINE] [input]

Convert PLATEAU CityGML to OBJ/GLB meshes.

positional arguments:
  input                 PLATEAU .zip, dataset directory, or .gml file (omit when using --pipeline)

options:
  -h, --help            show this help message and exit
  -o OUTPUT             output directory
  --format {glb,obj,both}
  --up {z,y}            up axis for OBJ output; GLB is always glTF-standard Y-up
  --recenter {dataset,file}
                        share one origin across all files, or recenter each file independently
  --themes THEMES       comma-separated themes to convert
  --pipeline PIPELINE   run a batch of conversions from a JSON pipeline file; ignores the single-file argument
```

Run a batch pipeline:

```bash
python citygml2mesh.py --pipeline pipeline.json
```

## Pipeline format

`pipeline.json` is a JSON object with an optional `defaults` block and a required `jobs` array. Each job is one conversion. Relative paths are resolved from the pipeline file directory.

See `pipeline.example.json` for a working example.

## Output

Each source is written under the output directory grouped by theme, for example:

```text
out/
  bldg/
    dataset_name.glb
  dem/
    dataset_name.glb
```

A `citygml2mesh.meta.json` file is also written to the output directory with the CRS, recentering mode, origins, and output counts.

## Notes

- Input coordinates are expected in EPSG:6697
- GLB is always written as Y-up, matching glTF conventions
- OBJ honors `--up`
