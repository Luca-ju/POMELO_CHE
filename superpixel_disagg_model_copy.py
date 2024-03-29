import os
os.environ["OMP_PROC_BIND"] = os.environ.get("OMP_PROC_BIND", "true")
import argparse
import pickle
import numpy as np
import torch
import matplotlib.pyplot as plt 
from osgeo import gdal
import wandb
from pathlib import Path
import h5py 
from tqdm import tqdm as tqdm
from pathlib import Path
import random
import config_pop as cfg
import rioxarray as rxr


import config_pop as cfg
from utils_copy_Luca import read_input_raster_data, read_input_raster_data_to_np, compute_performance_metrics, write_geolocated_image, create_map_of_valid_ids, \
    compute_grouped_values, transform_dict_to_array, transform_dict_to_matrix, calculate_densities, plot_2dmatrix, \
    bbox2
from cy_utils import compute_map_with_new_labels, compute_accumulated_values_by_region, compute_disagg_weights, \
    set_value_for_each_region

from pix_transform_CHE.pix_admin_transform import PixAdminTransform 
from pix_transform_CHE.evaluation import Eval5Fold_PixAdminTransform, EvalModel_PixAdminTransform, Eval5Fold_FeatureImportance
from pix_transform_utils.plots import plot_result
from distutils.util import strtobool

from coarse_census_CH_test import cr_census_arr_new, geo_metadata_new, relabel_new, cr_census_new, map_valid_ids_new

def get_dataset(dataset_name, params, building_features, related_building_features):

    # configure pathscd /
    rst_wp_regions_path = cfg.metadata[dataset_name]["rst_wp_regions_path"] # admin regions vo wp

    # Read input data
    input_paths = cfg.input_paths[dataset_name]
    no_data_values = cfg.no_data_values[dataset_name]

    cr_census_arr = cr_census_arr_new 

    no_valid_ids = cfg.metadata[dataset_name]["wp_no_data"]
    print("no_valid_ids {}".format(no_valid_ids))
   
    num_coarse_regions = len(cr_census_arr)
    geo_metadata = geo_metadata_new ## meta daten eines schweizer tiff; hani

    features = read_input_raster_data_to_np(input_paths) ### Bildern , width, height; muss alles einheitlich sein

    map_valid_ids = map_valid_ids_new

    cr_regions = relabel_new

    cr_census = cr_census_new

    # Reorganize features into one numpy array and handling of no-data mask
    feature_names = list(input_paths.keys())

    
    ##### COMMENT THIS OUT WHEN WORKING WITH ONE BUILDING DATASET #####
    
    # Merging building features from TLM and OSM if both are available

    if ('buildings_count_2020_TLM' in feature_names) and ('buildings_count_2020_OSM' in feature_names):
        # Taking the mean over both available features
        #  max operation for mean building areas
        gidx = np.where([el=='buildings_count_2020_OSM' for el in feature_names])
        midx = np.where([el=='buildings_count_2020_TLM' for el in feature_names])

        mean_values = np.mean(np.concatenate([features[gidx,:,:,None], features[midx,:,:,None]], 4), 4).squeeze()

        mask = mean_values > 1  # Create a mask for mean values greater than 1
        mean_values = np.ceil(mean_values) # round to next highest integer

        features[gidx, mask] = mean_values[mask]  # Replace values in gidx with mean values where mask is True
        feature_names[np.squeeze(gidx)] = 'buildings_merge'

        bkeepers = np.where([el!='buildings_count_2020_TLM' for el in feature_names])
        features = features[bkeepers]
        feature_names.remove('buildings_count_2020_TLM') 
    
    ############################# UNTIL HERE ################################
            
    # Assert that first input is a building variable
    assert(feature_names[0] in building_features)

    num_feat, ih, iw = features.shape
    valid_data_mask = torch.ones( (ih, iw), dtype=torch.bool) 
    for i, name in enumerate(feature_names):
        
        if name in (building_features + related_building_features):
            features[i][features[i]<0] = 0
        else:
            this_mask = features[i]!=no_data_values[name]
            if no_data_values[name]>1e30:
                this_mask *= ~(np.isclose(features[i],no_data_values[name]))
            valid_data_mask *= this_mask

        # Normalize the features, execpt for the buildings layer when the scale Network is used
        if (params['Net'] in ['ScaleNet']) and (name not in building_features):
            if name in list(cfg.norms[dataset_name].keys()):
                # normalize by known mean and std
                features[i] = (features[i] - cfg.norms[dataset_name][name][0]) / cfg.norms[dataset_name][name][1]
            else:
                raise Exception("Did not find precalculated mean and std")
                
    # features = torch.cat(features, 0)
    features = torch.from_numpy(features)

    if params["Net"]=='ScaleNet':
        valid_data_mask *= features[0]>0

    guide_res = features.shape[1:3]

    # also account for the invalid map ids
    valid_data_mask *= map_valid_ids.astype(bool)

    cr_built_area = {}
    
    for key in tqdm(cr_census.keys()):
        cr_built_area[key] = valid_data_mask[cr_regions==key].sum()

    replacement = 0

    valid_data_mask =  valid_data_mask.to(torch.bool)
    map_valid_ids = torch.from_numpy(map_valid_ids.astype(np.bool8))
    cr_regions = torch.from_numpy(cr_regions.astype(np.int32)) 

    # replacements of invalid values
    features[:,~valid_data_mask] = replacement

    dataset = {
        "features": features,
        "feature_names":feature_names,
        "valid_data_mask": valid_data_mask,
        "map_valid_ids": map_valid_ids,
        "cr_regions": cr_regions,
        "cr_census": cr_census,
        "guide_res": guide_res,
        "geo_metadata": geo_metadata,
        "num_valid_pix": valid_data_mask.sum(),
        "coarse": "coarse",
    }
    
    return dataset

#### Bis hier ist gut


def prep_train_hdf5_file(training_source, h5_filename, var_filename, silent_mode=True):

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Iterate throuh the image an cut out examples
    tX,tY,tregid,tMasks,tregMasks,tBBox = [],[],[],[],[],[]

    tr_features, tr_census, tr_regions, tr_guide_res, tr_valid_data_mask, level, feature_names = training_source
    
    tr_regions = tr_regions.to(device)
    tr_valid_data_mask = tr_valid_data_mask.to(device)
    
    for regid in tqdm(tr_census.keys(), disable=silent_mode):
        regmask = regid==tr_regions
        mask = regmask * tr_valid_data_mask
        boundingbox = bbox2(regmask)
        # boundingbox = bbox2(mask)
        rmin, rmax, cmin, cmax = boundingbox
        tX.append(tr_features[:,rmin:rmax, cmin:cmax].numpy())
        tY.append(np.asarray(tr_census[regid]))
        tregid.append(np.asarray(regid))
        tMasks.append(mask[rmin:rmax, cmin:cmax].cpu().numpy())
        tregMasks.append(regmask[rmin:rmax, cmin:cmax].cpu().numpy())
        boundingbox = [rmin.cpu(), rmax.cpu(), cmin.cpu(), cmax.cpu()]
        tBBox.append(boundingbox)
        
    tr_regions = tr_regions.cpu()
    tr_valid_data_mask = tr_valid_data_mask.cpu().numpy()

    # write to disk
    
    with open(var_filename, 'wb') as handle:
        pickle.dump([tr_census, tr_regions, tr_valid_data_mask, tY, tregid, tMasks, tregMasks, tBBox, feature_names], handle, protocol=pickle.HIGHEST_PROTOCOL)
    
    dim, h, w = tr_features.shape
    if not os.path.isfile(h5_filename):
        with h5py.File(h5_filename, "w") as f:
            h5_features = f.create_dataset("features", (1, dim, h, w), dtype=np.float32, fillvalue=0, chunks=(1,dim,512,512))
            for i,feat in enumerate(tqdm(tr_features)):
                h5_features[:,i] = feat


def prep_test_hdf5_file(validation_data, this_disaggregation_data, h5_filename,  var_filename, disag_filename):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    val_features, val_census, val_regions, val_map_valid_ids, val_guide_res, val_valid_data_mask, geo_metadata =  validation_data

    dim, h, w = val_features.shape

    if not os.path.isfile(h5_filename):
        with h5py.File(h5_filename, "w") as f:
            h5_features = f.create_dataset("features", (1, dim, h, w), dtype=np.float32, fillvalue=0, chunks=(1,dim,512,512))
            for i,feat in tqdm(enumerate(val_features)):
                h5_features[:,i] = feat
            
    with open(var_filename, 'wb') as handle:
        pickle.dump(
            [val_census, val_regions,
            val_map_valid_ids, val_guide_res, val_valid_data_mask,
            geo_metadata],
            handle, protocol=pickle.HIGHEST_PROTOCOL)
    
    with open(disag_filename, 'wb') as handle:
        pickle.dump(this_disaggregation_data,  handle, protocol=pickle.HIGHEST_PROTOCOL)
    
  


def build_variable_list(dataset: dict, var_list: list) -> list:
    """
    Selects the variables specified in var_list from the datset and returns them as a list of same order as var_list
    """
    outlist = []
    for var in var_list:
        outlist.append(dataset[var])
    return outlist


def superpixel_with_pix_data(
    train_dataset_name,
    train_level,
    test_dataset_name,
    optimizer,
    learning_rate,
    num_epochs,
    weights_regularizer,
    weights_regularizer_adamw, 
    memory_mode,
    log_step,
    random_seed,
    validation_split,
    validation_fold,
    weights,
    sampler,
    custom_sampler_weights,
    dropout,
    loss,
    admin_augment,
    population_target,
    load_state,
    eval_only,
    input_scaling,
    output_scaling,
    silent_mode,
    dataset_dir,
    max_step,
    eval_5fold,
    eval_feat_importance,
    grad_clip,
    lr_scheduler_step,
    lr_scheduler_gamma,
    small_net,
    e5f_metric,
    wandb_user,
    name,
    random_seed_folds,
    kernel_size,
    eval_model,
    full_ceval,
    remove_feat_idxs
    ):

    ####  define parameters  ########################################################

    params = {
            'weights_regularizer': weights_regularizer,
            'weights_regularizer_adamw': weights_regularizer_adamw,
            'kernel_size': kernel_size,
            'loss': loss,

            "admin_augment": admin_augment,
            "population_target": population_target,
            "load_state": load_state, # not maintained anymore?
            "Net": 'ScaleNet', 

            'optim': optimizer,
            'lr': learning_rate,
            "epochs": num_epochs,
            'logstep': log_step,
            'maxstep': max_step,
            'train_dataset_name': train_dataset_name,
            'train_level': train_level,
            'test_dataset_name': test_dataset_name,
            'input_variables': list(cfg.input_paths[train_dataset_name[0]].keys()),
            'memory_mode': memory_mode,
            'random_seed': random_seed,
            'validation_split': validation_split,
            'validation_fold': validation_fold,
            'weights': weights,
            'sampler': sampler,
            'custom_sampler_weights': custom_sampler_weights,
            'dropout': dropout,
            'input_scaling': input_scaling,
            'output_scaling': output_scaling,
            'silent_mode': silent_mode,
            'dataset_dir': dataset_dir,
            'eval_5fold': eval_5fold,
            'eval_feat_importance' : eval_feat_importance,
            'grad_clip': grad_clip,
            'lr_scheduler_step': lr_scheduler_step,
            'lr_scheduler_gamma': lr_scheduler_gamma,
            'small_net': small_net,
            'e5f_metric': e5f_metric,
            'name': name,
            'random_seed_folds': random_seed_folds,
            'eval_model': eval_model,
            'full_ceval': full_ceval,
            'remove_feat_idxs' : remove_feat_idxs
            }
   
    #building_features = ['buildings_merged', 'buildings_osm', 'buildings_merge'] # für die Schweiz
    building_features = ['buildings_count_2020_OSM', 'buildings_count_2020_TLM', 'buildings_merge']
    #building_features = ['buildings_count_2020_OSM']
    #building_features = ['buildings_count_2020_TLM']

    related_building_features = []
    
    cr_train_source_vars = ["features", "cr_census", "cr_regions", "guide_res", "valid_data_mask", "coarse", "feature_names"]
    
    fine_val_data_vars = ["features", "cr_census", "cr_regions",  "map_valid_ids", "guide_res",
                            "valid_data_mask", "geo_metadata"] #, "feature_names"]
    cr_disaggregation_data_vars = ["cr_census", "cr_regions"]

    wandb.init(project="HAC", entity=wandb_user, config=params, name=params["name"])

    # Fix all random seeds
    torch.manual_seed(random_seed)
    random.seed(random_seed)
    np.random.seed(random_seed)
    torch.cuda.manual_seed(random_seed)
    torch.cuda.manual_seed_all(random_seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    os.environ['PYTHONHASHSEED'] = str(random_seed)

    ####  load dataset  #############################################################

    assert(all(elem=="c" or elem=="f" or elem=="ac" for elem in train_level))

    datalocations = {} 
    test_but_not_train = list(set(test_dataset_name) - set(train_dataset_name) )
    all_dataset_names = train_dataset_name + test_but_not_train
    #train_level = pad_list(train_level, fill='f', target_len=len(all_dataset_names))   
    train_level = pad_list(train_level, fill='c', target_len=len(all_dataset_names))    
    params["memory_mode"] = pad_list(params["memory_mode"], fill='d', target_len=len(all_dataset_names))    
    params["weights"] = pad_list(params["weights"], fill=1., target_len=len(all_dataset_names))    
    params["custom_sampler_weights"] = pad_list(params["custom_sampler_weights"], fill=1., target_len=len(all_dataset_names))    

    for i,ds in enumerate(all_dataset_names):
        this_level = train_level[i]

        h5_filename = f"{dataset_dir}/{ds}/data.hdf5"
        train_var_filename_c = f"{dataset_dir}/{ds}/additional_train_vars_c.pkl"
        train_var_filename_f = f"{dataset_dir}/{ds}/additional_train_vars_f.pkl"
        eval_var_filename = f"{dataset_dir}/{ds}/additional_test_vars.pkl"
        eval_disag_filename = f"{dataset_dir}/{ds}/disag_vars.pkl"
        parent_dir = f"{dataset_dir}/{ds}/"
        print("h5_filename", h5_filename)

        if not (os.path.isfile(h5_filename) and os.path.isfile(train_var_filename_c) and os.path.isfile(eval_var_filename) and os.path.isfile(eval_disag_filename)): #and os.path.isfile(eval_disag_filename)):
            Path(parent_dir).mkdir(parents=True, exist_ok=True)

            this_dataset = get_dataset(ds, params, building_features, related_building_features) 
            prep_train_hdf5_file(build_variable_list(this_dataset, cr_train_source_vars), h5_filename, train_var_filename_c, silent_mode=silent_mode)
            
            # Build testdataset here to avoid dublicate executions later
            this_validation_data = build_variable_list(this_dataset, fine_val_data_vars)
            this_disaggregation_data = build_variable_list(this_dataset, cr_disaggregation_data_vars) 
            prep_test_hdf5_file(this_validation_data, this_disaggregation_data, h5_filename,  eval_var_filename, eval_disag_filename)
            
            # Free up RAM
            del this_disaggregation_data, this_validation_data
            del this_dataset 
        
        datalocations[ds] = {"features": h5_filename, "train_vars_f": train_var_filename_f, "train_vars_c": train_var_filename_c,
            "eval_vars": eval_var_filename, "disag": eval_disag_filename}

    if eval_5fold is None and eval_model is None:
        res = PixAdminTransform(#log_dict = PixAdminTransform(
            datalocations=datalocations,
            train_dataset_name=train_dataset_name,
            test_dataset_names=test_dataset_name,
            params=params, 
        )
    elif eval_model is not None:
        res, log_dict = EvalModel_PixAdminTransform(
            datalocations=datalocations,
            train_dataset_name=train_dataset_name,
            test_dataset_names=test_dataset_name,
            params=params, 
        )
    elif eval_5fold is not None and eval_feat_importance > 0:
        res, log_dict = Eval5Fold_FeatureImportance(
            datalocations=datalocations,
            train_dataset_name=train_dataset_name,
            test_dataset_names=test_dataset_name,
            params=params, 
        )
    else:
        res, log_dict = Eval5Fold_PixAdminTransform(
            datalocations=datalocations,
            train_dataset_name=train_dataset_name,
            test_dataset_names=test_dataset_name,
            params=params, 
        )

    # save as geoTIFF files
    save_files = True
    if save_files:
        for name in test_dataset_name:
            print("started saving files for", name)

            #Prepate the output folder
            dest_folder = '../../../viz/outputs/{}'.format(wandb.run.name)
            if not os.path.exists(dest_folder):
                os.makedirs(dest_folder)
            print("dest_folder {}".format(dest_folder))
            
            geo_metadata = geo_metadata_new


            predicted_target_img = res[name+'/predicted_target_img']
            #predicted_target_img_adjusted = res[name+'/predicted_target_img_adjusted']
            valid_data_mask = map_valid_ids_new > 0
            predicted_target_img[~valid_data_mask] = 1e-16
            scales = res[name+'/scales']

            if name+'/variances' in list(res.keys()):
                variances = res[name+'/variances']
                variances[~valid_data_mask]= np.nan

            scale_vars_available = False
            if scales.shape.__len__()==3:
                scale_vars = scales[1]
                scale_vars[~valid_data_mask]= np.nan
                scales = scales[0]
                scale_vars_available = True

            write_geolocated_image(predicted_target_img.numpy(), dest_folder+'/{}_predicted_target_img.tiff'.format(name),
                geo_metadata["geo_transform"], geo_metadata["projection"] )
           
            write_geolocated_image(scales.numpy(), dest_folder+'/{}_scales.tiff'.format(name),
                geo_metadata["geo_transform"], geo_metadata["projection"] )

            if name+'/variances' in list(res.keys()):
                write_geolocated_image( variances.numpy(), dest_folder+'/{}_variances.tiff'.format(name),
                    geo_metadata["geo_transform"], geo_metadata["projection"] )
            if scale_vars_available:
                write_geolocated_image( scale_vars.numpy(), dest_folder+'/{}_scale_variances.tiff'.format(name),
                    geo_metadata["geo_transform"], geo_metadata["projection"] )
            if name+'/id_map' in list(res.keys()):
                id_map = res[name+'/id_map']
                #id_map[~valid_data_mask]= np.nan
                write_geolocated_image( id_map.numpy(), dest_folder+'/{}_id_map.tiff'.format(name),
                    geo_metadata["geo_transform"], geo_metadata["projection"] )
            if name+'/fold_map' in list(res.keys()):
                fold_map = res[name+'/fold_map']
                #fold_map[~valid_data_mask]= np.nan
                write_geolocated_image( fold_map.numpy(), dest_folder+'/{}_fold_map.tiff'.format(name),
                    geo_metadata["geo_transform"], geo_metadata["projection"] )

    return


def pad_list(arg_list, fill, target_len):
    if fill is not None:
        arg_list.extend([fill]*(target_len- len(arg_list)))
    return arg_list

def unroll_arglist(arg_list, fill=None, target_len=None):
    arg_list = arg_list.split(",")
    return pad_list(arg_list, fill, target_len)

def main():
    parser = argparse.ArgumentParser()
    # parser.add_argument("preproc_data_path", type=str, help="Preprocessed data of regions (pickle file)")
    # parser.add_argument("rst_wp_regions_path", type=str,
                        # help="Raster of WorldPop administrative boundaries information") 
    parser.add_argument("--train_dataset_name", "-train", type=str, help="Train Dataset name (separated by commas)", required=True)
    parser.add_argument("--train_level", "-train_lvl", type=str,  default='c', help="ordered by --train_dataset_name [f:finest, c: coarser level] (separated by commas) ")
    parser.add_argument("--test_dataset_name", "-test", type=str, help="Test Dataset name (separated by commas)", required=True)
    parser.add_argument("--eval_5fold", "-e5f", type=str, default=None, help="Evaluates 5 fold cross with the 5 pretrained models specified in a comma sparated list. \
                            Example: '-e5f fine-shape-1418,morning-blaze-1415,volcanic-shadow-1416,devoted-snowball-1417,eternal-donkey-1419', for the folds 0,1,2,3,4 respectively")
    parser.add_argument("--eval_model", "-em", type=str, default=None, help="Evaluates the model on the specified test dataset(s).")
    parser.add_argument("--eval_feat_importance", "-efi", type=int, default=0, help="Evaluates feature importance give as a parameter the number of permutations to perform")

    parser.add_argument("--sampler", "-sap", type=str, default=None, help="Options: natural (not recommended yet), custom (see --custom_sampler_weights), <blank> (no sampler)")
    parser.add_argument("--custom_sampler_weights", "-csw", type=str,  default='1', help="ordered by --train_dataset_name weight for the sampler (separated by commas) ")

    parser.add_argument("--optimizer", "-optim", type=str, default="adam", help="adam, adamw ")
    parser.add_argument("--loss", "-l", type=str, default="NormL1", help="NormL1, NormL2, gaussNLL, laplaceNLL")
    parser.add_argument("--train_weight", "-train_w", type=str,  default='1', help="ordered by --train_dataset_name weighting of the samples in the datasets (separated by commas) ") # war auf 1
    parser.add_argument("--learning_rate", "-lr", type=float, default=0.00001, help=" ")
    parser.add_argument("--grad_clip", "-gc", type=float, default=10., help="Gradient norm clipping value")
    parser.add_argument("--lr_scheduler_step", "-lrs", type=float, default=np.inf, help="How many interations until LR is reduced to 10%.")
    parser.add_argument("--lr_scheduler_gamma", "-lrg", type=float, default=0.5, help="How many interations until LR is reduced to 10%.")
    parser.add_argument("--weights_regularizer", "-wr", type=float, default=0., help=" ")
    parser.add_argument("--weights_regularizer_adamw", "-adamwr", type=float, default=0.001, help=" ")
    parser.add_argument("--dropout", "-drop", type=float, default=0.0, help="dropout probability ")
    parser.add_argument("--small_net", "-sn", type=bool, default=False, help="Using small variant.")
    parser.add_argument("--kernel_size", "-ks", type=str, default="1,1,1,1", help="Commaseperated list of integer kernel sizes with size 4.")

    parser.add_argument("--memory_mode", "-mm", type=str, default='m', help="Loads the variables into memory to speed up the training process. Obviously: Needs more memory! m:load into memory; d: load from a hdf5 file on disk. (separated by commas)")
    parser.add_argument("--log_step", "-lstep", type=float, default=2000, help="Evealuate the model after 'logstep' batchiterations.")
    parser.add_argument("--max_step", "-mstep", type=float, default=np.inf, help="Evealuate the model after 'logstep' batchiterations.")

    parser.add_argument("--validation_split", "-vs", type=float, default=0.2, help="Evaluate the model after 'logstep' batchiterations.")
    parser.add_argument("--validation_fold", "-fold", type=int, default=None, help="Validation fold. One of [0,1,2,3,4]. When used --validation_split is ignored.")
    parser.add_argument("--random_seed", "-rs", type=int, default=1610, help="Random seed for this run. This does not (!) affect the random split of the validation/heldout/test-fold.")
    parser.add_argument("--random_seed_folds", "-rsf", type=int, default=1610, help=" This does only affect the random split of the validation/heldout/test-fold.")
    parser.add_argument("--full_ceval", type=lambda x: bool(strtobool(x)), default=True, help="Doing full evaluation during training?")

    parser.add_argument("--load_state", "-load", type=str, default=None, help="Loading from a specific state. Attention: 5fold evaluation not implmented yet!")
    parser.add_argument("--eval_only", "-eval", type=bool, default=False, help="Just evaluate the model and save results. Attention: 5fold evaluation not implmented yet! ")

    parser.add_argument("--input_scaling", "-is", type=bool, default=False, help="Countrywise input feature scaling.") 
    parser.add_argument("--output_scaling", "-os", type=bool, default=False, help="Countrywise output scaling.") 

    parser.add_argument("--silent_mode", "-silent", type=bool, default=False, help="Surpresses tqdm output mostly")
    parser.add_argument("--dataset_dir", "-dd", type=str, default='datasets', help="Directory of the hdf5 files")
    
    parser.add_argument("--e5f_metric", "-e5fmt", type=str, default="final", help="metric final, best_r2, best_mae, best_mape")
    
    parser.add_argument("--admin_augment", "-adm_aug", type=lambda x: bool(strtobool(x)), default=True, help="Use data augmentation by merging administrative regions")
    
    parser.add_argument("--population_target", "-pop_target", type=lambda x: bool(strtobool(x)), default=False, help="Use population as target") 
    
    parser.add_argument("--num_epochs", "-ep", type=int, default=2000, help="Number of epochs") # changed this
    
    parser.add_argument("--wandb_user", "-wandbu", type=str, default="lucadominiak", help="Wandb username")
    parser.add_argument("--name", type=str, default=None, help="short name for the run to identify it")
    
    parser.add_argument("--remove_feat_idxs", "-rmfi", type=str, default=None, help="Comaseparated list of indexes of features to be removed")

    args = parser.parse_args()  


    # check arguments and fill with default values
    args.train_dataset_name = unroll_arglist(args.train_dataset_name)
    args.train_level = unroll_arglist(args.train_level, 'c', len(args.train_dataset_name))
    args.test_dataset_name = unroll_arglist(args.test_dataset_name)
    args.memory_mode = unroll_arglist(args.memory_mode, 'm', len(args.train_dataset_name))
    if args.eval_5fold is not None: 
        args.eval_5fold = unroll_arglist(args.eval_5fold)
        if args.eval_5fold.__len__()!=5:
            raise Exception("Argument eval_5fold must have comma separated 5 elements!")
    

    args.train_weight = unroll_arglist(args.train_weight, '1', len(args.train_dataset_name))
    args.train_weight = [ float(el) for el in args.train_weight ]
    args.train_weight =  [ el/sum(args.train_weight) for el in args.train_weight ]

    args.custom_sampler_weights = unroll_arglist(args.custom_sampler_weights, '1', len(args.train_dataset_name))
    args.custom_sampler_weights = [ float(el) for el in args.custom_sampler_weights ]
    args.custom_sampler_weights =  [ el/sum(args.custom_sampler_weights) for el in args.custom_sampler_weights ]

    args.kernel_size = unroll_arglist(args.kernel_size, '1', 4)
    args.kernel_size = [ int(el) for el in args.kernel_size ] 

    if args.remove_feat_idxs is not None:
        args.remove_feat_idxs = [int(el) for el in args.remove_feat_idxs.split(",") ] 

    import gc
    for obj in gc.get_objects():   # Browse through ALL objects
        if isinstance(obj, h5py.File):   # Just HDF5 files
            try:
                obj.close()
            except:
                pass # Was already closed

    superpixel_with_pix_data( 
        args.train_dataset_name,
        args.train_level,
        args.test_dataset_name,
        args.optimizer,
        args.learning_rate,
        args.num_epochs,
        args.weights_regularizer,
        args.weights_regularizer_adamw,
        args.memory_mode,
        args.log_step,
        args.random_seed,
        args.validation_split,
        args.validation_fold,
        args.train_weight,
        args.sampler,
        args.custom_sampler_weights,
        args.dropout,
        args.loss,
        args.admin_augment,
        args.population_target,
        args.load_state,
        args.eval_only,
        args.input_scaling,
        args.output_scaling,
        args.silent_mode,
        args.dataset_dir,
        args.max_step,
        args.eval_5fold,
        args.eval_feat_importance,
        args.grad_clip,
        args.lr_scheduler_step,
        args.lr_scheduler_gamma,
        args.small_net,
        args.e5f_metric, 
        args.wandb_user,
        args.name,
        args.random_seed_folds,
        args.kernel_size,
        args.eval_model,
        args.full_ceval,
        args.remove_feat_idxs
    )


if __name__ == "__main__":
    main()
