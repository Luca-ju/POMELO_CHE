import numpy as np
from numpy.core.numeric import zeros_like
import os
import logging
logging.basicConfig( format='%(asctime)s %(levelname)-8s %(message)s', level=logging.INFO, datefmt='%Y-%m-%d %H:%M:%S')

import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data 
#from torch.utils.data import Subset
import sys
import wandb
import h5py
import pickle
from pathlib import Path
import random

from utils_copy_Luca import my_mean_absolute_error, mean_absolute_percentage_error, plot_2dmatrix, accumulate_values_by_region, compute_performance_metrics, bbox2, \
     PatchDataset, MultiPatchDataset, NormL1, LogL1, LogL2, LogoutputL1, LogoutputL2, compute_performance_metrics_arrays, myMSEloss
from cy_utils import compute_map_with_new_labels, compute_accumulated_values_by_region, compute_disagg_weights, \
    set_value_for_each_region
# from pix_transform_utils.utils import upsample

from sklearn.metrics import r2_score, mean_absolute_error, mean_squared_error

from pix_transform_CHE.pix_transform_net import PixTransformNet, PixScaleNet

from bayesian_dl.loss import GaussianNLLLoss, LaplacianNLLLoss

from pix_transform_CHE.evaluation import disag_map, disag_wo_map, disag_and_eval_map, eval_my_model, checkpoint_model, log_scales

if 'ipykernel' in sys.modules:
    from tqdm import tqdm_notebook as tqdm
else:
    from tqdm import tqdm as tqdm


def PixAdminTransform(
    datalocations,
    train_dataset_name,
    test_dataset_names,
    params):

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    #### prepare Dataset #########################################################################
    # unique_datasets = set(list(validation_data.keys()) + list(training_source.keys()))

    #if params["admin_augment"]:
    # WICHTIG
    dataset = MultiPatchDataset(datalocations, train_dataset_name, params["train_level"], params['memory_mode'], device, 
        params["validation_split"], params["validation_fold"], params["weights"], params["custom_sampler_weights"], 
        random_seed_folds=params["random_seed_folds"], build_pairs=params["admin_augment"], remove_feat_idxs=params["remove_feat_idxs"])
    #else:
    #    raise Exception("option not available")
    #    dataset = PatchDataset(training_source, params['memory_mode'], device, params["validation_split"])

    # Fix all random seeds
    torch.manual_seed(params["random_seed"])
    random.seed(params["random_seed"])
    np.random.seed(params["random_seed"])
    torch.cuda.manual_seed(params["random_seed"])
    torch.cuda.manual_seed_all(params["random_seed"])
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    os.environ['PYTHONHASHSEED'] = str(params["random_seed"])

    # IGNORE
    if params["sampler"] in ['custom', 'natural']:
        weights = dataset.all_natural_weights if params["sampler"]=="natural" else dataset.custom_sampler_weights
        sampler = torch.utils.data.WeightedRandomSampler(weights, len(weights), replacement=False)
        shuffle = False
    else:
        logging.info(f'Using no weighted sampler') 
        sampler = None
        shuffle = True

    #train_subset = Subset(dataset, [i for i in range(4)])
    
    train_loader = torch.utils.data.DataLoader(dataset, batch_size=1, shuffle=shuffle, sampler=sampler, num_workers=0) 

    #### setup loss/network ############################################################################

    if params['loss'] == 'mse':
        myloss = torch.nn.MSELoss()
        myloss = myMSEloss
    elif params['loss'] == 'l1':
        myloss = torch.nn.L1Loss()
    elif params['loss'] == 'NormL1':
        myloss = NormL1
    elif params['loss'] == 'LogL1':
        myloss = LogL1 # Standard
    elif params['loss'] == 'LogL2':
        myloss = LogL2
    elif params['loss'] == 'LogoutputL1':
        myloss = LogoutputL1
    elif params['loss'] == 'LogoutputL2':
        myloss = LogoutputL2
    elif params['loss'] == 'gaussNLL':
        myloss = GaussianNLLLoss(max_clamp=20.)
    elif params['loss'] == 'laplaceNLL':
        myloss = LaplacianNLLLoss(max_clamp=20.)
    else:
        raise Exception("unknown loss!")
    
    
        
    if params['Net']=='PixNet':
        # IGNORE
        mynet = PixTransformNet(channels_in=dataset.num_feats(),
                                weights_regularizer=params['weights_regularizer'],
                                device=device).train().to(device)
        
    

    elif params['Net']=='ScaleNet':
        # WICHTIG
        mynet = PixScaleNet(channels_in=dataset.num_feats(),
                        weights_regularizer=params['weights_regularizer'],
                        device=device, loss=params['loss'], kernel_size=params['kernel_size'],
                        dropout=params["dropout"],
                        input_scaling=params["input_scaling"], output_scaling=params["output_scaling"],
                        datanames=train_dataset_name, small_net=params["small_net"], pop_target=params["population_target"]
                        ).train().to(device)
        
    
    
    #Optimizer
    if params["optim"]=="adam":
        optimizer = optim.Adam(mynet.params_with_regularizer, lr=params['lr'])
    elif params["optim"]=="adamw":
        optimizer = optim.AdamW(mynet.params_with_regularizer, lr=params['lr'], weight_decay=params["weights_regularizer_adamw"])
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=params["lr_scheduler_step"], gamma=params["lr_scheduler_gamma"])

    # Load from state
    if params["load_state"] is not None:
        # checkpoint = torch.load('checkpoints/best_mape/{}/VAL/{}.pth'.format(test_dataset_names[0], params["load_state"]))
        checkpoint = torch.load('checkpoints/Final/Maxstepstate_{}.pth'.format(params["load_state"]))
        mynet.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    # wandb.watch(mynet)
    
    #### train network ############################################################################

    epochs = params["epochs"]
    itercounter = 0
    batchiter = 0

    # initialize the best score variables
    best_scores, best_val_scores = {}, {}
    for test_dataset_name in test_dataset_names:
        best_scores[test_dataset_name] = [-1e12, 1e12, 1e12, -1e12, 1e12, 1e12]
        best_val_scores[test_dataset_name] = [-1e12, 1e12, 1e12, -1e12, 1e12, 1e12]
        best_val_scores_avg = [-1e12, 1e12, 1e12, -1e12, 1e12, 1e12]

    with tqdm(range(0, epochs), leave=True, disable=params["silent_mode"]) as tnr:
        for epoch in tnr:
            
            pred_list = []
            gt_list = []
           
            for sample in tqdm(train_loader, disable=params["silent_mode"]):
                
                optimizer.zero_grad()
                
                # Feed forward the network
                y_pred_list = mynet.forward_one_or_more(sample)
                
                #check if any valid values are there, else skip   
                if y_pred_list is None:
                    continue

                # Sum over the census data per patch 
                y_pred = torch.stack([pred*samp[4] for pred,samp in zip(y_pred_list, sample)]).sum(0)
                y_gt = torch.tensor([samp[1]*samp[4] for samp in sample]).sum().unsqueeze(0) 

                # Backwards
                loss = myloss(y_pred, y_gt)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(mynet.parameters(), params["grad_clip"])
                optimizer.step()
                scheduler.step()

                pred_list.append(y_pred.detach())
                gt_list.append(y_gt.detach())
                
                # train logging
                train_log_dict = {}
                if batchiter % 50 == 0:
                    if len(y_pred)==2:
                        train_log_dict["train/y_pred_"] = y_pred[0]
                        train_log_dict["train/y_var"] = y_pred[1]
                    else:
                        train_log_dict["train/y_pred"] = y_pred
                    train_log_dict["train/y_gt"] = y_gt
                    train_log_dict['train/loss'] = loss 
                    train_log_dict['epoch'] = epoch 
                    train_log_dict['batchiter'] = batchiter
                    train_log_dict['current_lr'] = optimizer.param_groups[0]["lr"]


                    pred_tensor = torch.cat(pred_list)
                    gt_tensor = torch.cat(gt_list)

                    # Convert the tensors to numpy arrays
                    pred_array = pred_tensor.detach().cpu().numpy()
                    gt_array = gt_tensor.detach().cpu().numpy()

                    if np.var(gt_array) == 0:
                        r2 = 0.0
                    else:
                        metrics = compute_performance_metrics_arrays(pred_array, gt_array)

                        r2 = metrics["r2"]
                        mae = metrics["mae"]
                        mse = metrics["mse"]
                        mape = metrics["mape"]

                        # Add the metrics to the logging dictionary
                        train_log_dict[train_dataset_name[0]+'/'+'r2'] = r2
                        train_log_dict[train_dataset_name[0]+'/'+'mse'] = mse
                        train_log_dict[train_dataset_name[0]+'/'+'mape'] = mape
                        train_log_dict[train_dataset_name[0]+'/'+'mae'] = mae
                        
                        pred_list.clear()
                        gt_list.clear()

                        # Log the dictionary
                        wandb.log(train_log_dict)

                itercounter += 1
                batchiter += 1


                if mynet.output_scaling:
                    mynet.normalize_out_scales()

                torch.cuda.empty_cache()

                if itercounter>=( params['logstep'] ):
                    itercounter = 0

                    with torch.no_grad(): ### nach einem log step wird das model validiert
                        # Validate and Test the model and save model
                        log_dict = {}

                        # Validation
                        if params["validation_split"]>0. or (params["validation_fold"] is not None):
                            this_val_scores_avg, n = np.zeros((3,)), 0
                            for name in test_dataset_names:
                                logging.info(f'Validating dataset of {name}')
                                agg_preds,val_census = [],[]
                                agg_preds_arr = torch.zeros((dataset.max_tregid[name]+1,))

                                for idx in tqdm(range(len(dataset.Ys_val[name])), disable=params["silent_mode"]):
                                    X, Y, Mask, name, census_id = dataset.get_single_validation_item(idx, name) 
                                    pred = mynet.forward(X, Mask, name=name, forward_only=True).detach().cpu().numpy()
                                    #predx = mynet.forward(X, Mask, name=name, forward_only=True, predict_map=True) # for debugging
                                    agg_preds.append(pred)
                                    val_census.append(Y.cpu().numpy())
                                    if isinstance(pred, np.ndarray) and pred.shape.__len__()==1:
                                        pred = pred[0] 
                                    agg_preds_arr[census_id.item()] = pred.item()
                                    torch.cuda.empty_cache()

                                metrics = compute_performance_metrics_arrays(np.asarray(agg_preds), np.asarray(val_census)) 
                                # best_val_scores[name] = checkpoint_model(mynet, optimizer.state_dict(), epoch, metrics, '/'+name+'/VAL/', best_val_scores[name])
                                if name in train_dataset_name:
                                    this_val_scores_avg += [metrics["r2"], metrics["mae"],  metrics["mape"]]
                                    n += 1

                                for key in metrics.keys():
                                    log_dict[name + '/validation/' + key ] = metrics[key]
                                    
                                
                                best_val_scores[name] = checkpoint_model(mynet, optimizer.state_dict(), epoch, metrics, '/'+name+'/VAL/', best_val_scores[name])
                                
                                torch.cuda.empty_cache()

                            avg_metrics = {}
                            avg_metrics["r2"], avg_metrics["mae"],  avg_metrics["mape"] = this_val_scores_avg/n
                            best_val_scores_avg = checkpoint_model(mynet, optimizer.state_dict(), epoch, avg_metrics, '/AVG/VAL/', best_val_scores_avg)
                            for key,value in avg_metrics.items():
                                log_dict["validation/average/"+key] = value  
                        
                        # Evaluation Model: Evaluates the training and validation regions at the same time!
                        
                        if params["full_ceval"]:
                            
                            for name in test_dataset_names: 
                                logging.info(f'Testing dataset of {name}')
                                
                                val_features = dataset.features[name]
                                
                                
                                res = eval_my_model(
                                    mynet, val_features, 
                                    val_census,
                                    dataset=dataset,
                                    dataset_name=name, return_scale=True, silent_mode=params["silent_mode"], full_eval=True
                                )
                                
                                torch.cuda.empty_cache()
                          
                    #log scales
                    log_dict = log_scales(mynet, list(datalocations.keys()), dataset, log_dict)

                    # log_dict['train/loss'] = loss 
                    log_dict['batchiter'] = batchiter
                    log_dict['epoch'] = epoch

                    # if val_fine_map is not None:
                    tnr.set_postfix(R2=log_dict[test_dataset_names[-1]+'/validation/r2'],
                                    zMAEc=log_dict[test_dataset_names[-1]+'/validation/mae'])
                    wandb.log(log_dict)
                        
                    mynet.train() 
                    torch.cuda.empty_cache()

                    if batchiter>=params["maxstep"]:
                        maxstep_reached = True
                        break
            else:
                # Continue if the inner loop was not broken.
                continue
            break

    # compute final prediction, un-normalize, and back to numpy

    with torch.no_grad():
        mynet.eval()
        
        Path('checkpoints/{}'.format('Final')).mkdir(parents=True, exist_ok=True) 

        saved_dict = {'model_state_dict': mynet.state_dict(), 'optimizer_state_dict': optimizer.state_dict(), 'epoch': epoch, 'log_dict': log_dict}
        if mynet.input_scaling:
            saved_dict["input_scales_bias"] = [mynet.in_scale, mynet.in_bias]
        if mynet.output_scaling:
            saved_dict["output_scales_bias"] = [mynet.out_scale, mynet.out_bias] 

        torch.save(saved_dict,
            'checkpoints/{}{}.pth'.format('Final/Maxstepstate_', wandb.run.name) )
        torch.cuda.empty_cache()

        # Validate and Test the model and save model
        log_dict = {}
        res_dict = {}

        # Evaluation Model
        # for test_dataset_name, values in validation_data.items():
        for name in test_dataset_names: 

            logging.info(f'Testing dataset of {name}')
            
            val_features = dataset.features[name]
            
            #res, this_log_dict = eval_my_model(
            res = eval_my_model(
                mynet, val_features,
                val_census,
                dataset=dataset,
                dataset_name=name, return_scale=True, silent_mode=params["silent_mode"], full_eval=True
            )

            # Model log collection
            for key in res.keys():
                res_dict[name+'/'+key] = res[key]
        
    return res_dict#, log_dict 
    

