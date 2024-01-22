# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Rigid image tile stitching.

Stitching is done in two stages. First, a rough XY offset is estimated between
every pair of nearest neighbor (NN) image tiles. A spring-mass mesh of tiles is
then formed by representing every tile with a unit mass and connecting it
with a Hookean spring to each NN, with the rest spring length determined
by the estimated offset. This system is relaxed to establish an initial position
for every tile based on cross-correlation between tile overlaps.
"""
import time
from pathlib import Path
from typing import Mapping, Optional

import jax.numpy as jnp
import matplotlib.pyplot as plt
import numpy as np
import skimage.io
from scipy import ndimage

from sofima import flow_field
from sofima import mesh
from sofima import flow_utils

TileXY = tuple[int, int]
TileMap = Mapping[TileXY, np.ndarray]
MaskMap = dict[TileXY, np.ndarray]


def _estimate_offset(
    a: np.ndarray, b: np.ndarray, range_limit: float, mask: np.ndarray,
    filter_size: int = 10,
) -> tuple[list[float], float, np.ndarray[bool]]:
  """Estimates the global offset vector between images 'a' and 'b'."""
  # Mask areas with insufficient dynamic range.

  a_mask = (
      ndimage.maximum_filter(a, filter_size)
      - ndimage.minimum_filter(a, filter_size)
  ) < range_limit
  b_mask = (
      ndimage.maximum_filter(b, filter_size)
      - ndimage.minimum_filter(b, filter_size)
  ) < range_limit

  # c_mask = np.zeros_like(mask)
  # c_mask[:8] = True
  # c_mask[:20, 1000:1800] = True
  # c_mask[:, 1800:] = True
  # b_mask |= c_mask

  if mask.size != 0:
    b_mask |= mask

  # Compute coarse-shift vector
  mfc = flow_field.JAXMaskedXCorrWithStatsCalculator()
  xo, yo, _, pr = mfc.flow_field(
      a, b, pre_mask=a_mask, post_mask=b_mask, patch_size=a.shape, step=(1, 1)
  ).squeeze()
  return [xo, yo], abs(pr), b_mask


def _estimate_offset_horiz(
    overlap: int,
    left: np.ndarray,
    right: np.ndarray,
    range_limit: float,
    filter_size: int,
    mask: np.ndarray,
) -> tuple[list[float], float, np.ndarray[bool]]:
  return _estimate_offset(
      left[:, -overlap:], right[:, :overlap], range_limit, mask, filter_size,
  )


def _estimate_offset_vert(
    overlap: int,
    top: np.ndarray,
    bot: np.ndarray,
    range_limit: float,
    filter_size: int,
    mask: np.ndarray,
) -> tuple[list[float], float, np.ndarray[bool]]:
  return _estimate_offset(
      top[-overlap:, :], bot[:overlap, :], range_limit, mask, filter_size,
  )

def compute_coarse_offsets(
    yx_shape: TileXY,
    tile_map: TileMap,
    mask_map: MaskMap,
    overlaps_xy=((200, 300), (200, 300)),
    min_range=(10, 100, 0),
    min_overlap=160,
    filter_size=10,
    mask_top_edge=4,
    max_valid_offset=400
) -> tuple[np.ndarray, np.ndarray, MaskMap]:
  """Computes a coarse offset between every neighboring tile pair.

  Args:
    yx_shape: vertical and horizontal number of tiles
    tile_map: maps (x, y) tile coordinate to the tile image
    mask_map: maps (x, y) tile coordinate to custom binary mask of tile image
    overlaps_xy: pair of two overlap sequences to try, for NN tiles in the X and
      Y direction, respectively; these overlaps define the number of pixels in
      the given dimension used to compute the offset vector
    min_range: regions with dynamic range smaller than this value will be masked
      in flow estimation; dynamic range is computed within 'filter_size'^2
      patches centered at every pixel; this is useful to improve estimated
      offsets in areas with very low contrast (e.g. due to charging)
    min_overlap: minimum overlap required for the estimate to be considered
      valid
    filter_size: size of the filter to use when evaluating dynamic range
    mask_top_edge: default number of lines to be masked (from top of the image)
    max_valid_offset: limit valid range of coarse offsets to +- max_valid_offset

  Returns:
    two arrays of shape [2, 1] + yx_shape, where the dimensions are:
      0: computed XY offset (in pixels)
      1: ignored (z dim for compatibility with the mesh solver)
      2: Y position in the tile grid
      3: X position in the tile grid

    The arrays represent the computed offsets between:
      - tiles at (x, y) and (x + 1, y)
      - tiles at (x, y) and (x, y + 1)

    The offsets are computed with the 'post' ((x+1, y) or (x, y+1)) tile
    as the 'moving' image and the 'pre' (x,y) tile as the fixed one. Estimated
    offsets are set to inf if a value compatible with the specified criteria
    could not be obtained. Values that cannot be computed because of missing
    tiles are set to nan.
  """

  def _find_offset(estimate_fn, pre, post, overlaps, max_ortho_shift, axis, mask):
    def _is_valid_offset(offset, axis):
      return (
        abs(offset[1 - axis]) < max_ortho_shift
        and min_overlap <= abs(offset[axis]) < max_valid_offset
      )

    done = False

    mask_fin = mask
    for range_limit in min_range:
      if done:
        break
      max_idx = -1
      max_pr = 0
      estimates = []

      for overlap in overlaps:
        print(f"ov_size, range_limit: {overlap}, {range_limit}")
        mask_ov = mask[:overlap]
        offset, pr, mask_fin = estimate_fn(overlap, pre, post, range_limit,
                                           filter_size, mask_ov)
        offset[axis] -= overlap

        # If a single peak is found, terminate search.
        if pr == 0.0:
          done = True
          break

        estimates.append(offset)

        # Record the valid offset with maximum peak ratio.
        if pr > max_pr and _is_valid_offset(offset, axis):
          max_pr = pr
          max_idx = len(estimates) - 1

      if done:
        break

      min_diff = np.inf
      min_idx = 0
      for i, (off0, off1) in enumerate(zip(estimates, estimates[1:])):
        diff = np.abs(off1[axis] - off0[axis])
        if diff < min_diff and _is_valid_offset(off1, axis):
          min_diff = diff
          min_idx = i

      # If we found an offset with good consistency between two consecutive
      # estimates, perfer that.
      if min_diff < 20:
        offset = estimates[min_idx + 1]
        done = True
      # Otherwise prefer the offset with maximum peak ratio.
      elif max_idx >= 0:
        offset = estimates[max_idx]
        done = True

    if not done or abs(offset[axis]) < min_overlap:
      offset = np.inf, np.inf

    return offset, mask_fin

  conn_x = np.full((2, 1, yx_shape[0], yx_shape[1]), np.nan)

  for x in range(0, yx_shape[1] - 1):
    for y in range(0, yx_shape[0]):
      if not ((x, y) in tile_map and (x + 1, y) in tile_map):
        continue

      left = tile_map[(x, y)]
      right = tile_map[(x + 1, y)]
      mask_x = np.empty((0,))  # TODO implement usage of vertical mask
      conn_x[:, 0, y, x], _ = _find_offset(
          _estimate_offset_horiz,
          left,
          right,
          overlaps_xy[0],
          max(overlaps_xy[1]),
          0,
          mask_x,
      )

  conn_y = np.full((2, 1, yx_shape[0], yx_shape[1]), np.nan)
  for y in range(0, yx_shape[0] - 1):
    for x in range(0, yx_shape[1]):
      if not ((x, y) in tile_map and (x, y + 1) in tile_map):
        continue

      print(f'Computing conn_y : {x, y} -> {x, y + 1}')
      top = tile_map[(x, y)]
      bot = tile_map[(x, y + 1)]
      mask_y = mask_map[(x, y + 1)]

      # Compute custom smearing mask if not defined or loaded old mask shape
      # would not match all shapes of min_range masks (_estimate_offset)

      fn = (r"c:\Users\ganctoma\OneDrive - Friedrich Miescher Institute for Biomedical Research\Code\tools\EM_ALIGNMENT\dev\Smearing_masking\to_process\20230523_RoLi_IV_130558_run2_g0001_t0866_s01131\tmp.png"
            )

      if mask_y.size == 0 or np.shape(mask_y)[0] < max(overlaps_xy[0]):
        bot_crop = bot[:max(overlaps_xy[1])]
        mask_y = flow_utils.get_smearing_mask(
          bot_crop,
          mask_top_edge,
          path_plot=fn,
          plot=False
        )
        mask_map[(x, y + 1)] = mask_y

      conn_y[:, 0, y, x], mask_yy = _find_offset(
          _estimate_offset_vert,
          top,
          bot,
          overlaps_xy[1],
          max(overlaps_xy[0]),
          1,
          mask_y,
      )

      # Save final version of binary mask used in _estimate_offset
      mask_map[(x, y + 1)] = mask_yy

  return conn_x, conn_y, mask_map


# TODO(mjanusz): add type aliases for ndarrays
def interpolate_missing_offsets(
    conn: np.ndarray, axis: int, max_r: int = 4
) -> np.ndarray:
  """Estimates missing coarsse offsets.

  Missing offsets are indicated by the value 'inf'. This function
  attempts to replace them with finite values from nearest valid
  horizontal or vertical neighbors.

  Args:
    conn: [2, 1, y, x] coarse offset array; modified in place; see return value
      of compute_coarse_offsets for details
    axis: axis in the 'conn' array along to search for valid nearest neighbors
      (-1 for x, -2 for y)
    max_r: maximum distance along the search axis to search for a valid neighbor

  Returns:
    conn array; might still contain inf if these could not be replaced with
    valid values from neighbors within the search radius
  """

  if conn.ndim != 4:
    raise ValueError('conn array must have rank 4')

  missing = np.isinf(conn[0, 0, ...])
  if not np.any(missing):
    return conn

  for y, x in zip(*np.where(missing)):
    found = []
    point = np.array([0, 0, y, x])
    off = np.array([0, 0, 0, 0])
    for r in range(1, max_r):
      off[axis] = r
      lo = point - off
      hi = point + off

      if lo[axis] >= 0 and np.isfinite(conn[tuple(lo)]):
        lo = lo.tolist()
        lo[0] = slice(None)
        found.append(conn[tuple(lo)])
      if hi[axis] < conn.shape[axis] and np.isfinite(conn[tuple(hi)]):
        hi = hi.tolist()
        hi[0] = slice(None)
        found.append(conn[tuple(hi)])
      if found:
        break

    if found:
      conn[:, 0, y, x] = np.mean(found, axis=0)
  return conn


def elastic_tile_mesh(
    x: jnp.ndarray,
    cx: jnp.ndarray,
    cy: jnp.ndarray,
    k=None,
    stride=None,
    prefer_orig_order=False,
    links=None,
) -> jnp.ndarray:
  """Computes force on nodes of a 2d tile mesh.

  Unused arguments are defined for compatibility with the mesh solver.

  Args:
    x: [2, z, y, x] mesh where every node represents a tile
    cx: desired XY offsets between (x, y) and (x+1, y) tiles
    cy: desired XY offsets between (x, y) and (x, y+1) tiles
    k: unused
    stride: unused
    prefer_orig_order: unused
    links: unused

  Returns:
    force field acting on the mesh, same shape as 'x'
  """
  del k, stride, prefer_orig_order, links

  f_tot = jnp.zeros_like(x)
  dx = x[0, :, :, 1:] - x[0, :, :, :-1]
  dy = x[1, :, 1:, :] - x[1, :, :-1, :]
  zeros = jnp.zeros_like(x[0:1, :, :, :-1])

  fx = dx - cx[0, :, :, :-1]  # applies to (0,0)
  f = jnp.concatenate([fx[None, ...], zeros], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 0], [0, 1]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [0, 0], [1, 0]])

  zeros = jnp.zeros_like(x[0:1, :, :-1, :])
  fy = dy - cy[1, :, :-1, :]
  f = jnp.concatenate([zeros, fy[None, ...]], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 1], [0, 0]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [1, 0], [0, 0]])

  zeros = jnp.zeros_like(x[0:1, :, :-1, :])
  dx = x[0, :, 1:, :] - x[0, :, :-1]
  fx = dx - cy[0, :, :-1, :]
  f = jnp.concatenate([fx[None, ...], zeros], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 1], [0, 0]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [1, 0], [0, 0]])

  zeros = jnp.zeros_like(x[0:1, :, :, :-1])
  dy = x[1, :, :, 1:] - x[1, :, :, :-1]
  fy = dy - cx[1, :, :, :-1]
  f = jnp.concatenate([zeros, fy[None, ...]], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 0], [0, 1]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [0, 0], [1, 0]])

  return f_tot


def elastic_tile_mesh_3d(
    x: jnp.ndarray,
    cx: jnp.ndarray,
    cy: jnp.ndarray,
    k=None,
    stride=None,
    prefer_orig_order=False,
    links=None,
) -> jnp.ndarray:
  """Computes force on nodes of a 3d tile mesh.

  Unused arguments are defined for compatibility with the mesh solver.

  Args:
    x: [3, z, y, x] mesh where every node represents a tile
    cx: desired XYZ offsets between (x, y, z) and (x+1, y, z) tiles
    cy: desired XYZ offsets between (x, y, z) and (x, y+1, z) tiles
    k: unused
    stride: unused
    prefer_orig_order: unused
    links: unused

  Returns:
    force field acting on the mesh, same shape as 'x'
  """
  del k, stride, prefer_orig_order, links

  f_tot = jnp.zeros_like(x)

  zeros = jnp.zeros_like(x[0:1, :, :, :-1])
  dx = x[0, :, :, 1:] - x[0, :, :, :-1]
  fx = dx - cx[0, :, :, :-1]  # applies to (0,0)
  f = jnp.concatenate([fx[None, ...], zeros, zeros], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 0], [0, 1]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [0, 0], [1, 0]])

  zeros = jnp.zeros_like(x[0:1, :, :-1, :])
  dy = x[1, :, 1:, :] - x[1, :, :-1, :]
  fy = dy - cy[1, :, :-1, :]
  f = jnp.concatenate([zeros, fy[None, ...], zeros], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 1], [0, 0]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [1, 0], [0, 0]])

  # x offset from cy/cz
  zeros = jnp.zeros_like(x[0:1, :, :-1, :])
  dx = x[0, :, 1:, :] - x[0, :, :-1, :]
  fx = dx - cy[0, :, :-1, :]
  f = jnp.concatenate([fx[None, ...], zeros, zeros], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 1], [0, 0]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [1, 0], [0, 0]])

  # y offset from cx/cz
  zeros = jnp.zeros_like(x[0:1, :, :, :-1])
  dy = x[1, :, :, 1:] - x[1, :, :, :-1]
  fy = dy - cx[1, :, :, :-1]
  f = jnp.concatenate([zeros, fy[None, ...], zeros], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 0], [0, 1]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [0, 0], [1, 0]])

  # z offset from cx/cy
  zeros = jnp.zeros_like(x[0:1, :, :, :-1])
  dz = x[2, :, :, 1:] - x[2, :, :, :-1]
  fz = dz - cx[2, :, :, :-1]
  f = jnp.concatenate([zeros, zeros, fz[None, ...]], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 0], [0, 1]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [0, 0], [1, 0]])

  zeros = jnp.zeros_like(x[0:1, :, :-1, :])
  dz = x[2, :, 1:, :] - x[2, :, :-1, :]
  fz = dz - cy[2, :, :-1, :]
  f = jnp.concatenate([zeros, zeros, fz[None, ...]], axis=0)
  f = jnp.nan_to_num(f)
  f_tot += jnp.pad(f, [[0, 0], [0, 0], [0, 1], [0, 0]])
  f_tot -= jnp.pad(f, [[0, 0], [0, 0], [1, 0], [0, 0]])
  return f_tot


def optimize_coarse_mesh(
    cx,
    cy,
    cfg: Optional[mesh.IntegrationConfig] = None,
    mesh_fn=elastic_tile_mesh,
) -> np.ndarray:
  """Computes rough initial positions of the tiles.

  Args:
    cx: desired XY offsets between (x, y) and (x+1, y) tiles
    cy: desired XY offsets between (x, y) and (x, y+1) tiles
    cfg: integration config to use; if not specified, falls back to reasonable
      default settings
    mesh_fn: function to use to for computing the forces acting
      on mesh nodes

  Returns:
    optimized tile positions as an array of the same shape as cx/cy
  """
  if cfg is None:
    # Default settings expected to be sufficient for usual situations.
    cfg = mesh.IntegrationConfig(
        dt=0.001,
        gamma=0.0,
        k0=0.0,  # unused
        k=0.1,
        stride=(1, 1),  # unused
        num_iters=1000,
        max_iters=100000,
        stop_v_max=0.001,
        dt_max=100,
    )

  def _mesh_force(x, *args, **kwargs):
    return mesh_fn(x, cx, cy, *args, **kwargs)

  res = mesh.relax_mesh(
      # Initial state (all zeros) corresponds to the regular grid
      # layout with no overlap. Significant deviations from it are
      # expected as the overlap is accounted for in the solution.
      np.zeros_like(cx),
      None,
      cfg,
      mesh_force=_mesh_force,
  )

  # Relative offsets from the baseline position as defined above.
  return np.array(res[0])
