import os
import sys
import re
import time
import math
import random
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape as xml_escape

import numpy as np
import torch
import tifffile
from scipy.sparse import lil_matrix
from scipy.sparse.linalg import lsqr

# Force line-buffered stdout so parent GUI/watcher apps receive progress immediately
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

TILE_PATTERN = re.compile(r"Tile[_-](\d+)[_-](\d+)[_-](\d+)", re.IGNORECASE)


def format_eta(seconds):
    seconds = max(0.0, float(seconds))
    if seconds < 60.0:
        return "%.1fs" % seconds
    mins, secs = divmod(int(round(seconds)), 60)
    if mins < 60:
        return "%dm %02ds" % (mins, secs)
    hrs, mins = divmod(mins, 60)
    return "%dh %02dm %02ds" % (hrs, mins, secs)


def find_fiji_executable(explicit_path=None):
    """Locates the standard Java 8 Fiji executable on Windows or macOS."""
    if explicit_path and os.path.exists(explicit_path):
        return explicit_path
    env_path = os.environ.get("FIJI_EXE")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates = [
        "C:/Fiji.app/ImageJ-win64.exe",
        "D:/Fiji.app/ImageJ-win64.exe",
        os.path.join(os.path.expanduser("~"), "Fiji.app", "ImageJ-win64.exe"),
        os.path.join(os.path.expanduser("~"), "Desktop", "Fiji.app", "ImageJ-win64.exe"),
        "/Applications/Fiji.app/Contents/MacOS/ImageJ-macosx",
        os.path.join(os.path.expanduser("~"), "Applications", "Fiji.app", "Contents", "MacOS", "ImageJ-macosx"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def launch_trakem2_in_fiji(fiji_path, xml_path):
    """Opens Fiji and loads the TrakEM2 XML project as an independent GUI process."""
    if not fiji_path or not os.path.exists(fiji_path):
        print("Notice: Fiji executable not found automatically. Open manually: " + xml_path, flush=True)
        return False

    print("Launching Fiji & TrakEM2 project: " + xml_path, flush=True)
    fiji_dir = os.path.dirname(os.path.abspath(fiji_path))
    kwargs = {"close_fds": True, "cwd": fiji_dir}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so Fiji stays open if applet exits
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    subprocess.Popen([fiji_path, xml_path], **kwargs)
    return True


# 
# (1) Cross-Platform Device Detection (Dual CUDA GPUs vs. macOS MPS / CPU)
# 
def detect_compute_devices():
    if torch.cuda.is_available() and torch.cuda.device_count() > 0:
        n = torch.cuda.device_count()
        devs = [torch.device("cuda:%d" % i) for i in range(n)]
        names = [torch.cuda.get_device_name(i) for i in range(n)]
        return devs, "CUDA (%d GPU(s): %s)" % (n, ", ".join(names))

    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        try:
            mps_dev = torch.device("mps")
            probe = torch.randn((16, 16), device=mps_dev, dtype=torch.float32)
            _ = torch.fft.irfft2(torch.fft.rfft2(probe), s=(16, 16))
            return [mps_dev], "Apple Silicon MPS (GPU)"
        except Exception:
            pass

    return [torch.device("cpu")], "CPU (%d threads, Apple Accelerate / MKL)" % torch.get_num_threads()


# ==============================================================================
# (2) PyTorch Bandpass FFT Phase Correlation Engine
# ==============================================================================
@torch.inference_mode()
def _torch_phase_correlate(strip_a_cpu, strip_b_cpu, device, downsample=1):
    a_np = np.ascontiguousarray(strip_a_cpu, dtype=np.float32)
    b_np = np.ascontiguousarray(strip_b_cpu, dtype=np.float32)

    if downsample > 1:
        a_np = a_np[::downsample, ::downsample]
        b_np = b_np[::downsample, ::downsample]

    h = min(a_np.shape[0], b_np.shape[0])
    w = min(a_np.shape[1], b_np.shape[1])
    if h < 16 or w < 16:
        return 0.0, 0.0, 0.0

    a = torch.from_numpy(a_np[:h, :w]).to(device=device, non_blocking=True)
    b = torch.from_numpy(b_np[:h, :w]).to(device=device, non_blocking=True)

    a = a - torch.mean(a)
    b = b - torch.mean(b)
    if float(torch.std(a).item()) < 1e-5 or float(torch.std(b).item()) < 1e-5:
        return 0.0, 0.0, 0.0

    wy = torch.hann_window(h, periodic=False, device=device, dtype=torch.float32)[:, None]
    wx = torch.hann_window(w, periodic=False, device=device, dtype=torch.float32)[None, :]
    win = wy * wx

    fa = torch.fft.rfft2(a * win)
    fb = torch.fft.rfft2(b * win)

    fy = torch.fft.fftfreq(h, device=device, dtype=torch.float32)[:, None]
    fx = torch.fft.rfftfreq(w, device=device, dtype=torch.float32)[None, :]
    freq_r = torch.sqrt(fy * fy + fx * fx)
    bandpass = torch.exp(-((freq_r - 0.12) ** 2) / (2.0 * (0.14 ** 2)))
    bandpass[0, 0] = 0.0

    cross = fa * torch.conj(fb)
    cps = (cross / (torch.abs(cross) + 1e-8)) * bandpass
    corr = torch.fft.irfft2(cps, s=(h, w))

    flat_idx = int(torch.argmax(corr).item())
    py, px = divmod(flat_idx, w)
    peak_val = float(corr[py, px].item())

    yy = (torch.arange(h, device=device)[:, None] - py + h // 2) % h - h // 2
    xx = (torch.arange(w, device=device)[None, :] - px + w // 2) % w - w // 2
    bg_vals = corr[(torch.abs(yy) > 5) | (torch.abs(xx) > 5)]
    bg_mean = float(torch.mean(bg_vals).item())
    bg_std = float(torch.std(bg_vals).item()) + 1e-8
    snr = (peak_val - bg_mean) / bg_std

    c_ym = float(corr[(py - 1) % h, px].item())
    c_yp = float(corr[(py + 1) % h, px].item())
    c_xm = float(corr[py, (px - 1) % w].item())
    c_xp = float(corr[py, (px + 1) % w].item())

    denom_y = 2.0 * (c_ym - 2.0 * peak_val + c_yp)
    denom_x = 2.0 * (c_xm - 2.0 * peak_val + c_xp)
    sub_y = max(-0.5, min(0.5, (c_ym - c_yp) / denom_y)) if abs(denom_y) > 1e-8 else 0.0
    sub_x = max(-0.5, min(0.5, (c_xm - c_xp) / denom_x)) if abs(denom_x) > 1e-8 else 0.0

    shift_y = (py - h if py >= h // 2 else py) + sub_y
    shift_x = (px - w if px >= w // 2 else px) + sub_x
    return float(shift_x * downsample), float(shift_y * downsample), snr


# 
# (3) Pass 1: Intra-Layer 2D Montaging
# 
def solve_2d_layer(layer_tiles, tile_w, tile_h, overlap_frac, devices, min_snr=6.0):
    coords = sorted(layer_tiles.keys())
    if not coords:
        return {}

    min_r = min(r for r, _ in coords)
    min_c = min(c for _, c in coords)
    step_x = tile_w * (1.0 - overlap_frac)
    step_y = tile_h * (1.0 - overlap_frac)

    nominal_pos = {
        (r, c): ((c - min_c) * step_x, (r - min_r) * step_y)
        for (r, c) in coords
    }
    if len(coords) == 1:
        return nominal_pos

    def _read_tile(rc):
        return rc, tifffile.imread(layer_tiles[rc])

    tile_imgs = {}
    with ThreadPoolExecutor(max_workers=8) as io_pool:
        for rc, img in io_pool.map(_read_tile, coords):
            tile_imgs[rc] = img

    strip_frac = min(0.35, overlap_frac * 1.5)
    strip_w = int(round(tile_w * strip_frac))
    strip_h = int(round(tile_h * strip_frac))

    pairs = []
    coord_set = set(coords)
    for (r, c) in coords:
        if (r, c + 1) in coord_set:
            ox_a = tile_w - strip_w
            pairs.append(((r, c), (r, c + 1), (ox_a, 0, strip_w, tile_h), (0, 0, strip_w, tile_h)))
        if (r + 1, c) in coord_set:
            oy_a = tile_h - strip_h
            pairs.append(((r, c), (r + 1, c), (0, oy_a, tile_w, strip_h), (0, 0, tile_w, strip_h)))

    if not pairs:
        return nominal_pos

    max_drift_px = max(tile_w, tile_h) * 0.08
    n_devs = len(devices)
    workers = n_devs * 2 if devices[0].type == "cuda" else 4

    def _match_pair(job):
        idx, (rc_a, rc_b, box_a, box_b) = job
        dev = devices[idx % n_devs]
        xa, ya, wa, ha = box_a
        xb, yb, wb, hb = box_b
        crop_a = tile_imgs[rc_a][ya:ya + ha, xa:xa + wa]
        crop_b = tile_imgs[rc_b][yb:yb + hb, xb:xb + wb]

        dx_strip, dy_strip, snr = _torch_phase_correlate(crop_a, crop_b, dev, downsample=2)
        rel_x = (xa - xb) - dx_strip
        rel_y = (ya - yb) - dy_strip

        nom_dx = nominal_pos[rc_b][0] - nominal_pos[rc_a][0]
        nom_dy = nominal_pos[rc_b][1] - nominal_pos[rc_a][1]
        if snr < min_snr or math.hypot(rel_x - nom_dx, rel_y - nom_dy) > max_drift_px:
            return None
        return (rc_a, rc_b, rel_x, rel_y, snr)

    valid_edges = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(_match_pair, enumerate(pairs)):
            if res is not None:
                valid_edges.append(res)

    if not valid_edges:
        return nominal_pos

    idx_map = {rc: i for i, rc in enumerate(coords)}
    n_tiles = len(coords)
    n_eqs = len(valid_edges) + n_tiles

    A = lil_matrix((n_eqs, n_tiles), dtype=np.float64)
    bx = np.zeros(n_eqs, dtype=np.float64)
    by = np.zeros(n_eqs, dtype=np.float64)

    row = 0
    for rc_a, rc_b, rel_x, rel_y, snr in valid_edges:
        w_edge = min(10.0, max(1.0, snr / min_snr))
        ia, ib = idx_map[rc_a], idx_map[rc_b]
        A[row, ib] = w_edge
        A[row, ia] = -w_edge
        bx[row] = w_edge * rel_x
        by[row] = w_edge * rel_y
        row += 1

    for rc, i in idx_map.items():
        w = 50.0 if i == 0 else 0.05
        A[row, i] = w
        bx[row] = w * nominal_pos[rc][0]
        by[row] = w * nominal_pos[rc][1]
        row += 1

    A_csr = A.tocsr()
    sol_x = lsqr(A_csr, bx, atol=1e-6, btol=1e-6)[0]
    sol_y = lsqr(A_csr, by, atol=1e-6, btol=1e-6)[0]

    return {rc: (float(sol_x[i]), float(sol_y[i])) for rc, i in idx_map.items()}


# 
# (4) Pass 2: Layer-as-Rigid-Unit Z-Alignment (2D Procrustes Fit)
# 
def fit_rigid_2d_procrustes(Q_pts, P_pts, outlier_thresh_px=35.0):
    n = len(Q_pts)
    if n == 0:
        return None

    if n >= 3:
        dxs = sorted([P_pts[i][0] - Q_pts[i][0] for i in range(n)])
        dys = sorted([P_pts[i][1] - Q_pts[i][1] for i in range(n)])
        med_dx, med_dy = dxs[n // 2], dys[n // 2]

        in_Q, in_P = [], []
        for i in range(n):
            err_x = (P_pts[i][0] - Q_pts[i][0]) - med_dx
            err_y = (P_pts[i][1] - Q_pts[i][1]) - med_dy
            if math.hypot(err_x, err_y) <= outlier_thresh_px:
                in_Q.append(Q_pts[i])
                in_P.append(P_pts[i])
        if in_Q:
            Q_pts, P_pts = in_Q, in_P
            n = len(Q_pts)

    qx_mean = sum(q[0] for q in Q_pts) / float(n)
    qy_mean = sum(q[1] for q in Q_pts) / float(n)
    px_mean = sum(p[0] for p in P_pts) / float(n)
    py_mean = sum(p[1] for p in P_pts) / float(n)

    num, den = 0.0, 0.0
    for i in range(n):
        ux = Q_pts[i][0] - qx_mean
        uy = Q_pts[i][1] - qy_mean
        vx = P_pts[i][0] - px_mean
        vy = P_pts[i][1] - py_mean
        num += (ux * vy - uy * vx)
        den += (ux * vx + uy * vy)

    theta = math.atan2(num, den)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    tx = px_mean - (cos_t * qx_mean - sin_t * qy_mean)
    ty = py_mean - (sin_t * qx_mean + cos_t * qy_mean)
    return theta, tx, ty, n


def align_z_pair_rigid(ref_tiles, ref_pos_2d, mov_tiles, mov_pos_2d, tile_w, tile_h, devices, min_snr=5.5):
    shared_rcs = sorted(set(ref_tiles.keys()) & set(mov_tiles.keys()))
    if not shared_rcs:
        return 0.0, 0.0, 0.0, 0

    margin_x = int(tile_w * 0.125)
    margin_y = int(tile_h * 0.125)
    cx_local = tile_w / 2.0
    cy_local = tile_h / 2.0
    n_devs = len(devices)
    workers = n_devs * 2 if devices[0].type == "cuda" else 4

    def _match_z_tile(job):
        idx, rc = job
        dev = devices[idx % n_devs]
        img_ref = tifffile.imread(ref_tiles[rc])[margin_y:tile_h - margin_y, margin_x:tile_w - margin_x]
        img_mov = tifffile.imread(mov_tiles[rc])[margin_y:tile_h - margin_y, margin_x:tile_w - margin_x]

        dx, dy, snr = _torch_phase_correlate(img_ref, img_mov, dev, downsample=4)
        if snr < min_snr or math.hypot(dx, dy) > max(tile_w, tile_h) * 0.25:
            return None

        rx, ry = ref_pos_2d[rc]
        mx, my = mov_pos_2d[rc]
        p_ref = (rx + cx_local, ry + cy_local)
        q_in_mov_frame = (mx + cx_local + dx, my + cy_local + dy)
        return q_in_mov_frame, p_ref

    Q_pts, P_pts = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(_match_z_tile, enumerate(shared_rcs)):
            if res is not None:
                Q_pts.append(res[0])
                P_pts.append(res[1])

    fit = fit_rigid_2d_procrustes(Q_pts, P_pts, outlier_thresh_px=35.0)
    if fit is None:
        return 0.0, 0.0, 0.0, 0
    return fit


# 
# (5) Direct Native TrakEM2 XML Writer (Java 8 Fiji Ready)
# 
def write_trakem2_xml(xml_path, layer_records, tile_w, tile_h, is_16bit):
    out_dir = os.path.dirname(xml_path)
    proj_title = os.path.splitext(os.path.basename(xml_path))[0]
    unuid = "%d.%d.%d" % (
        int(time.time() * 1000),
        random.randint(100000000, 999999999),
        random.randint(100000000, 999999999)
    )
    mipmaps_rel = "trakem2.%s/trakem2.mipmaps/" % unuid
    mipmaps_abs = os.path.join(out_dir, "trakem2.%s" % unuid, "trakem2.mipmaps")
    if not os.path.exists(mipmaps_abs):
        os.makedirs(mipmaps_abs)

    corners = [(0.0, 0.0), (float(tile_w), 0.0), (0.0, float(tile_h)), (float(tile_w), float(tile_h))]
    min_x, min_y = float("inf"), float("inf")
    max_x, max_y = float("-inf"), float("-inf")

    for rec in layer_records:
        for p in rec["patches"]:
            m00, m10, m01, m11, tx, ty = p["matrix"]
            for cx, cy in corners:
                wx = m00 * cx + m01 * cy + tx
                wy = m10 * cx + m11 * cy + ty
                min_x = min(min_x, wx)
                min_y = min(min_y, wy)
                max_x = max(max_x, wx)
                max_y = max(max_y, wy)

    if not math.isfinite(min_x):
        min_x, min_y, max_x, max_y = 0.0, 0.0, float(tile_w), float(tile_h)

    layer_w = int(math.ceil(max_x - min_x))
    layer_h = int(math.ceil(max_y - min_y))
    patch_type = "1" if is_16bit else "0"
    max_val = "65535.0" if is_16bit else "255.0"

    oid_counter = 3
    first_layer_id = 3

    with open(xml_path, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n')
        f.write('<!DOCTYPE trakem2_anything [\n')
        f.write('\t<!ELEMENT trakem2 (project,t2_layer_set,t2_display)>\n')
        f.write('\t<!ELEMENT project (anything)>\n')
        f.write('\t<!ATTLIST project id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project unuid NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project title NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project mipmaps_folder NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project storage_folder NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project mipmaps_format NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project image_resizing_mode NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_layer_set (t2_calibration,t2_layer*)>\n')
        f.write('\t<!ATTLIST t2_layer_set id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set title NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set layer_width NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set layer_height NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_calibration EMPTY>\n')
        f.write('\t<!ATTLIST t2_calibration pixelWidth NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration pixelHeight NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration pixelDepth NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration unit NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_layer (t2_patch*)>\n')
        f.write('\t<!ATTLIST t2_layer id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer title NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer z NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer thickness NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_patch EMPTY>\n')
        f.write('\t<!ATTLIST t2_patch oid NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch width NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch height NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch transform NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch title NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch type NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch file_path NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch o_width NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch o_height NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch min NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch max NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_display EMPTY>\n')
        f.write('\t<!ATTLIST t2_display id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display layer_id NMTOKEN #REQUIRED>\n')
        f.write(']>\n\n')

        f.write('<trakem2>\n')
        f.write('\t<project id="0" unuid="%s" title="%s" mipmaps_folder="%s" storage_folder="" mipmaps_format="4" image_resizing_mode="AreaDownsampling">\n' % (
            unuid, xml_escape(proj_title, {'"': '&quot;'}), xml_escape(mipmaps_rel, {'"': '&quot;'})))
        f.write('\t</project>\n')

        f.write('\t<t2_layer_set id="1" title="Top level" links="" layer_width="%d" layer_height="%d" rot_x="0.0" rot_y="0.0" rot_z="0.0" snapshots_quality="true" snapshots_mode="Full" color_cues="true" area_color_cues="true" avoid_color_cue_colors="false" n_layers_color_cue="0" paint_arrows="true" paint_tags="true" paint_edge_confidence_boxes="true" preloader_threads="8" preload_ahead="1">\n' % (
            layer_w, layer_h))
        f.write('\t\t<t2_calibration pixelWidth="1.0" pixelHeight="1.0" pixelDepth="1.0" xOrigin="0.0" yOrigin="0.0" zOrigin="0.0" info="" valueUnit="Gray Value" timeUnit="sec" unit="pixel" />\n')

        for idx, rec in enumerate(layer_records):
            layer_id = oid_counter
            oid_counter += 1
            if idx == 0:
                first_layer_id = layer_id

            f.write('\t\t<t2_layer id="%d" title="z_%06d" links="" z="%.1f" thickness="1.0" visibility="true">\n' % (
                layer_id, rec["z_orig"], float(idx)))

            for p in rec["patches"]:
                patch_id = oid_counter
                oid_counter += 1
                m00, m10, m01, m11, tx, ty = p["matrix"]
                tx_norm = tx - min_x
                ty_norm = ty - min_y
                mat_str = "matrix(%.6f,%.6f,%.6f,%.6f,%.3f,%.3f)" % (
                    m00, m10, m01, m11, tx_norm, ty_norm)
                fname = os.path.basename(p["path"])

                f.write('\t\t\t<t2_patch oid="%d" width="%.1f" height="%.1f" transform="%s" title="%s" links="" type="%s" file_path="%s" style="fill-opacity:1.0;stroke:#ffff00;" o_width="%d" o_height="%d" min="0.0" max="%s" mres="32" ct_id="-1" alpha_mask_id="-1" />\n' % (
                    patch_id, float(tile_w), float(tile_h), mat_str,
                    xml_escape(fname, {'"': '&quot;'}),
                    patch_type, xml_escape(p["path"], {'"': '&quot;'}), tile_w, tile_h, max_val))

            f.write('\t\t</t2_layer>\n')

        f.write('\t</t2_layer_set>\n')
        f.write('\t<t2_display id="%d" layer_id="%d" c_alphas="-1" c_alphas_state="-1" x="0" y="0" magnification="0.15" srcrect_x="0" srcrect_y="0" srcrect_width="%d" srcrect_height="%d" />\n' % (
            oid_counter, first_layer_id, layer_w, layer_h))
        f.write('</trakem2>\n')


# 
# (6) Main Entrypoint
# 
def run_pipeline(
    target_dir,
    project_name,
    rows,
    cols,
    mode="auto",
    overlap_pct=10.0,
    fiji_exe=None,
    open_fiji=True,
    output_dir=None,
    progress_callback=None,
):
    def _emit(stage, progress_frac, elapsed_s, eta_s, message):
        if progress_callback is not None:
            try:
                progress_callback({
                    "stage": stage,
                    "progress": max(0.0, min(1.0, float(progress_frac))),
                    "elapsed": elapsed_s,
                    "eta": eta_s,
                    "message": message,
                })
            except Exception:
                pass

    t_start = time.time()
    target_dir = target_dir.replace("\\", "/")
    out_dir = (
        output_dir.replace("\\", "/")
        if output_dir
        else os.path.join(os.path.expanduser("~"), "Desktop", project_name).replace("\\", "/")
    )
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    xml_path = os.path.join(out_dir, project_name + ".xml").replace("\\", "/")

    devices, backend_desc = detect_compute_devices()
    print("=================================================================", flush=True)
    print("BioEM PyTorch Aligner -> Native TrakEM2 XML", flush=True)
    print("Compute Backend: " + backend_desc, flush=True)
    print("Project: %s | Grid: %dx%d | Source: %s" % (project_name, rows, cols, target_dir), flush=True)
    print("=================================================================", flush=True)
    _emit("init", 0.02, 0.0, None, "Initializing %s..." % backend_desc)

    tile_files = []
    for root, _, dir_files in os.walk(target_dir):
        for fname in sorted(dir_files):
            if fname.lower().endswith((".tif", ".tiff")):
                tile_files.append(os.path.join(root, fname).replace("\\", "/"))
    if not tile_files:
        raise RuntimeError("No .tif files found in " + target_dir)

    tiles_by_z = {}
    first_path = None
    for full_p in tile_files:
        m = TILE_PATTERN.search(os.path.basename(full_p))
        if m:
            r, c, z = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if first_path is None:
                first_path = full_p
            tiles_by_z.setdefault(z, {})[(r, c)] = full_p

    if not tiles_by_z:
        raise RuntimeError("No files matched Tile_XXX-XXX-XXXXXX pattern.")

    with tifffile.TiffFile(first_path) as tf:
        page = tf.pages[0]
        tile_h, tile_w = page.shape[:2]
        is_16bit = (page.dtype == np.uint16)

    sorted_zs = sorted(tiles_by_z.keys())
    total_z = len(sorted_zs)
    is_spatial_mosaic = (rows * cols) > 1 or any(len(v) > 1 for v in tiles_by_z.values())
    overlap_frac = overlap_pct / 100.0

    print("Tiles: %d across %d Z-sections | Tile size: %dx%d (%s)" % (
        sum(len(v) for v in tiles_by_z.values()), total_z,
        tile_w, tile_h, "16-bit" if is_16bit else "8-bit"), flush=True)

    # Progress weights across passes (Pass 1 = 0..0.55, Pass 2 = 0.55..0.95, Pass 3 = 0.95..1.0)
    p1_span = 0.55 if (is_spatial_mosaic and total_z > 1) else (0.95 if is_spatial_mosaic else 0.0)
    p2_span = 0.95 - p1_span if total_z > 1 else 0.0

    # 
    # PASS 1: 2D Intra-Section Montaging
    # 
    pos_2d_by_z = {}
    if is_spatial_mosaic:
        t1 = time.time()
        print("\n[Pass 1/3] 2D Intra-Section Montaging (%d sections)..." % total_z, flush=True)
        _emit("pass1", 0.02, 0.0, None, "[Pass 1/3] Starting 2D Montaging (%d sections)..." % total_z)
        for idx, z in enumerate(sorted_zs):
            pos_2d_by_z[z] = solve_2d_layer(tiles_by_z[z], tile_w, tile_h, overlap_frac, devices)
            done = idx + 1
            elapsed = time.time() - t1
            eta = (elapsed / done) * (total_z - done)
            pct = (100.0 * done) / total_z
            msg = "[Pass 1/3] 2D Montage %d/%d (%.1f%%) | Elapsed: %s | Remaining: %s" % (
                done, total_z, pct, format_eta(elapsed), format_eta(eta)
            )
            print("  " + msg, flush=True)
            _emit("pass1", p1_span * (float(done) / total_z), elapsed, eta, msg)
        print("[Pass 1/3] Completed in %s." % format_eta(time.time() - t1), flush=True)
    else:
        print("\n[Pass 1/3] Skipped (single tile per Z-section).", flush=True)
        for z in sorted_zs:
            pos_2d_by_z[z] = {rc: (0.0, 0.0) for rc in tiles_by_z[z]}

    # 
    # PASS 2: Sequential Layer-as-Rigid-Unit Z-Alignment
    # 
    cumulative_rigid = {sorted_zs[0]: (0.0, 0.0, 0.0)}
    if total_z > 1:
        t2 = time.time()
        total_trans = total_z - 1
        print("\n[Pass 2/3] Layer-as-Rigid-Unit Z-Alignment (%d transitions)..." % total_trans, flush=True)
        _emit("pass2", p1_span, 0.0, None, "[Pass 2/3] Starting Z-Alignment (%d transitions)..." % total_trans)
        for i in range(1, total_z):
            z_prev = sorted_zs[i - 1]
            z_curr = sorted_zs[i]

            d_theta, d_tx, d_ty, n_inliers = align_z_pair_rigid(
                tiles_by_z[z_prev], pos_2d_by_z[z_prev],
                tiles_by_z[z_curr], pos_2d_by_z[z_curr],
                tile_w, tile_h, devices
            )

            prev_th, prev_tx, prev_ty = cumulative_rigid[z_prev]
            c_p, s_p = math.cos(prev_th), math.sin(prev_th)

            cum_th = prev_th + d_theta
            cum_tx = c_p * d_tx - s_p * d_ty + prev_tx
            cum_ty = s_p * d_tx + c_p * d_ty + prev_ty
            cumulative_rigid[z_curr] = (cum_th, cum_tx, cum_ty)

            elapsed = time.time() - t2
            eta = (elapsed / i) * (total_trans - i)
            pct = (100.0 * i) / total_trans
            msg = "[Pass 2/3] Z-Align %d/%d (%.1f%%, inliers=%d) | Elapsed: %s | Remaining: %s" % (
                i, total_trans, pct, n_inliers, format_eta(elapsed), format_eta(eta)
            )
            print("  " + msg, flush=True)
            _emit("pass2", p1_span + p2_span * (float(i) / total_trans), elapsed, eta, msg)
        print("[Pass 2/3] Completed in %s." % format_eta(time.time() - t2), flush=True)
    else:
        print("\n[Pass 2/3] Skipped (single Z-section).", flush=True)

    # 
    # PASS 3: Export Native TrakEM2 XML Project
    # 
    t3 = time.time()
    print("\n[Pass 3/3] Writing Native TrakEM2 Project XML...", flush=True)
    _emit("pass3", 0.97, time.time() - t_start, 0.0, "[Pass 3/3] Writing TrakEM2 XML project...")
    layer_records = []
    for z in sorted_zs:
        th, stx, sty = cumulative_rigid[z]
        cos_t, sin_t = math.cos(th), math.sin(th)
        patches = []
        for rc, (px, py) in sorted(pos_2d_by_z[z].items()):
            w_tx = cos_t * px - sin_t * py + stx
            w_ty = sin_t * px + cos_t * py + sty
            patches.append({
                "path": tiles_by_z[z][rc],
                "matrix": (cos_t, sin_t, -sin_t, cos_t, w_tx, w_ty)
            })
        layer_records.append({"z_orig": z, "patches": patches})

    write_trakem2_xml(xml_path, layer_records, tile_w, tile_h, is_16bit)
    print("[Pass 3/3] TrakEM2 XML written in %s." % format_eta(time.time() - t3), flush=True)

    total_time = time.time() - t_start
    print("\n=================================================================", flush=True)
    print("--- BioEM PyTorch Pipeline Completed in %s ---" % format_eta(total_time), flush=True)
    print("TrakEM2 Project Ready: " + xml_path, flush=True)
    print("=================================================================", flush=True)
    _emit("done", 1.0, total_time, 0.0, "Alignment complete in %s. Opening Fiji..." % format_eta(total_time))

    if open_fiji:
        resolved_fiji = find_fiji_executable(fiji_exe)
        launch_trakem2_in_fiji(resolved_fiji, xml_path)

    return xml_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BioEM Cross-Platform PyTorch Aligner")
    parser.add_argument("arg_string", nargs="?", help="Pipe-separated string: targetPath|projectName|rows|cols|mode")
    parser.add_argument("--fiji-exe", default=None, help="Path to Java 8 Fiji executable")
    parser.add_argument("--no-open", action="store_true", help="Do not launch Fiji automatically on completion")
    args = parser.parse_args()

    if not args.arg_string:
        print("Usage: python bioem_torch_align.py \"/path/to/tiles|proj_name|4|3|mosaic-z\"")
        sys.exit(1)

    parts = args.arg_string.split("|")
    run_pipeline(
        target_dir=parts[0],
        project_name=parts[1],
        rows=int(parts[2]),
        cols=int(parts[3]),
        mode=parts[4] if len(parts) >= 5 else "auto",
        fiji_exe=args.fiji_exe,
        open_fiji=not args.no_open
    )