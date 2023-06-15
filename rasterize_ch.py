from os.path import isfile, join, exists 
from os import makedirs, walk, remove
import glob

import pandas as pd
import numpy as np
from osgeo import gdal
from rasterio.warp import transform_geom
from rasterio.features import is_valid_geom
from rasterio.warp import reproject, Resampling, calculate_default_transform
from rasterio.windows import Window
from rasterio import CRS
from rasterio.transform import from_origin
import rasterio 

from tqdm import tqdm, tqdm_pandas
import scipy
from PIL import Image
from PIL.Image import Resampling as PILRes
import rioxarray as rxr

tqdm.pandas()
from utils_copy_Luca import plot_2dmatrix


def progress_cb(complete, message, cb_data):
    '''Emit progress report in numbers for 10% intervals and dots for 3% in the classic GDAL style'''
    if int(complete*100) % 10 == 0:
        print(f'{complete*100:.0f}', end='', flush=True)
    elif int(complete*100) % 3 == 0:
        print(f'.', end='', flush=True)
        

def rasterize_csv(csv_filename, source_popBi_file, template_file, output_file,force=False):
    # definition
    resample_alg = gdal.GRA_Cubic

    # global
    cx = 100
    cy = 100

    # pseudoscaling
    ps = 10


    # with rasterio.open(template_file, "r") as tmp:
    #     tmp_meta = tmp.meta.copy()

    # read
    if not isfile(source_popBi_file) or force:

        # read swiss census data
        df = pd.read_csv(csv_filename, sep=';')
        #df = pd.read_csv(csv_filename)[["E_KOORD", "N_KOORD", "B20BTOT"]]

        E_min = df["E_KOORD"].min()
        N_min = df["N_KOORD"].max()-1
        w = ( df["E_KOORD"].max() - df["E_KOORD"].min() )//cx + 1
        h = ( df["N_KOORD"].max() - df["N_KOORD"].min() )//cy + 1
        pop_raster = np.zeros((h,w))

        df["E_IMG"] = (df["E_KOORD"] - E_min) // cx
        df["N_IMG"] = -(df["N_KOORD"] - N_min) // cy

        # convert to raster
        pop_raster[df["N_IMG"].tolist(), df["E_IMG"].to_list()] = df["B20BTOT"]

        meta = {"driver": "GTiff", "count": 1, "dtype": "float32", "width":w, "height":h, "crs": CRS.from_epsg(2056),
                "transform": from_origin(E_min, N_min, cx, cy)}

        # save it as temp raster
        with rasterio.open("tmp.tif", 'w', **meta) as dst:
            dst.write(pop_raster,1)

    src = rxr.open_rasterio('tmp.tif')
    target = rxr.open_rasterio(template_file)

    # no-data values from 1e38 to zero
    target.rio.write_nodata(0, inplace=True)
    target.rio.set_nodata(0, inplace=True)
    src.rio.set_nodata(0, inplace=True)
    src.rio.write_nodata(0, inplace=True)

    ps = 10 # new scaling

    new_height = src.rio.height * ps
    new_width = src.rio.width * ps
    new_src = src.rio.reproject(src.rio.crs, shape=(new_height, new_width), resampling=Resampling.nearest)

    new_src /= ps**2 # ugly bug

    # no-data values from 1e38 to zero
    new_src.rio.set_nodata(0, inplace=True)
    new_src.rio.write_nodata(0, inplace=True)

    final_src = new_src.rio.reproject_match(target, resampling=Resampling.sum)

    final_clean = final_src.where(final_src>0.5, 0)

    print(final_clean.sum())

    final_clean.rio.to_raster(output_file)
        
    print("Done")

    remove('tmp.tif')

    return None


def process():
    # inputs
    source_folder = "/scratch3/ldominiak/luca_pomelo_input_data/CHE/CHE_Census_Data/" # hani
    source_filename = "STATPOP2020.csv" # hani
    template_file = "/scratch3/ldominiak/luca_pomelo_input_data/CHE/CHE_Covariates/che_tt50k_100m_2000.tif" # hani
    
    source_meta_poprasterBi = "PopRasterBi_2020.tif"
    output_file = "STATPOP_Census_2020_cleaned.tif"

    source_file = join(source_folder, source_filename)
    source_popBi_file = join(source_folder, source_meta_poprasterBi)
    output_file = join(source_folder, output_file)

    rasterize_csv(source_file, source_popBi_file, template_file, output_file, force=True)
    
    return


if __name__=="__main__":
    process()
    print("Done")