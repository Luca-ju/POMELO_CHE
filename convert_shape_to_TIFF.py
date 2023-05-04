import os
import numpy as np
from osgeo import gdal, ogr, osr
import geopandas as gpd
import pandas as pd
import rasterio as rio
from rasterio.crs import CRS
import shutil
import argparse


def convert_shapefile_to_GTiff(input_path, output_path):

    shape_path = input_path
    gdf = gpd.read_file(shape_path) 

    pixel_size = 0.00083333333

    path = "/scratch3/ldominiak/luca_pomelo_input_data/CHE/CHE_Covariates/che_dst_bsgme_100m_2015.tif"

    with rio.open(path) as src:
        # read the spatial information
        no_data_value = src.nodata
        left, bottom, right, top = src.bounds

    x_res = int(round((right - left) / pixel_size))
    y_res = int(round((top - bottom) / pixel_size))

    raster_data = np.zeros((y_res, x_res), dtype='float32')

    for idx, row in gdf.iterrows():
        x,y = row['geometry'].centroid.coords[0]
        col = int((x - left) / pixel_size)
        row = int((top - y) / pixel_size)
        raster_data[row, col] += 1
    
    profile = {
        'driver': 'GTiff',
        'dtype': raster_data.dtype,
        'nodata': no_data_value,
        'count': 1,
        'width': x_res,
        'height': y_res,
        'crs': CRS.from_epsg(4326),
        'transform': rio.transform.from_bounds(left, bottom, right, top, x_res, y_res),
    }
    
    output_file = output_path

    with rio.open(output_file, 'w', **profile) as dist:
        dist.write(raster_data, 1)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path", type=str, help="Input Shapefile Path")
    parser.add_argument("output_path", type=str, help="Output GeoTiff path")
    args = parser.parse_args()
    
    convert_shapefile_to_GTiff(args.input_path, args.output_path)


if __name__ == "__main__":
    main()