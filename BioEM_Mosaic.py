import os
import sys
import re
import math
from java.io import File
from java.awt.geom import AffineTransform, Point2D # default transformation used strictly to place tiles in rough coordinates
from java.util import ArrayList, HashSet
from java.lang import System
from java.awt import GraphicsEnvironment
from ij import IJ
from ini.trakem2 import Project
from ini.trakem2.display import Display, Patch, Layer
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
imp = IJ.openImage(first_image_path) #this is IO-heavy, change to a hybrid-approach in the next iteration
tile_w = imp.getWidth() # could use a single patch as we don't image multiple "sizes"
tile_h = imp.getHeight() # the first image is representative to all the data (presumably)
imp.close()

# 10% nominal overlap
step_x = tile_w * 0.90
step_y = tile_h * 0.90

# (4) Initialize TrakEM2 Project & Populate Layers
# set GUI display to false during initialization (prevents premature GUI window & mid-alignment repaints):
project = Project.newFSProject("blank", None, desktop_dir, False)
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
            layerset.add(layer)  # Register in layerset so layerset.getLayers() includes it
            
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

# Retrieve Layers from the Active Project
layerset = project.getRootLayerSet()
all_layers = layerset.getLayers()

if all_layers.isEmpty():
    sys.exit("Error: 0 layers found in project after ingestion.")

def fit_rigid_2d_procrustes(P_pts, Q_pts, outlier_thresh_px=25.0):
    """
    Fits a single 2D rigid transform (rotation + translation) mapping P_pts -> Q_pts
    via centroid-centered Kabsch/Procrustes SVD, with median-displacement outlier rejection.
    Returns an AffineTransform in world coordinates, or None if no valid pairs exist.
    """
    n = len(P_pts)
    if n == 0:
        return None

    # (1) Outlier rejection via median displacement vector (when >= 3 tiles matched)
    if n >= 3:
        dxs = sorted([Q_pts[i][0] - P_pts[i][0] for i in range(n)])
        dys = sorted([Q_pts[i][1] - P_pts[i][1] for i in range(n)])
        med_dx = dxs[n // 2]
        med_dy = dys[n // 2]

        inlier_P, inlier_Q = [], []
        for i in range(n):
            err_x = (Q_pts[i][0] - P_pts[i][0]) - med_dx
            err_y = (Q_pts[i][1] - P_pts[i][1]) - med_dy
            if math.hypot(err_x, err_y) <= outlier_thresh_px:
                inlier_P.append(P_pts[i])
                inlier_Q.append(Q_pts[i])

        if len(inlier_P) >= 1:
            P_pts, Q_pts = inlier_P, inlier_Q
            n = len(P_pts)

    # (2) Compute centroids P_bar and Q_bar (eliminates (0, 0) lever-arm bias)
    px_mean = sum(p[0] for p in P_pts) / float(n)
    py_mean = sum(p[1] for p in P_pts) / float(n)
    qx_mean = sum(q[0] for q in Q_pts) / float(n)
    qy_mean = sum(q[1] for q in Q_pts) / float(n)

    # (3) Closed-form 2D Procrustes optimal rotation angle theta around centroids
    # If n == 1, num = den = 0 -> atan2(0, 0) = 0.0 (pure translation fallback)
    num = 0.0
    den = 0.0
    for i in range(n):
        ux = P_pts[i][0] - px_mean
        uy = P_pts[i][1] - py_mean
        vx = Q_pts[i][0] - qx_mean
        vy = Q_pts[i][1] - qy_mean
        num += (ux * vy - uy * vx)
        den += (ux * vx + uy * vy)

    theta = math.atan2(num, den)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    # (4) World-space translation: t = Q_bar - R * P_bar
    tx = qx_mean - (cos_t * px_mean - sin_t * py_mean)
    ty = qy_mean - (sin_t * px_mean + cos_t * py_mean)

    # AffineTransform(m00, m10, m01, m11, m02, m12)
    return AffineTransform(cos_t, sin_t, -sin_t, cos_t, tx, ty), theta, (qx_mean - px_mean), (qy_mean - py_mean), n
    # might be an over-kill but let's see if this works
#
# PASS 1: Stitch spatial tiles first
#
print("Pass 1: Montaging intra-layer spatial tiles (Rigid + Regularized)...")

intra_params = Align.ParamOptimize()
intra_params.sift.minOctaveSize = 64
intra_params.sift.maxOctaveSize = 1024
intra_params.expectedModelIndex = 0        # Baseline: Translation
intra_params.desiredModelIndex = 1         # Solved: Rigid (fixes slight stage rotation)
intra_params.regularizerModelIndex = 0     # Regularize toward Translation
intra_params.regularize = True
setattr(intra_params, 'lambda', 0.1)
intra_params.minNumInliers = 10

future = AlignTask.montageLayers(intra_params, all_layers, False, False, False, False)
if future is not None:
    future.get()

layerset.setMinimumDimensions()
print("Pass 1: 2D Montaging completed.")

#
# PASS 2: Sequential Layer-as-Rigid-Unit Z-Alignment
#
if all_layers.size() > 1:
    print("Pass 2: Aligning layers sequentially across Z (Layer-as-Rigid-Unit)...")

    z_param = Align.Param()
    z_param.sift.minOctaveSize = 64
    z_param.sift.maxOctaveSize = 1024
    z_param.expectedModelIndex = 0        # Baseline: Translation
    z_param.desiredModelIndex = 1         # Solved: Rigid
    z_param.regularizerModelIndex = 0     # Regularize toward Translation
    z_param.regularize = True
    setattr(z_param, 'lambda', 0.1)
    z_param.minNumInliers = 8

    for i in range(1, all_layers.size()):
        ref_layer = all_layers.get(i - 1)
        mov_layer = all_layers.get(i)

        ref_patches = ref_layer.getDisplayables(Patch)
        mov_patches = mov_layer.getDisplayables(Patch)

        # 1. Snapshot exact 2D montaged transforms before Z-alignment
        pre_transforms = [AffineTransform(p.getAffineTransform()) for p in mov_patches]

        ref_tiles = ArrayList([RigidTile2D(p) for p in ref_patches])
        mov_tiles = ArrayList([RigidTile2D(p) for p in mov_patches])

        try:
            Align.alignTileCollections(z_param, ref_tiles, mov_tiles)

            # 2. Extract tile-center correspondences P_i -> Q_i from RigidTile2D models
            P_pts = []
            Q_pts = []
            for idx, p in enumerate(mov_patches):
                pre_at = pre_transforms[idx]
                # Read solved transform directly from the tile's model
                post_at = mov_tiles.get(idx).createAffine()

                # Check if this tile actually matched/moved relative to pre_at
                m_pre = [0.0] * 6
                m_post = [0.0] * 6
                pre_at.getMatrix(m_pre)
                post_at.getMatrix(m_post)
                max_diff = max(abs(m_post[k] - m_pre[k]) for k in range(6))

                if max_diff > 1e-6:
                    cx = float(p.getOWidth()) / 2.0
                    cy = float(p.getOHeight()) / 2.0

                    pt_pre = Point2D.Double(cx, cy)
                    pt_post = Point2D.Double(cx, cy)
                    pre_at.transform(pt_pre, pt_pre)
                    post_at.transform(pt_post, pt_post)

                    P_pts.append((pt_pre.getX(), pt_pre.getY()))
                    Q_pts.append((pt_post.getX(), pt_post.getY()))

            # 3. Fit single centroid-centered rigid transform across all matched tile centers
            fit_result = fit_rigid_2d_procrustes(P_pts, Q_pts, outlier_thresh_px=25.0)

            if fit_result is not None:
                slice_at, theta, cent_dx, cent_dy, used_tiles = fit_result

                for idx, p in enumerate(mov_patches):
                    locked_at = AffineTransform(pre_transforms[idx])
                    locked_at.preConcatenate(slice_at)
                    p.setAffineTransform(locked_at)
                    p.updateBucket()

                print("  Layer %d -> %d locked rigid shift (%d/%d tiles): dx=%.1f, dy=%.1f, rot=%.3f deg" % (
                    i, i - 1, used_tiles, mov_patches.size(), cent_dx, cent_dy, math.degrees(theta)))
            else:
                for idx, p in enumerate(mov_patches):
                    p.setAffineTransform(pre_transforms[idx])
                    p.updateBucket()
                print("  Notice: No cross-layer consensus on Layer %d; preserving 2D montage." % i)
                
        except Exception as z_err:
            for idx, p in enumerate(mov_patches):
                p.setAffineTransform(pre_transforms[idx])
                p.updateBucket()
            print("  Notice: Layer %d Z-align fallback: %s" % (i, str(z_err)))

    # Normalize global canvas origin and rebuild all spatial index buckets
    layerset.setMinimumDimensions()
    layerset.recreateBuckets(True)
    print("Pass 2: Layer-as-Rigid-Unit Z-alignment completed.")

#
# PASS 3: Patch Homogenisation (Per-Layer), with-holding function for user choice or optimisation later
#
print("The Current Workflow Will NOT BE Homogenising Patches...")
# modfies pixel intensity vlaues, creates sharp 1-pixel boundaries, matching B&C across tiles, no ghosting
#for layer in all_layers:
#    patches = layer.getDisplayables(Patch)
#    if patches.size() > 1:
#        try:
#            AlignTask.homogenizePatches(patches)
#        except Exception as homog_err:
#            print("Notice: Homogenisation fallback on Z=" + str(layer.getZ()) + ": " + str(homog_err))
#
#print("Pass 3: Patch Homogenisation completed.")

# Project Persistence & Safe Save
print("Saving TrakEM2 project...")
try:
    project.saveAs(xml_path, True)
    print("--- BioEM Pipeline Finished Successfully: " + xml_path + " ---")
    
    if not GraphicsEnvironment.isHeadless():
        front_disp = Display.getFront(project)
        if front_disp is None:
            Display(project, all_layers.get(0))
        else:
            front_disp.setLayer(all_layers.get(0))
            Display.update(layerset)
    else:
        # Headless mode: free cached patch rasters from JVM heap
        project.getLoader().releaseAll()
        System.gc()
except Exception as save_err:
    print("Error saving project: " + str(save_err))