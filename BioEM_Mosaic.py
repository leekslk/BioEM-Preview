import os
import sys
import re
from java.io import File
from java.awt.geom import AffineTransform
from ij import IJ
from ini.trakem2 import Project
from ini.trakem2.display import Display, Patch, Layer
from java.util import HashSet
from ini.trakem2.imaging import Blending
from mpicbg.trakem2.align import Align, AlignTask, RigidTile2D, TranslationTile2D
from mpicbg.models import RigidModel2D, TranslationModel2D

TILE_PATTERN = re.compile(r"Tile[_-](\d+)[_-](\d+)[_-](\d+)", re.IGNORECASE)

# (1) Parse Arguments: "targetPath|projectName|rows|cols|mode"
raw_arg = getArgument()
if not raw_arg:
    print("BioEM_Mosaic: No arguments received.")
    sys.exit(0)

# re-written to handle incomplete mosaic/mosaic z-stack acquisitions, needs to be tested
parts = raw_arg.split("|")
target_dir = parts[0].replace("\\", "/")
project_name = parts[1]
rows = int(parts[2])
cols = int(parts[3])
mode = parts[4] if len(parts) >= 5 else "mosaic"

desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop", project_name).replace("\\", "/")
xml_path = os.path.join(desktop_dir, project_name + ".xml").replace("\\", "/")

if not os.path.exists(desktop_dir):
    os.makedirs(desktop_dir)

print("BioEM Mosaic Pipeline Starting... ...")
print("Project: " + project_name + " [" + mode + "]")
print("Grid Size: " + str(rows) + " rows x " + str(cols) + " cols")
print("Source: " + target_dir)

# (2) Decode files with TILE_PATTERN into 
files = [f for f in os.listdir(target_dir) if f.lower().endswith(('.tif', '.tiff'))]
if not files:
    print("ERROR: No .tif tiles found in " + target_dir)
    sys.exit(0)

tiles_by_z = {}
min_d1 = None
min_d2 = None
first_image_path = None

for f in files:
    m = TILE_PATTERN.search(f)
    if m:
        d1 = int(m.group(1))
        d2 = int(m.group(2))
        z  = int(m.group(3))
        
        full_path = os.path.join(target_dir, f).replace("\\", "/")
        if not first_image_path:
            first_image_path = full_path

        if z not in tiles_by_z:
            tiles_by_z[z] = {}
        tiles_by_z[z][(d1, d2)] = full_path

        min_d1 = d1 if min_d1 is None else min(min_d1, d1)
        min_d2 = d2 if min_d2 is None else min(min_d2, d2)

if not tiles_by_z:
    print("ERROR: Files did not match Tile_XXX-XXX-XXXXXX format.")
    sys.exit(0)

sorted_z_levels = sorted(tiles_by_z.keys())
print("Found " + str(len(sorted_z_levels)) + " Z-slice(s) to process.")

# (3) Compute offset using tile dimensions?
imp = IJ.openImage(first_image_path)
tile_w = imp.getWidth()
tile_h = imp.getHeight()
imp.close()

# 10% nominal overlap
step_x = tile_w * 0.90
step_y = tile_h * 0.90

# (4) Initialize TrakEM2 Project & Populate Layers
project = Project.newFSProject("blank", None, desktop_dir, True)
layerset = project.getRootLayerSet()

all_layers = ArrayList()
all_patches = []

for layer_idx, z in enumerate(sorted_z_levels):
    # Retrieve index 0 if it exists, otherwise create new slice layer with thickness=1.0
    if layer_idx < layerset.getLayers().size():
        layer = layerset.getLayers().get(layer_idx)
    else:
        try:
            layer = layerset.getLayer(float(layer_idx), 1.0, True)
        except Exception:
            layer = Layer(project, float(layer_idx), 1.0, layerset)
            
    all_layers.add(layer)
    
    layer_tiles = tiles_by_z[z]
    for (d1, d2), img_path in layer_tiles.items():
        # d1 = row, d2 = column; convert 1-based microscope indexing to 0-based canvas positions
        c = d2 - min_d2
        r = d1 - min_d1
        
        x_pos = float(c * step_x)
        y_pos = float(r * step_y)
        
        p = Patch.createPatch(project, img_path)
        if p:
            p.setAffineTransform(AffineTransform.getTranslateInstance(x_pos, y_pos))
            layer.add(p)
            all_patches.append(p)

print("Placed " + str(len(all_patches)) + " tiles across " + str(all_layers.size()) + " layer(s).")


# (5) 1st Pass: Intra-Layer 2D Montaging (Per-Slice Alignment)
print("Pass 1: Solving intra-layer 2D tile alignment...")
intra_params = Align.ParamOptimize()
intra_params.sift.minOctaveSize = 64
intra_params.sift.maxOctaveSize = 1024

# DEFAULT: Rigid-only
intra_params.desiredModelIndex = 1
# intra_params.desiredModelIndex = 0 # Uncomment for pure Translation-only

# Stitches each slice independently
AlignTask.montageLayers(intra_params, all_layers, False, False, False, False)

# (6) 2nd Pass: Consecutive Pairwise Inter-Layer Z-Alignment (Rigid)
if len(sorted_z_levels) > 1:
    print("Pass 2: Aligning layers sequentially across Z (Rigid)...")
    
    p = Align.Param()
    p.sift.minOctaveSize = 64
    p.sift.maxOctaveSize = 1024
    
    # Rigid: Corrects translation drift and rotation between consecutive cuts
    p.expectedModelIndex = 1
    p.desiredModelIndex = 1
    p.regularize = False
    p.minNumInliers = 8  # Stable threshold for adjacent serial sections

    # Strictly pairwise: Compare slice i ONLY to slice i-1
    for i in range(1, all_layers.size()):
        ref_layer = all_layers.get(i - 1)
        mov_layer = all_layers.get(i)
        
        # Reference pool: ONLY the immediate previous layer
        ref_tiles = ArrayList()
        for patch in ref_layer.getDisplayables(Patch):
            ref_tiles.add(RigidTile2D(patch))
            
        # Moving pool: The current layer to align
        mov_tiles = ArrayList()
        for patch in mov_layer.getDisplayables(Patch):
            mov_tiles.add(RigidTile2D(patch))
            
        print("Aligning Layer " + str(i) + " to Layer " + str(i - 1) + "...")
        try:
            Align.alignTileCollections(p, ref_tiles, mov_tiles)
        except Exception as err:
            print("Notice: Layer " + str(i) + " pair alignment fallback: " + str(err))
            
    print("Layer alignment complete.")

# (7) 3rd Pass: Contrast Homogenization
# removed contrast homogenization as I am uncertain how it is running/working rigth now.
print("Pass 3: Contrast homogenization skipped.")

# (8) Refresh View & Persist XML
display = Display.getFront(project)
if display:
    display.update(layerset.getLayers().get(0))
    display.getCanvas().repaint()

# Purge undo snapshots to prevent Java 21 compact string crash ([B cannot be cast to [C)
try:
    f_set = layerset.getClass().getDeclaredField("snapshots")
    f_set.setAccessible(True)
    sn_set = f_set.get(layerset)
    if sn_set is not None:
        sn_set.clear()
        f_set.set(layerset, None)
except Exception:
    pass

for lyr in layerset.getLayers():
    try:
        f_lyr = lyr.getClass().getDeclaredField("snapshots")
        f_lyr.setAccessible(True)
        sn_lyr = f_lyr.get(lyr)
        if sn_lyr is not None:
            sn_lyr.clear()
            f_lyr.set(lyr, None)
    except Exception:
        pass

print("Saving TrakEM2 project...")
try:
    project.saveAs(xml_path, True)
    print("--- BioEM Pipeline Finished Successfully: " + xml_path + " ---")
except Exception as e:
    print("Standard save notice: " + str(e))
    try:
        project.save()
        print("--- BioEM Pipeline Finished Successfully (via save) ---")
    except Exception as err:
        print("CRITICAL: Failed to save project XML: " + str(err))