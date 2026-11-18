import os
import sys
import re
import time
import math
import json
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
def _tukey_window_1d(length, alpha, device):
    """Flat-top Tukey window (alpha=0 -> rectangular, alpha=1 -> Hann)."""
    if alpha <= 0.0 or length <= 2:
        return torch.ones(length, device=device, dtype=torch.float32)
    if alpha >= 1.0:
        return torch.hann_window(length, periodic=False, device=device, dtype=torch.float32)
    x = torch.linspace(0.0, 1.0, length, device=device, dtype=torch.float32)
    w = torch.ones(length, device=device, dtype=torch.float32)
    half_alpha = 0.5 * alpha
    left = x < half_alpha
    right = x > (1.0 - half_alpha)
    w[left] = 0.5 * (1.0 + torch.cos(math.pi * (x[left] / half_alpha - 1.0)))
    w[right] = 0.5 * (1.0 + torch.cos(math.pi * ((x[right] - 1.0) / half_alpha + 1.0)))
    return w


@torch.inference_mode()
def _torch_phase_correlate(strip_a_cpu, strip_b_cpu, device, downsample=1, linear_pad=False, tukey_alpha=0.20):
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

    std_a = float(torch.std(a).item())
    std_b = float(torch.std(b).item())
    if std_a < 1e-4 or std_b < 1e-4:
        return 0.0, 0.0, 0.0

    a = (a - torch.mean(a)) / std_a
    b = (b - torch.mean(b)) / std_b

    wy = _tukey_window_1d(h, tukey_alpha, device)[:, None]
    wx = _tukey_window_1d(w, tukey_alpha, device)[None, :]
    win = wy * wx
    a = a * win
    b = b * win

    fft_h = (2 * h) if linear_pad else h
    fft_w = (2 * w) if linear_pad else w

    fa = torch.fft.rfft2(a, s=(fft_h, fft_w))
    fb = torch.fft.rfft2(b, s=(fft_h, fft_w))

    fy = torch.fft.fftfreq(fft_h, device=device, dtype=torch.float32)[:, None]
    fx = torch.fft.rfftfreq(fft_w, device=device, dtype=torch.float32)[None, :]
    freq_r = torch.sqrt(fy * fy + fx * fx)
    # Suppress DC / slow illumination ramps and high-frequency Nyquist shot noise
    highpass = 1.0 - torch.exp(-((freq_r / 0.015) ** 2))
    lowpass = torch.exp(-((freq_r / 0.35) ** 2))
    bandpass = highpass * lowpass
    bandpass[0, 0] = 0.0

    cross = fa * torch.conj(fb)
    cps = (cross / (torch.abs(cross) + 1e-8)) * bandpass
    corr = torch.fft.irfft2(cps, s=(fft_h, fft_w))

    if linear_pad:
        # Restrict valid shifts in linear mode to at most 80% of strip size so edge overlap stays meaningful
        max_sy = max(4, int(h * 0.80))
        max_sx = max(4, int(w * 0.80))
        sy_grid = torch.arange(fft_h, device=device)
        sx_grid = torch.arange(fft_w, device=device)
        sy_signed = torch.where(sy_grid >= fft_h // 2, sy_grid - fft_h, sy_grid)
        sx_signed = torch.where(sx_grid >= fft_w // 2, sx_grid - fft_w, sx_grid)
        valid_mask = (torch.abs(sy_signed)[:, None] <= max_sy) & (torch.abs(sx_signed)[None, :] <= max_sx)
        corr_masked = corr.masked_fill(~valid_mask, float("-inf"))
        flat_idx = int(torch.argmax(corr_masked).item())
    else:
        flat_idx = int(torch.argmax(corr).item())

    py, px = divmod(flat_idx, fft_w)
    peak_val = float(corr[py, px].item())

    yy = (torch.arange(fft_h, device=device)[:, None] - py + fft_h // 2) % fft_h - fft_h // 2
    xx = (torch.arange(fft_w, device=device)[None, :] - px + fft_w // 2) % fft_w - fft_w // 2
    bg_region = corr[(torch.abs(yy) > 5) | (torch.abs(xx) > 5)]
    if linear_pad:
        bg_region = bg_region[bg_region != 0.0]
        if bg_region.numel() < 16:
            bg_region = corr
    bg_mean = float(torch.mean(bg_region).item())
    bg_std = float(torch.std(bg_region).item()) + 1e-8
    snr = (peak_val - bg_mean) / bg_std

    c_ym = float(corr[(py - 1) % fft_h, px].item())
    c_yp = float(corr[(py + 1) % fft_h, px].item())
    c_xm = float(corr[py, (px - 1) % fft_w].item())
    c_xp = float(corr[py, (px + 1) % fft_w].item())

    denom_y = 2.0 * (c_ym - 2.0 * peak_val + c_yp)
    denom_x = 2.0 * (c_xm - 2.0 * peak_val + c_xp)
    sub_y = max(-0.5, min(0.5, (c_ym - c_yp) / denom_y)) if abs(denom_y) > 1e-8 else 0.0
    sub_x = max(-0.5, min(0.5, (c_xm - c_xp) / denom_x)) if abs(denom_x) > 1e-8 else 0.0

    shift_y = (py - fft_h if py >= fft_h // 2 else py) + sub_y
    shift_x = (px - fft_w if px >= fft_w // 2 else px) + sub_x
    return float(shift_x * downsample), float(shift_y * downsample), snr


# 
# (3) Pass 1: Intra-Layer 2D Montaging (Auto-Overlap + Multi-Block 1x + IRLS)
# 
def _match_pair_coarse_to_fine(img_a, img_b, tile_w, tile_h, nom_dx, nom_dy, max_drift_px, dev, min_snr=5.5):
    """
    Two-stage registration for an adjacent tile pair (A -> B):
      Stage 1: Zero-padded linear FFT at downsample=2 around nominal overlap (wrap-free).
      Stage 2: Extract exact overlapping sub-rectangle at downsample=1 across up to 3 blocks
               along the seam and return the consensus shift.
    """
    is_horiz = abs(nom_dx) >= abs(nom_dy)
    max_dim = max(tile_w, tile_h)
    ds1 = 4 if max_dim >= 3072 else (2 if max_dim >= 1024 else 1)

    if is_horiz:
        expected_ov = max(16, int(round(tile_w - abs(nom_dx))))
        coarse_w = min(tile_w, max(expected_ov + int(tile_w * 0.12), int(tile_w * 0.24)))
        xa0 = tile_w - coarse_w
        xb0 = 0
        crop_a = img_a[:, xa0:tile_w]
        crop_b = img_b[:, xb0:coarse_w]
        dx_s1, dy_s1, snr1 = _torch_phase_correlate(crop_a, crop_b, dev, downsample=ds1, linear_pad=True)
        rel_x_c = (xa0 - xb0) + dx_s1
        rel_y_c = dy_s1
    else:
        expected_ov = max(16, int(round(tile_h - abs(nom_dy))))
        coarse_h = min(tile_h, max(expected_ov + int(tile_h * 0.12), int(tile_h * 0.24)))
        ya0 = tile_h - coarse_h
        yb0 = 0
        crop_a = img_a[ya0:tile_h, :]
        crop_b = img_b[yb0:coarse_h, :]
        dx_s1, dy_s1, snr1 = _torch_phase_correlate(crop_a, crop_b, dev, downsample=ds1, linear_pad=True)
        rel_x_c = dx_s1
        rel_y_c = (ya0 - yb0) + dy_s1

    if snr1 < (min_snr * 0.7) or math.hypot(rel_x_c - nom_dx, rel_y_c - nom_dy) > max_drift_px:
        return None

    # Stage 2: Exact overlapping sub-rectangle at full 1x resolution
    rx_i = int(round(rel_x_c))
    ry_i = int(round(rel_y_c))
    ix0 = max(0, rx_i)
    iy0 = max(0, ry_i)
    ix1 = min(tile_w, rx_i + tile_w)
    iy1 = min(tile_h, ry_i + tile_h)
    ov_w = ix1 - ix0
    ov_h = iy1 - iy0
    if ov_w < 12 or ov_h < 12:
        return None

    ov_a = img_a[iy0:iy1, ix0:ix1]
    ov_b = img_b[iy0 - ry_i:iy1 - ry_i, ix0 - rx_i:ix1 - rx_i]
    t_alpha = 0.12 if min(ov_w, ov_h) < 48 else 0.22

    # Split seam into up to 3 overlapping blocks along the long axis to reject local resin/charging regions
    n_blocks = 3 if (ov_h >= 240 if is_horiz else ov_w >= 240) else 1
    block_results = []
    if n_blocks == 1:
        dx2, dy2, snr2 = _torch_phase_correlate(ov_a, ov_b, dev, downsample=1, linear_pad=False, tukey_alpha=t_alpha)
        if snr2 >= min_snr and math.hypot(dx2, dy2) <= 12.0:
            block_results.append((rx_i + dx2, ry_i + dy2, snr2))
    else:
        for b_idx in range(n_blocks):
            if is_horiz:
                seg_h = int(ov_h * 0.45)
                s0 = int((ov_h - seg_h) * (b_idx / float(n_blocks - 1)))
                ba = ov_a[s0:s0 + seg_h, :]
                bb = ov_b[s0:s0 + seg_h, :]
            else:
                seg_w = int(ov_w * 0.45)
                s0 = int((ov_w - seg_w) * (b_idx / float(n_blocks - 1)))
                ba = ov_a[:, s0:s0 + seg_w]
                bb = ov_b[:, s0:s0 + seg_w]

            dx2, dy2, snr2 = _torch_phase_correlate(ba, bb, dev, downsample=1, linear_pad=False, tukey_alpha=t_alpha)
            if snr2 >= min_snr and math.hypot(dx2, dy2) <= 12.0:
                block_results.append((rx_i + dx2, ry_i + dy2, snr2))

    if not block_results:
        return None

    if len(block_results) >= 2:
        xs = sorted(r[0] for r in block_results)
        ys = sorted(r[1] for r in block_results)
        med_x = xs[len(xs) // 2]
        med_y = ys[len(ys) // 2]
        inliers = [r for r in block_results if math.hypot(r[0] - med_x, r[1] - med_y) <= 3.5]
        if not inliers:
            return None
        w_sum = sum(r[2] for r in inliers)
        final_x = sum(r[0] * r[2] for r in inliers) / w_sum
        final_y = sum(r[1] * r[2] for r in inliers) / w_sum
        best_snr = max(r[2] for r in inliers)
    else:
        # Single block only: require stronger SNR to guard against blank-resin false peaks
        if block_results[0][2] < (min_snr * 1.3):
            return None
        final_x, final_y, best_snr = block_results[0]

    if math.hypot(final_x - nom_dx, final_y - nom_dy) > max_drift_px:
        return None
    return (final_x, final_y, best_snr)


def estimate_grid_overlap(layer_tiles, tile_w, tile_h, default_overlap_frac, devices):
    """
    Probes central horizontal and vertical tile pairs to auto-detect actual
    X and Y tile overlap fractions (handles 1%..30% overlap automatically).
    """
    coords = sorted(layer_tiles.keys())
    if not coords:
        return default_overlap_frac, default_overlap_frac
    coord_set = set(coords)
    mid_r = sum(r for r, _ in coords) / float(len(coords))
    mid_c = sum(c for _, c in coords) / float(len(coords))

    h_pairs = sorted(
        [((r, c), (r, c + 1)) for (r, c) in coords if (r, c + 1) in coord_set],
        key=lambda pair: abs(pair[0][0] - mid_r) + abs(pair[0][1] - mid_c),
    )[:6]
    v_pairs = sorted(
        [((r, c), (r + 1, c)) for (r, c) in coords if (r + 1, c) in coord_set],
        key=lambda pair: abs(pair[0][0] - mid_r) + abs(pair[0][1] - mid_c),
    )[:6]
    if not h_pairs and not v_pairs:
        return default_overlap_frac, default_overlap_frac

    needed = set()
    for a, b in h_pairs + v_pairs:
        needed.add(a)
        needed.add(b)
    imgs = {rc: tifffile.imread(layer_tiles[rc]) for rc in needed}
    dev = devices[0]
    ds = 4 if max(tile_w, tile_h) >= 2048 else 2

    ov_x_samples = []
    strip_w = min(tile_w, max(64, int(tile_w * 0.30)))
    for a, b in h_pairs:
        xa0 = tile_w - strip_w
        dx, dy, snr = _torch_phase_correlate(imgs[a][:, xa0:tile_w], imgs[b][:, 0:strip_w], dev, downsample=ds, linear_pad=True)
        rel_x = xa0 + dx
        ov = (tile_w - rel_x) / float(tile_w)
        if snr >= 7.0 and 0.01 <= ov <= 0.30 and abs(dy) <= tile_h * 0.08:
            ov_x_samples.append(ov)

    ov_y_samples = []
    strip_h = min(tile_h, max(64, int(tile_h * 0.30)))
    for a, b in v_pairs:
        ya0 = tile_h - strip_h
        dx, dy, snr = _torch_phase_correlate(imgs[a][ya0:tile_h, :], imgs[b][0:strip_h, :], dev, downsample=ds, linear_pad=True)
        rel_y = ya0 + dy
        ov = (tile_h - rel_y) / float(tile_h)
        if snr >= 7.0 and 0.01 <= ov <= 0.30 and abs(dx) <= tile_w * 0.08:
            ov_y_samples.append(ov)

    ov_x = sorted(ov_x_samples)[len(ov_x_samples) // 2] if ov_x_samples else default_overlap_frac
    ov_y = sorted(ov_y_samples)[len(ov_y_samples) // 2] if ov_y_samples else default_overlap_frac
    return float(ov_x), float(ov_y)


def _is_connected(n_nodes, edges):
    if n_nodes <= 1:
        return True
    adj = [[] for _ in range(n_nodes)]
    for ia, ib in edges:
        adj[ia].append(ib)
        adj[ib].append(ia)
    seen = {0}
    stack = [0]
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return len(seen) == n_nodes


def solve_2d_layer(layer_tiles, tile_w, tile_h, overlap_frac, devices, min_snr=5.5):
    coords = sorted(layer_tiles.keys())
    if not coords:
        return {}

    if isinstance(overlap_frac, (tuple, list)):
        ov_x, ov_y = float(overlap_frac[0]), float(overlap_frac[1])
    else:
        ov_x = ov_y = float(overlap_frac)

    min_r = min(r for r, _ in coords)
    min_c = min(c for _, c in coords)
    step_x = tile_w * (1.0 - ov_x)
    step_y = tile_h * (1.0 - ov_y)

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

    pairs = []
    coord_set = set(coords)
    for (r, c) in coords:
        if (r, c + 1) in coord_set:
            pairs.append(((r, c), (r, c + 1)))
        if (r + 1, c) in coord_set:
            pairs.append(((r, c), (r + 1, c)))

    if not pairs:
        return nominal_pos

    max_drift_px = max(tile_w, tile_h) * 0.12
    n_devs = len(devices)
    workers = n_devs * 2 if devices[0].type == "cuda" else 4

    def _match_pair(job):
        idx, (rc_a, rc_b) = job
        dev = devices[idx % n_devs]
        nom_dx = nominal_pos[rc_b][0] - nominal_pos[rc_a][0]
        nom_dy = nominal_pos[rc_b][1] - nominal_pos[rc_a][1]
        res = _match_pair_coarse_to_fine(
            tile_imgs[rc_a], tile_imgs[rc_b], tile_w, tile_h, nom_dx, nom_dy, max_drift_px, dev, min_snr=min_snr
        )
        if res is None:
            return None
        rel_x, rel_y, snr = res
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

    def _solve_edges(edge_list):
        degrees = [0] * n_tiles
        for rc_a, rc_b, _, _, _ in edge_list:
            degrees[idx_map[rc_a]] += 1
            degrees[idx_map[rc_b]] += 1

        n_eqs = len(edge_list) + n_tiles
        A = lil_matrix((n_eqs, n_tiles), dtype=np.float64)
        bx = np.zeros(n_eqs, dtype=np.float64)
        by = np.zeros(n_eqs, dtype=np.float64)

        row = 0
        for rc_a, rc_b, rel_x, rel_y, snr in edge_list:
            w_edge = min(10.0, max(1.0, snr / min_snr))
            ia, ib = idx_map[rc_a], idx_map[rc_b]
            A[row, ib] = w_edge
            A[row, ia] = -w_edge
            bx[row] = w_edge * rel_x
            by[row] = w_edge * rel_y
            row += 1

        for rc, i in idx_map.items():
            # Pin tile 0; keep weak anchor only for disconnected tiles so nominal spring doesn't fight real seams
            w = 50.0 if i == 0 else (5.0 if degrees[i] == 0 else 0.01)
            A[row, i] = w
            bx[row] = w * nominal_pos[rc][0]
            by[row] = w * nominal_pos[rc][1]
            row += 1

        A_csr = A.tocsr()
        sx = lsqr(A_csr, bx, atol=1e-7, btol=1e-7)[0]
        sy = lsqr(A_csr, by, atol=1e-7, btol=1e-7)[0]
        return sx, sy

    sol_x, sol_y = _solve_edges(valid_edges)

    # IRLS outlier edge pruning: drop inconsistent edges (> 6 px residual) if graph connectivity permits
    active_edges = list(valid_edges)
    for _ in range(4):
        if len(active_edges) <= n_tiles - 1:
            break
        residuals = []
        for e_idx, (rc_a, rc_b, rel_x, rel_y, _) in enumerate(active_edges):
            ia, ib = idx_map[rc_a], idx_map[rc_b]
            err = math.hypot((sol_x[ib] - sol_x[ia]) - rel_x, (sol_y[ib] - sol_y[ia]) - rel_y)
            residuals.append((err, e_idx))
        worst_err, worst_idx = max(residuals, key=lambda t: t[0])
        if worst_err <= 6.0:
            break
        candidate = [e for k, e in enumerate(active_edges) if k != worst_idx]
        if _is_connected(n_tiles, [(idx_map[e[0]], idx_map[e[1]]) for e in candidate]):
            active_edges = candidate
            sol_x, sol_y = _solve_edges(active_edges)
        else:
            break

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
    ds = 4 if max(tile_w, tile_h) >= 2048 else (2 if max(tile_w, tile_h) >= 512 else 1)

    def _match_z_tile(job):
        idx, rc = job
        dev = devices[idx % n_devs]
        img_ref = tifffile.imread(ref_tiles[rc])[margin_y:tile_h - margin_y, margin_x:tile_w - margin_x]
        img_mov = tifffile.imread(mov_tiles[rc])[margin_y:tile_h - margin_y, margin_x:tile_w - margin_x]

        dx, dy, snr = _torch_phase_correlate(img_ref, img_mov, dev, downsample=ds)
        if snr < min_snr or math.hypot(dx, dy) > max(tile_w, tile_h) * 0.25:
            return None

        rx, ry = ref_pos_2d[rc]
        mx, my = mov_pos_2d[rc]
        # dx = x_ref - x_mov shifts mov onto ref: moving point q_mov lands at q_ref = (rx+cx+dx, ry+cy+dy)
        q_in_mov_frame = (mx + cx_local, my + cy_local)
        p_ref = (rx + cx_local + dx, ry + cy_local + dy)
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

    layer_w = max(1, int(math.ceil(max_x - min_x)))
    layer_h = max(1, int(math.ceil(max_y - min_y)))
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
        f.write('\t<!ATTLIST project preprocessor NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project mipmaps_folder NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project storage_folder NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project mipmaps_format NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project image_resizing_mode NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST project first_mipmap_level_saved NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT anything EMPTY>\n')
        f.write('\t<!ATTLIST anything id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST anything expanded NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_prop EMPTY>\n')
        f.write('\t<!ATTLIST t2_prop key NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_prop value NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_linked_prop EMPTY>\n')
        f.write('\t<!ATTLIST t2_linked_prop target_id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_linked_prop key NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_linked_prop value NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_annot EMPTY>\n')
        f.write('\t<!ELEMENT t2_layer (t2_patch,t2_label,t2_layer_set,t2_profile)>\n')
        f.write('\t<!ATTLIST t2_layer oid NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer thickness NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer z NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_layer_set (t2_prop,t2_linked_prop,t2_annot,t2_layer,t2_pipe,t2_ball,t2_area_list,t2_calibration,t2_stack,t2_treeline)>\n')
        f.write('\t<!ATTLIST t2_layer_set oid NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set layer_id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set transform NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set style NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set locked NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set visible NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set title NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set links NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set composite NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set layer_width NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set layer_height NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set rot_x NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set rot_y NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set rot_z NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set snapshots_quality NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set color_cues NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set area_color_cues NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set avoid_color_cue_colors NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set n_layers_color_cue NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set paint_arrows NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set paint_tags NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set paint_edge_confidence_boxes NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_layer_set preload_ahead NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_calibration EMPTY>\n')
        f.write('\t<!ATTLIST t2_calibration pixelWidth NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration pixelHeight NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration pixelDepth NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration xOrigin NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration yOrigin NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration zOrigin NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration info NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration valueUnit NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration timeUnit NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_calibration unit NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_filter EMPTY>\n')
        f.write('\t<!ELEMENT t2_patch (t2_prop,t2_linked_prop,t2_annot,ict_transform,ict_transform_list,t2_filter)>\n')
        f.write('\t<!ATTLIST t2_patch oid NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch layer_id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch transform NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch style NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch locked NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch visible NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch title NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch links NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch composite NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch file_path NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch original_path NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch type NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch false_color NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch ct NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch o_width NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch o_height NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch min NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch max NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch pps NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch mres NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch ct_id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_patch alpha_mask_id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ELEMENT t2_display EMPTY>\n')
        f.write('\t<!ATTLIST t2_display id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display layer_id NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display x NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display y NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display magnification NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display srcrect_x NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display srcrect_y NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display srcrect_width NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display srcrect_height NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display scroll_step NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display c_alphas NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display c_alphas_state NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_enabled NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_min_max_enabled NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_min NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_max NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_invert NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_clahe_enabled NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_clahe_block_size NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_clahe_histogram_bins NMTOKEN #REQUIRED>\n')
        f.write('\t<!ATTLIST t2_display filter_clahe_max_slope NMTOKEN #REQUIRED>\n')
        f.write('] >\n\n')

        f.write('<trakem2>\n')
        f.write('\t<project\n')
        f.write('\t\tid="0"\n')
        f.write('\t\ttitle="%s"\n' % xml_escape(proj_title, {'"': '&quot;'}))
        f.write('\t\tunuid="%s"\n' % unuid)
        f.write('\t\tmipmaps_folder="%s"\n' % xml_escape(mipmaps_rel, {'"': '&quot;'}))
        f.write('\t\tstorage_folder="./"\n')
        f.write('\t\tmipmaps_format="4"\n')
        f.write('\t\timage_resizing_mode="AreaDownsampling"\n')
        f.write('\t\tfirst_mipmap_level_saved="0"\n')
        f.write('\t>\n')
        f.write('\t</project>\n')

        f.write('\t<t2_layer_set\n')
        f.write('\t\toid="1"\n')
        f.write('\t\twidth="%.1f"\n' % float(layer_w))
        f.write('\t\theight="%.1f"\n' % float(layer_h))
        f.write('\t\ttransform="matrix(1.0,0.0,0.0,1.0,0.0,0.0)"\n')
        f.write('\t\ttitle="Top level"\n')
        f.write('\t\tlinks=""\n')
        f.write('\t\tlayer_width="%.1f"\n' % float(layer_w))
        f.write('\t\tlayer_height="%.1f"\n' % float(layer_h))
        f.write('\t\trot_x="0.0"\n')
        f.write('\t\trot_y="0.0"\n')
        f.write('\t\trot_z="0.0"\n')
        f.write('\t\tsnapshots_quality="true"\n')
        f.write('\t\tsnapshots_mode="Full"\n')
        f.write('\t\tcolor_cues="true"\n')
        f.write('\t\tarea_color_cues="true"\n')
        f.write('\t\tavoid_color_cue_colors="false"\n')
        f.write('\t\tn_layers_color_cue="0"\n')
        f.write('\t\tpaint_arrows="true"\n')
        f.write('\t\tpaint_tags="true"\n')
        f.write('\t\tpaint_edge_confidence_boxes="true"\n')
        f.write('\t\tprepaint="false"\n')
        f.write('\t\tpreload_ahead="1"\n')
        f.write('\t>\n')
        f.write('\t\t<t2_calibration\n')
        f.write('\t\t\tpixelWidth="1.0"\n')
        f.write('\t\t\tpixelHeight="1.0"\n')
        f.write('\t\t\tpixelDepth="1.0"\n')
        f.write('\t\t\txOrigin="0.0"\n')
        f.write('\t\t\tyOrigin="0.0"\n')
        f.write('\t\t\tzOrigin="0.0"\n')
        f.write('\t\t\tinfo=""\n')
        f.write('\t\t\tvalueUnit="Gray Value"\n')
        f.write('\t\t\ttimeUnit="sec"\n')
        f.write('\t\t\tunit="pixel"\n')
        f.write('\t\t/>\n')

        for idx, rec in enumerate(layer_records):
            layer_id = oid_counter
            oid_counter += 1
            if idx == 0:
                first_layer_id = layer_id

            f.write('\t\t<t2_layer oid="%d" thickness="1.0" z="%.1f" title="z_%06d">\n' % (
                layer_id, float(idx), rec["z_orig"]))

            for p in rec["patches"]:
                patch_id = oid_counter
                oid_counter += 1
                m00, m10, m01, m11, tx, ty = p["matrix"]
                tx_norm = tx - min_x
                ty_norm = ty - min_y
                mat_str = "matrix(%.6f,%.6f,%.6f,%.6f,%.3f,%.3f)" % (
                    m00, m10, m01, m11, tx_norm, ty_norm)
                fname = os.path.basename(p["path"])

                f.write(
                    '\t\t\t<t2_patch oid="%d" width="%.1f" height="%.1f" transform="%s" '
                    'title="%s" links="" type="%s" file_path="%s" style="fill-opacity:1.0;stroke:#ffff00;" '
                    'o_width="%d" o_height="%d" min="0.0" max="%s" mres="32" ct_id="0" alpha_mask_id="0" />\n' % (
                        patch_id, float(tile_w), float(tile_h), mat_str,
                        xml_escape(fname, {'"': '&quot;'}),
                        patch_type, xml_escape(p["path"], {'"': '&quot;'}),
                        tile_w, tile_h, max_val
                    )
                )

            f.write('\t\t</t2_layer>\n')

        f.write('\t</t2_layer_set>\n')
        f.write(
            '\t<t2_display id="%d" layer_id="%d" c_alphas="-1" c_alphas_state="-1" '
            'x="0" y="0" magnification="0.15" srcrect_x="0" srcrect_y="0" '
            'srcrect_width="%d" srcrect_height="%d" scroll_step="1" '
            'filter_enabled="false" filter_min_max_enabled="false" filter_min="0" filter_max="255" '
            'filter_invert="false" filter_clahe_enabled="false" filter_clahe_block_size="127" '
            'filter_clahe_histogram_bins="256" filter_clahe_max_slope="3.0" />\n' % (
                oid_counter, first_layer_id, layer_w, layer_h
            )
        )
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
        if mode == "z-stack" and (rows * cols) <= 1:
            def _nat_key(p):
                return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", os.path.basename(p))]
            for z_idx, full_p in enumerate(sorted(tile_files, key=_nat_key), start=1):
                if first_path is None:
                    first_path = full_p
                tiles_by_z[z_idx] = {(1, 1): full_p}
        else:
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
        probe_z = sorted_zs[total_z // 2]
        ov_x, ov_y = estimate_grid_overlap(tiles_by_z[probe_z], tile_w, tile_h, overlap_frac, devices)
        print("\n[Pass 1/3] Detected overlap: X=%.2f%%, Y=%.2f%%" % (ov_x * 100.0, ov_y * 100.0), flush=True)
        print("[Pass 1/3] 2D Intra-Section Montaging (%d sections)..." % total_z, flush=True)
        _emit("pass1", 0.02, 0.0, None, "[Pass 1/3] Starting 2D Montaging (ov X=%.1f%%, Y=%.1f%%)..." % (ov_x * 100.0, ov_y * 100.0))
        for idx, z in enumerate(sorted_zs):
            pos_2d_by_z[z] = solve_2d_layer(tiles_by_z[z], tile_w, tile_h, (ov_x, ov_y), devices)
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
    parser.add_argument("--overlap", type=float, default=10.0, help="Nominal tile overlap percentage")
    parser.add_argument("--output-dir", default=None, help="Output directory for TrakEM2 XML project")
    parser.add_argument("--no-open", action="store_true", help="Do not launch Fiji automatically on completion")
    parser.add_argument("--progress-json", action="store_true", help="Emit __BIOEM_PROGRESS__ JSON lines to stdout")
    args = parser.parse_args()

    if not args.arg_string:
        print("Usage: python bioem_torch_align.py \"/path/to/tiles|proj_name|4|3|mosaic-z\"")
        sys.exit(1)

    def _cli_progress(info):
        if args.progress_json:
            print("__BIOEM_PROGRESS__:" + json.dumps(info), flush=True)

    parts = args.arg_string.split("|")
    out_xml = run_pipeline(
        target_dir=parts[0],
        project_name=parts[1],
        rows=int(parts[2]),
        cols=int(parts[3]),
        mode=parts[4] if len(parts) >= 5 else "auto",
        overlap_pct=args.overlap,
        fiji_exe=args.fiji_exe,
        open_fiji=not args.no_open,
        output_dir=args.output_dir,
        progress_callback=_cli_progress if args.progress_json else None,
    )
    if args.progress_json:
        print("__BIOEM_RESULT__:" + json.dumps({"xml_path": out_xml}), flush=True)