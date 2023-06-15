import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon
import rasterio as rio
from rasterio.features import geometry_mask
from osgeo import ogr
from osgeo import gdal
import numpy as np
from utils_copy_Luca import plot_2dmatrix, create_map_of_valid_ids
import config_pop as cfg

path1 = "/scratch3/ldominiak/luca_pomelo_input_data/CHE/CHE_Census_Data/che_population_2000_2020.csv"
data = pd.read_csv(path1)

path2 = "/scratch3/ldominiak/luca_pomelo_input_data/CHE/CHE_Census_Data/che_subnational_admin_2000_2020.tif"
raster = rio.open(path2) # no data: 8888.0 # gewässer: 0

band1 = raster.read()
indexex_overview = np.unique(band1)
indexex_overview = indexex_overview.tolist() # liste mit allen pixelwerten

cr_census_arr_new = [0,]


for i in range(len(data)):
    if np.any(data["GID"][i] in indexex_overview):
        cr_census_arr_new.append(data["P_2020"][i])
    else:
        pass

cr_census_arr_new = np.asarray(cr_census_arr_new, dtype = 'float32')

cr_census_new = {i: val for i, val in enumerate(cr_census_arr_new)} # dict mit korrespondenzen zu coarse census aus wp daten
cr_census_new.pop(0)
### Census data

no_valid_ids_new = cfg.metadata["che"]["wp_no_data"]

CHE_raster_path = cfg.metadata["che"]["rst_wp_regions_path"]
raster = gdal.Open(CHE_raster_path)
geo_transform = raster.GetGeoTransform()
projection = raster.GetProjection()
geo_metadata_new = {"geo_transform": geo_transform, "projection": projection}

fine_regions = gdal.Open(CHE_raster_path).ReadAsArray().astype(np.uint32)

unique_list = list(np.unique(fine_regions)) ### mache liste aus validen ids
unique_list = unique_list[2:]### die ersten beiden braucht man nicht
unique_list.insert(0,0) 

h = fine_regions.shape[0]
w = fine_regions.shape[1]

relabel_new = np.zeros((h, w), dtype=np.int32)
for i,idx in enumerate(unique_list):
    relabel_new[fine_regions==idx] = i

#relabel ### those are coarse regions basically visualized down below; line 61 in superpixel script

map_valid_ids_new = create_map_of_valid_ids(relabel_new, no_valid_ids_new)

