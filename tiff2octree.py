
import argparse
import fnmatch
import glob
import io
import re
import json
import logging
import os
import random
import sys
import time
import uuid
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import dask
import dask.array as da
import dask.bag as db
import dask.dataframe as dd
import dask_image.imread
from dask.distributed import Client, progress
from dask.diagnostics import ProgressBar, Profiler, ResourceProfiler, CacheProfiler, visualize
import numpy as np
import pandas as pd
import skimage
from skimage import util
from skimage.transform import resize, downscale_local_mean
from dask_jobqueue import LSFCluster
from distributed import LocalCluster
from tifffile import TiffFile
from multiprocessing.pool import ThreadPool

from shutil import which, copyfile
from typing import Union, List, Dict, Any, Optional
import warnings

import ktx
from ktx.util import create_mipmaps, mipmap_dimension, interleave_channel_arrays, downsample_array_xy
from ktx.octree.ktx_from_rendered_tiff import RenderedMouseLightOctree, RenderedTiffBlock

import rasterio
from rasterio.windows import Window

dask.config.set({"jobqueue.lsf.use-stdin": True})

threading_env_vars = [
    "NUM_MKL_THREADS",
    "OPENBLAS_NUM_THREADS",
    "OPENMP_NUM_THREADS",
    "OMP_NUM_THREADS",
]

def make_single_threaded_env_vars(threads: int) -> List[str]:
    return [f"export {var}={threads}" for var in threading_env_vars]


def bsub_available() -> bool:
    """Check if the `bsub` shell command is available
    Returns True if the `bsub` command is available on the path, False otherwise. This is used to check whether code is
    running on the Janelia Compute Cluster.
    """
    result = which("bsub") is not None
    return result


def get_LSFCLuster(
    threads_per_worker: int = 1,
    walltime: str = "1:00",
    death_timeout: str = "600s",
    **kwargs,
) -> LSFCluster:
    """Create a dask_jobqueue.LSFCluster for use on the Janelia Research Campus compute cluster.
    This function wraps the class dask_jobqueue.LSFCLuster and instantiates this class with some sensible defaults,
    given how the Janelia cluster is configured.
    This function will add environment variables that prevent libraries (OPENMP, MKL, BLAS) from running multithreaded routines with parallelism
    that exceeds the number of requested cores.
    Additional keyword arguments added to this function will be passed to the dask_jobqueue.LSFCluster constructor.
    Parameters
    ----------
    threads_per_worker: int
        Number of cores to request from LSF. Directly translated to the `cores` kwarg to LSFCluster.
    walltime: str
        The expected lifetime of a worker. Defaults to one hour, i.e. "1:00"
    cores: int
        The number of cores to request per worker. Defaults to 1.
    death_timeout: str
        The duration for the scheduler to wait for workers before flagging them as dead, e.g. "600s". For jobs with a large number of workers,
        LSF may take a long time (minutes) to request workers. This timeout value must exceed that duration, otherwise the scheduler will
        flag these slow-to-arrive workers as unresponsive and kill them.
    **kwargs:
        Additional keyword arguments passed to the LSFCluster constructor
    Examples
    --------
    >>> cluster = get_LSFCLuster(cores=2, project="scicompsoft", queue="normal")
    """

    if "env_extra" not in kwargs:
        kwargs["env_extra"] = []

    kwargs["env_extra"].extend(make_single_threaded_env_vars(threads_per_worker))

    USER = os.environ["USER"]
    HOME = os.environ["HOME"]

    if "local_directory" not in kwargs:
        # The default local scratch directory on the Janelia Cluster
        kwargs["local_directory"] = f"/scratch/{USER}/"

    if "log_directory" not in kwargs:
        log_dir = f"{HOME}/.dask_distributed/"
        Path(log_dir).mkdir(parents=False, exist_ok=True)
        kwargs["log_directory"] = log_dir

    cluster = LSFCluster(
        cores=threads_per_worker,
        walltime=walltime,
        death_timeout=death_timeout,
        **kwargs,
    )
    return cluster


def get_LocalCluster(threads_per_worker: int = 1, n_workers: int = 0, **kwargs):
    """
    Creata a distributed.LocalCluster with defaults that make it more similar to a deployment on the Janelia Compute cluster.
    This function is a light wrapper around the distributed.LocalCluster constructor.
    Parameters
    ----------
    n_workers: int
        The number of workers to start the cluster with. This defaults to 0 here.
    threads_per_worker: int
        The number of threads to assign to each worker.
    **kwargs:
        Additional keyword arguments passed to the LocalCluster constructor
    Examples
    --------
    >>> cluster = get_LocalCluster(threads_per_worker=8)
    """
    return LocalCluster(
        n_workers=n_workers, threads_per_worker=threads_per_worker, **kwargs
    )


def get_cluster(
    threads_per_worker: int = 1,
    deployment: Optional[str] = None,
    local_kwargs: Dict[str, Any] = {},
    lsf_kwargs: Dict[str, Any] = {"memory": "16GB"},
) -> Union[LSFCluster, LocalCluster]:

    """Convenience function to generate a dask cluster on either a local machine or the compute cluster.
    Create a distributed.Client object backed by either a dask_jobqueue.LSFCluster (for use on the Janelia Compute Cluster)
    or a distributed.LocalCluster (for use on a single machine). This function uses the output of the bsubAvailable function
    to determine whether code is running on the compute cluster or not.
    Additional keyword arguments given to this function will be forwarded to the constructor for the Client object.
    Parameters
    ----------
    threads_per_worker: int
        Number of threads per worker. Defaults to 1.
    deployment: str or None
        Which deployment (LocalCluster or LSFCluster) to prefer. If deployment=None, then LSFCluster is preferred, but LocalCluster is used if
        bsub is not available. If deployment='lsf' and bsub is not available, an error is raised.
    local_kwargs: dict
        Dictionary of keyword arguments for the distributed.LocalCluster constructor
    lsf_kwargs: dict
        Dictionary of keyword arguments for the dask_jobqueue.LSFCluster constructor
    """

    if "cores" in lsf_kwargs:
        warnings.warn(
            "The `cores` kwarg for LSFCLuster has no effect. Use the `threads_per_worker` argument instead."
        )

    if "threads_per_worker" in local_kwargs:
        warnings.warn(
            "the `threads_per_worker` kwarg was found in `local_kwargs`. It will be overwritten with the `threads_per_worker` argument to this function."
        )

    if deployment is None:
        if bsub_available():
            cluster = get_LSFCLuster(threads_per_worker, **lsf_kwargs)
        else:
            cluster = get_LocalCluster(threads_per_worker, **local_kwargs)
    elif deployment == "lsf":
        if bsub_available():
            cluster = get_LSFCLuster(threads_per_worker, **lsf_kwargs)
        else:
            raise EnvironmentError(
                "You requested an LSFCluster but the command `bsub` is not available."
            )
    elif deployment == "local":
        cluster = get_LocalCluster(threads_per_worker, **local_kwargs)
    else:
        raise ValueError(
            f'deployment must be one of (None, "lsf", or "local"), not {deployment}'
        )

    return cluster

def get_crop_from_tiled_tif(page, i0, j0, h, w):
    """Extract a crop from a TIFF image file directory (IFD).
    
    Only the tiles englobing the crop area are loaded and not the whole page.
    This is usefull for large Whole slide images that can't fit int RAM.
    Parameters
    ----------
    page : TiffPage
        TIFF image file directory (IFD) from which the crop must be extracted.
    i0, j0: int
        Coordinates of the top left corner of the desired crop.
    h: int
        Desired crop height.
    w: int
        Desired crop width.
    Returns
    -------
    out : ndarray of shape (imagedepth, h, w, sampleperpixel)
        Extracted crop.
    """

    if not page.is_tiled:
        raise ValueError("Input page must be tiled.")

    im_width = page.imagewidth
    im_height = page.imagelength

    if h < 1 or w < 1:
        raise ValueError("h and w must be strictly positive.")

    if i0 < 0 or j0 < 0 or i0 + h >= im_height or j0 + w >= im_width:
        raise ValueError("Requested crop area is out of image bounds.")

    tile_width, tile_height = page.tilewidth, page.tilelength
    i1, j1 = i0 + h, j0 + w

    tile_i0, tile_j0 = i0 // tile_height, j0 // tile_width
    tile_i1, tile_j1 = np.ceil([i1 / tile_height, j1 / tile_width]).astype(int)

    tile_per_line = int(np.ceil(im_width / tile_width))

    out = np.empty((page.imagedepth,
                    (tile_i1 - tile_i0) * tile_height,
                    (tile_j1 - tile_j0) * tile_width,
                    page.samplesperpixel), dtype=page.dtype)

    fh = page.parent.filehandle

    jpegtables = page.tags.get('JPEGTables', None)
    if jpegtables is not None:
        jpegtables = jpegtables.value

    for i in range(tile_i0, tile_i1):
        for j in range(tile_j0, tile_j1):
            index = int(i * tile_per_line + j)

            offset = page.dataoffsets[index]
            bytecount = page.databytecounts[index]

            fh.seek(offset)
            data = fh.read(bytecount)
            tile, indices, shape = page.decode(data, index, jpegtables)

            im_i = (i - tile_i0) * tile_height
            im_j = (j - tile_j0) * tile_width
            out[:, im_i: im_i + tile_height, im_j: im_j + tile_width, :] = tile

    im_i0 = i0 - tile_i0 * tile_height
    im_j0 = j0 - tile_j0 * tile_width

    return out[:, im_i0: im_i0 + h, im_j0: im_j0 + w, :]

def get_untiled_crop(page, i0, j0, h, w):
    if page.is_tiled:
        raise ValueError("Input page must not be tiled")
    
    im_width = page.imagewidth
    im_height = page.imagelength
    
    if h < 1 or w < 1:
        raise ValueError("h and w must be strictly positive.")

    i1, j1 = i0 + h, j0 + w
    if i0 < 0 or j0 < 0 or i1 >= im_height or j1 >= im_width:
        raise ValueError(f"Requested crop area is out of image bounds.{i0}_{i1}_{im_height}, {j0}_{j1}_{im_width}")
        
    out = np.empty((page.imagedepth, h, w, page.samplesperpixel), dtype=page.dtype)
    fh = page.parent.filehandle
    
    for index in range(i0, i1):
        offset = page.dataoffsets[index]
        bytecount = page.databytecounts[index]

        fh.seek(offset)
        data = fh.read(bytecount)

        tile, indices, shape = page.decode(data, index)
        
        out[:,index-i0,:,:] = tile[:,:,j0:j1,:]
    
    return out

def get_crop_tif_stack(fpath ,s0, i0, j0, d, h, w):
    tf = TiffFile(fpath)

    out = []

    for page in tf.pages:
        if page.is_tiled:
            out.append(get_crop_from_tiled_tif(page, i0, j0, h, w))
        else:
            out.append(get_untiled_crop(page, i0, j0, h, w))
    return out

# 2nd brightest of the 8 pixels
# equivalent to sort(vec(arg))[7] but half the time and a third the memory usage
def downsampling_function(view):
    m0 = 0
    m1 = 0
    for z in range(0, 2):
        for y in range(0, 2):
            for x in range(0, 2):
                tmp = view[z, y, x]
                if tmp > m0:
                    m1 = m0
                    m0 = tmp
                elif tmp > m1:
                    m1 = tmp
    return m1

def downsample(out_tile_jl, coord, shape_leaf_px, scratch):
    iy = ((coord - 1) >> 1) & 1 * shape_leaf_px[1] >> 1
    ix = ((coord - 1) >> 0) & 1 * shape_leaf_px[2] >> 1
    iz = ((coord - 1) >> 2) & 1 * shape_leaf_px[3] >> 1
    for z in range(1, shape_leaf_px[3] - 1, 2):
        tmpz = iz + (z + 1) >> 1
        for x in range(1, shape_leaf_px[2] - 1, 2):
            tmpx = ix + (x + 1) >> 1
            for y in range(1, shape_leaf_px[1] - 1, 2):
                tmpy = iy + (y + 1) >> 1
                out_tile_jl[tmpy, tmpx, tmpz] = downsampling_function(scratch[y:y + 1, x:x + 1, z:z + 1])

def save_data(path, image):
    skimage.io.imsave(path, image)

def octree_division(jobs, ch, nlevels, targetpath, dim_leaf, relpath, img_view):
        morton = relpath.split(os.path.sep)
        level = 1 if relpath == "" else (morton.length + 1)
        print("Level: "+str(level)+", Current path: "+str(targetpath)+"/"+str(relpath))
        Path(os.path.join(targetpath, relpath)).mkdir(parents=True, exist_ok=True)
        if level < nlevels:
            img_down_next = np.zeros((3, 2))
            dim_view = np.array([img_view[0][1] - img_view[0][0], img_view[1][1] - img_view[1][0], img_view[2][1] - img_view[2][0]])
            for z in range(1,3):
                for y in range(1,3):
                    for x in range(1,3):
                        octant_path = str((x + 2 * (y - 1) + 4 * (z - 1)))
                        start_x = 1 if x == 1 else dim_view[2] >> 1 + 1
                        end_x = dim_view[2] >> 1 if x == 1 else dim_view[2]
                        start_y = 1 if y == 1 else dim_view[1] >> 1 + 1
                        end_y = dim_view[1] >> 1 if y == 1 else dim_view[1]
                        start_z = 1 if z == 1 else dim_view[3] >> 1 + 1
                        end_z = dim_view[3] >> 1 if z == 1 else dim_view[3]
      
            print("octant:({0},{1},{2}) -> {3}/{4} ({5}:{6}, {7}:{8}, {9}:{10})".format(x, y, z, relpath, octant_path, start_x, end_x, start_y, end_y, start_z, end_z))
            new_view = np.array([start_x, end_x], [start_y, end_y], [start_z, end_z])
            octree_division(jobs, ch, targetpath, os.path.join(relpath, octant_path), new_view)
        
        saveme = img_view if level == nlevels else img_down_next
        filename = "default.{0}.tif".format(ch)
        save_data(os.path.join(targetpath, relpath, filename), saveme)
        if (level > 1):
            downsample(os.path.join(targetpath, relpath, filename), dim_leaf, saveme)


def downsample_2ndmax(out_tile_jl, coord, shape_leaf_px, scratch):
    ix = (((coord - 1) >> 0) & 1) * shape_leaf_px[2] >> 1
    iy = (((coord - 1) >> 1) & 1) * shape_leaf_px[1] >> 1
    iz = (((coord - 1) >> 2) & 1) * shape_leaf_px[0] >> 1
    for z in range(0, shape_leaf_px[0]-1, 2):
        tmpz = iz + ((z + 1) >> 1)
        for y in range(0, shape_leaf_px[1]-1, 2):
            tmpy = iy + ((y + 1) >> 1)
            for x in range(0, shape_leaf_px[2]-1, 2):
                tmpx = ix + ((x + 1) >> 1)
                out_tile_jl[tmpz, tmpy, tmpx] = downsampling_function(scratch[z:z + 2, y:y + 2, x:x + 2])

def downsample_aa(out_tile_jl, coord, shape_leaf_px, scratch):
    ix = (((coord - 1) >> 0) & 1) * shape_leaf_px[2] >> 1
    iy = (((coord - 1) >> 1) & 1) * shape_leaf_px[1] >> 1
    iz = (((coord - 1) >> 2) & 1) * shape_leaf_px[0] >> 1
    ixx = ix+(shape_leaf_px[2] >> 1)
    iyy = iy+(shape_leaf_px[1] >> 1)
    izz = iz+(shape_leaf_px[0] >> 1)
    if scratch.dtype is np.dtype('uint8'):
        out_tile_jl[iz:izz, iy:iyy, ix:ixx] = util.img_as_ubyte(resize(scratch, (shape_leaf_px[0]>>1, shape_leaf_px[1]>>1, shape_leaf_px[2]>>1), anti_aliasing=True))
    elif scratch.dtype is np.dtype('uint16'):
        out_tile_jl[iz:izz, iy:iyy, ix:ixx] = util.img_as_uint(resize(scratch, (shape_leaf_px[0]>>1, shape_leaf_px[1]>>1, shape_leaf_px[2]>>1), anti_aliasing=True))
    elif scratch.dtype is np.dtype('float32'):
        out_tile_jl[iz:izz, iy:iyy, ix:ixx] = util.img_as_float32(resize(scratch, (shape_leaf_px[0]>>1, shape_leaf_px[1]>>1, shape_leaf_px[2]>>1), anti_aliasing=True))

def downsample_area(out_tile_jl, coord, shape_leaf_px, scratch):
    ix = (((coord - 1) >> 0) & 1) * shape_leaf_px[2] >> 1
    iy = (((coord - 1) >> 1) & 1) * shape_leaf_px[1] >> 1
    iz = (((coord - 1) >> 2) & 1) * shape_leaf_px[0] >> 1
    ixx = ix+(shape_leaf_px[2] >> 1)
    iyy = iy+(shape_leaf_px[1] >> 1)
    izz = iz+(shape_leaf_px[0] >> 1)
    down_img = downscale_local_mean(scratch, (2, 2 ,2))
    out_tile_jl[iz:izz, iy:iyy, ix:ixx] = down_img.astype(scratch.dtype)

def get_octree_relative_path(chunk_coord, level):
    relpath = ''
    pos = np.asarray(chunk_coord)
    pos = np.add(pos, np.full(3, -1))
    for lv in range(level-1, -1, -1):
        d = pow(2, lv)
        octant_path = str(1 + int(pos[2] / d) + 2 * int(pos[1] / d) + 4 * int(pos[0] / d))
        if lv < level-1:
            relpath = os.path.join(relpath, octant_path)
        pos[2] = pos[2] - int(pos[2] / d) * d
        pos[1] = pos[1] - int(pos[1] / d) * d
        pos[0] = pos[0] - int(pos[0] / d) * d
    
    return relpath

def get_cropped_image_rasterio(input_dir, file_paths, z0, y0, x0, d, h, w, type):
    output = np.zeros((d, h, w), dtype=type)
    for i in range(z0, z0 + d):
        if i - z0 < len(file_paths):
            with rasterio.open(os.path.join(input_dir, file_paths[i - z0])) as src:
                data = src.read(1, window=Window(x0, y0, w, h))
                output[i - z0, :h, :w] = data[:, :]
    return output

def save_block_from_slices_batch(chunk_coords, input_dir, file_paths, target_path, nlevels, dim_leaf, ch, type):
    for pos in chunk_coords:
        save_block_from_slices(pos, input_dir, file_paths, target_path, nlevels, dim_leaf, ch, type)

def save_block_from_slices(chunk_coord, input_dir, file_paths, target_path, nlevels, dim_leaf, ch, type):
    relpath = get_octree_relative_path(chunk_coord, nlevels)

    dir_path = os.path.join(target_path, relpath)
    full_path = os.path.join(dir_path, "default.{0}.tif".format(ch))

    #print((dim_leaf[0]*chunk_coord[0], dim_leaf[1]*chunk_coord[1], dim_leaf[2]*chunk_coord[2]))

    img_data = get_cropped_image_rasterio(input_dir, file_paths, dim_leaf[0]*(chunk_coord[0]-1), dim_leaf[1]*(chunk_coord[1]-1), dim_leaf[2]*(chunk_coord[2]-1), dim_leaf[0], dim_leaf[1], dim_leaf[2], type)
    
    print(full_path)
    Path(dir_path).mkdir(parents=True, exist_ok=True)
    skimage.io.imsave(full_path, img_data, compress=6)


def save_block(chunk, target_path, nlevels, dim_leaf, ch, block_id=None):
    if block_id == None:
        return np.array(0)[None, None, None]
    
    relpath = get_octree_relative_path(block_id, nlevels)

    dir_path = os.path.join(target_path, relpath)
    Path(dir_path).mkdir(parents=True, exist_ok=True)

    full_path = os.path.join(dir_path, "default.{0}.tif".format(ch))

    print(full_path)

    skimage.io.imsave(full_path, chunk, compress=6)

    return np.array(block_id[0])[None, None, None] if block_id != None else np.array(0)[None, None, None]

def downsample_and_save_block_batch(chunk_coords, target_path, level, dim_leaf, ch, type, downsampling_method):
    for pos in chunk_coords:
        downsample_and_save_block(pos, target_path, level, dim_leaf, ch, type, downsampling_method)

def downsample_and_save_block(chunk_coord, target_path, level, dim_leaf, ch, type, downsampling_method):

    relpath = get_octree_relative_path(chunk_coord, level)

    dir_path = os.path.join(target_path, relpath)
    img_name = "default.{0}.tif".format(ch)
    
    img_down = np.zeros(dim_leaf, dtype=type)
    
    for oct in range(1, 9):
        blk_path = os.path.join(dir_path, str(oct))
        scratch = skimage.io.imread(os.path.join(blk_path, img_name))
        if downsampling_method == 'area':
            downsample_area(img_down, oct, dim_leaf, scratch)
        elif downsampling_method == 'aa':
            downsample_aa(img_down, oct, dim_leaf, scratch)
        else:
            downsample_2ndmax(img_down, oct, dim_leaf, scratch)

    full_path = os.path.join(dir_path, img_name)

    print(full_path)

    skimage.io.imsave(full_path, img_down, compress=6)

def convert_block_ktx_batch(chunk_coords, source_path, target_path, level, downsample_intensity, downsample_xy, make_dir):
    for pos in chunk_coords:
        convert_block_ktx(pos, source_path, target_path, level, downsample_intensity, downsample_xy, make_dir)

def convert_block_ktx(chunk_coord, source_path, target_path, level, downsample_intensity, downsample_xy, make_dir):

    relpath = get_octree_relative_path(chunk_coord, level)
    if make_dir:
        dir_path = os.path.join(target_path, relpath)
        Path(dir_path).mkdir(parents=True, exist_ok=True)

    octree_path0 = relpath.split(os.path.sep)
    octree_path = []
    for level in octree_path0:
        if re.match(r'[1-8]', level):
            octree_path.append(int(level))
    
    o = RenderedMouseLightOctree(input_folder=source_path, downsample_intensity=downsample_intensity, downsample_xy=downsample_xy)
    block = RenderedTiffBlock(o.input_folder, o, octree_path)
    
    file_name = 'block'
    if downsample_intensity:
        file_name += '_8'
    if downsample_xy:
        file_name += '_xy'
    opath1 = "".join([str(n) for n in block.octree_path])
    file_name += '_'+opath1+".ktx"
    output_dir = os.path.join(target_path, relpath)
    full_file = os.path.join(output_dir, file_name)
    
    print(full_file)
    
    overwrite = True
    if os.path.exists(full_file) and not overwrite:
        file_size = os.stat(full_file).st_size
        if file_size > 0:
            print("Skipping existing file")
            return
    f = open(full_file, 'wb')
    try:
        block.write_ktx_file(f)
        f.flush()
        f.close()
    except:
        print("Error writing file %s" % full_file)
        f.flush()
        f.close()
        os.unlink(f.name)

def build_octree_from_tiff_slices():
    argv = sys.argv
    argv = argv[1:]

    usage_text = ("Usage:" + "  slice2octree.py" + " [options]")
    parser = argparse.ArgumentParser(description=usage_text)
    parser.add_argument("-t", "--thread", dest="number", type=int, default=16, help="number of threads")
    parser.add_argument("-i", "--inputdir", dest="input", type=str, default="", help="input directories")
    parser.add_argument("-f", "--inputfile", dest="file", type=str, default="", help="input image stacks")
    parser.add_argument("-o", "--output", dest="output", type=str, default="", help="output directory")
    parser.add_argument("-l", "--level", dest="level", type=int, default=1, help="number of levels")
    parser.add_argument("-c", "--channel", dest="channel", type=int, default=0, help="channel id")
    parser.add_argument("-d", "--downsample", dest="downsample", type=str, default='area', help="downsample method: 2ndmax, area, aa (anti-aliasing)")
    parser.add_argument("-m", "--monitor", dest="monitor", default=False, action="store_true", help="activate monitoring")
    parser.add_argument("--origin", dest="origin", type=str, default="0,0,0", help="position of the corner of the top-level image in nanometers")
    parser.add_argument("--voxsize", dest="voxsize", type=str, default="1.0,1.0,1.0", help="voxel size of the top-level image")
    parser.add_argument("--memory", dest="memory", type=str, default="16GB", help="memory amount per thread (for LSF cluster)")
    parser.add_argument("--project", dest="project", type=str, default=None, help="project name (for LSF cluster)")
    parser.add_argument("--maxjobs", dest="maxjobs", type=int, default=16, help="maximum jobs (for LSF cluster)")
    parser.add_argument("--lsf", dest="lsf", default=False, action="store_true", help="use LSF cluster")
    parser.add_argument("--ktx", dest="ktx", default=False, action="store_true", help="generate ktx files")
    parser.add_argument("--ktxout", dest="ktxout", type=str, default=None, help="output directory for a ktx octree")
    parser.add_argument("--cluster", dest="cluster", type=str, default=None, help="address of a dask scheduler server")

    if not argv:
        parser.print_help()
        exit()

    args = parser.parse_args(argv)

    tnum = args.number
    indirs = args.input.split(",")
    infiles = args.file.split(",")
    outdir = args.output
    nlevels = args.level
    ch = args.channel
    dmethod = args.downsample
    monitoring = args.monitor
    ktxout = args.ktxout
    ktx_mkdir = False

    if not ktxout:
        ktxout = outdir
    else:
        ktx_mkdir = True

    my_lsf_kwargs={}
    if args.memory:
       my_lsf_kwargs['memory'] = args.memory
    if args.project:
       my_lsf_kwargs['project'] = args.project
    
    cluster = None
    if args.cluster:
        cluster = args.cluster
    elif args.lsf:
        cluster = get_cluster(deployment="lsf", lsf_kwargs = my_lsf_kwargs)
        cluster.adapt(minimum_jobs=1, maximum_jobs = args.maxjobs)
        cluster.scale(tnum)
    else:
        cluster = get_cluster(deployment="local")
        cluster.scale(tnum)

    dashboard_address = None
    if monitoring: 
        dashboard_address = ":8787"
        print(f"Starting dashboard on {dashboard_address}")
        client = Client(address=cluster, processes=True, dashboard_address=dashboard_address)
    else:
        client = Client(address=cluster)

    task_num = tnum * 2
    if not args.lsf:
        tmp_batch_num = 0
        workers_info = client.scheduler_info()['workers']
        for k in workers_info.keys():
            tmp_batch_num += workers_info[k]['nthreads']
        if tmp_batch_num > 0:
            task_num = tmp_batch_num * 2

    print("batch_num: " + str(task_num))

    images = None
    dim = None
    volume_dtype = None
    if len(indirs) > 0 and indirs[0]:
        tif_files = [f for f in os.listdir(indirs[0]) if f.endswith('.tif')]
        tif_files.sort()
        im_width = 0
        im_height = 0
        with TiffFile(os.path.join(indirs[0], tif_files[0])) as tif:
            im_width = tif.pages[0].imagewidth
            im_height = tif.pages[0].imagelength
            print(tif.pages[0].dtype)
            volume_dtype = tif.pages[0].dtype
        dim = np.asarray((len(tif_files), im_height, im_width))
    elif len(infiles) > 0 and infiles[0]:
        images = dask_image.imread.imread(infiles[0])
        dim = np.asarray(images.shape)
        volume_dtype = images.dtype
    else:
        print('Please specify an input dataset.')
        return
    
    print("Will generate octree with " + str(nlevels) +" levels to " + str(outdir))
    print("Image dimensions: " + str(dim))
    
    while(dim[0] % pow(2, nlevels) > 0):
        dim[0] -= 1
    while(dim[1] % pow(2, nlevels) > 0):
        dim[1] -= 1
    while(dim[2] % pow(2, nlevels) > 0):
        dim[2] -= 1
    dim_leaf = [x >> (nlevels - 1) for x in dim]

    print("Adjusted image size: " + str(dim) + ", Dim leaf: " + str(dim_leaf))

    ranges = [(c, c+dim_leaf[0]) for c in range(0,dim[0],dim_leaf[0])]


    ostr = args.origin.split(",")
    o = []
    if len(ostr) > 0:
        o.append(ostr[0])
    else:
        o.append("0.0")
    if len(ostr) > 1:
        o.append(ostr[1])
    else:
        o.append("0.0")
    if len(ostr) > 2:
        o.append(ostr[2])
    else:
        o.append("0.0")

    vsstr = args.voxsize.split(",")
    vs = []
    if len(vsstr) > 0:
        vs.append(float(vsstr[0]))
    else:
        vs.append(1.0)
    if len(vsstr) > 1:
        vs.append(float(vsstr[1]))
    else:
        vs.append(1.0)
    if len(vsstr) > 2:
        vs.append(float(vsstr[2]))
    else:
        vs.append(1.0)

    l = []
    l.append("ox: " + o[0])
    l.append("oy: " + o[1])
    l.append("oz: " + o[2])
    l.append("sx: " + str(dim[0] * vs[0]))
    l.append("sy: " + str(dim[1] * vs[1]))
    l.append("sz: " + str(dim[2] * vs[2]))
    l.append("nl: " + str(nlevels))

    tr_path = os.path.join(outdir, "transform.txt")
    with open(tr_path, mode='w') as f:
        f.write('\n'.join(l))

    if ktx_mkdir and outdir != ktxout:
        Path(ktxout).mkdir(parents=True, exist_ok=True)
        copyfile(tr_path, os.path.join(ktxout, "transform.txt"))

    #initial
    if len(indirs) > 0:
        ch_ids = []
        for i in range(0, len(indirs)):
            if i > 0:
                tif_files = [os.path.join(indirs[i], f) for f in os.listdir(indirs[i]) if f.endswith('.tif')]
            chunked_list = [tif_files[j:j+dim_leaf[0]] for j in range(0, len(tif_files), dim_leaf[0])]
            futures = []
            bnum = pow(2, nlevels - 1)
            for z in range(1, bnum+1):
                batch_block_num = (int)(bnum * bnum / task_num)
                if batch_block_num < 1: 
                    batch_block_num = 1
                coord_list = []
                for y in range(1, bnum+1):
                    for x in range(1, bnum+1):
                        coord_list.append((z,y,x))
                        if len(coord_list) >= batch_block_num:
                            future = dask.delayed(save_block_from_slices_batch)(coord_list, indirs[i], chunked_list[z-1], outdir, nlevels, dim_leaf, ch+i, volume_dtype)
                            futures.append(future)
                            coord_list = []
            with ProgressBar():
                dask.compute(futures)
            ch_ids.append(ch+i)
    elif len(infiles) > 0:
        adjusted = images[:dim[0], :dim[1], :dim[2]]
        volume = adjusted.rechunk(dim_leaf)
        volume.map_blocks(save_block, outdir, nlevels, dim_leaf, ch, chunks=(1,1,1)).compute()
        ch_ids = []
        for i in range(1, len(infiles)):
            images = dask_image.imread.imread(infiles[i])
            adjusted = images[:dim[0], :dim[1], :dim[2]]
            volume = adjusted.rechunk(dim_leaf)
            volume.map_blocks(save_block, outdir, nlevels, dim_leaf, ch+i, chunks=(1,1,1)).compute()
            ch_ids.append(ch+i)

    #downsample
    for lv in range(nlevels-1, 0, -1):
        futures = []
        bnum = pow(2, lv - 1)
        batch_block_num = (int)(bnum * bnum * bnum * len(ch_ids) / task_num)
        if batch_block_num < 1: 
            batch_block_num = 1
        coord_list = []
        for z in range(1, bnum+1):
            for y in range(1, bnum+1):
                for x in range(1, bnum+1):
                    for c in ch_ids:
                        coord_list.append((z,y,x))
                        if len(coord_list) >= batch_block_num:
                            future = dask.delayed(downsample_and_save_block_batch)(coord_list, outdir, lv, dim_leaf, c, volume_dtype, dmethod)
                            futures.append(future)
                            coord_list = []
        with ProgressBar():
            dask.compute(futures)

    #ktx conversion
    if args.ktx:
        for lv in range(nlevels, 0, -1):
            futures = []
            bnum = pow(2, lv - 1)
            batch_block_num = (int)(bnum * bnum * bnum / task_num)
            if batch_block_num < 1: 
                batch_block_num = 1
            coord_list = []
            for z in range(1, bnum+1):
                for y in range(1, bnum+1):
                    for x in range(1, bnum+1):
                        coord_list.append((z,y,x))
                        if len(coord_list) >= batch_block_num:
                            future_ktx = dask.delayed(convert_block_ktx_batch)(coord_list, outdir, ktxout, lv, True, True, ktx_mkdir if lv == nlevels else False)
                            futures.append(future_ktx)
                            coord_list = []
            with ProgressBar():
                dask.compute(futures)

    client.close()


def main():
    build_octree_from_tiff_slices()


if __name__ == '__main__':
    main()
