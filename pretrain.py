
#import optuna
from train.nodal_snap_step import run_nodalosssnap, run_one_epoch
from evaluator.evaluate import run_fourcast, run_fourcast_during_training
from utils.tools import distributed_initial
from mltool.visualization import *
from mltool.dataaccelerate import DataLoader,DataSimfetcher
from mltool.loggingsystem import LoggingSystem

from pathlib import Path
import numpy as np
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data.distributed import DistributedSampler
from utils.tools import  find_free_port
import timm.optim
from timm.scheduler import create_scheduler
import torch.distributed as dist

from model.patch_model import *
from model.time_embeding_model import *
from model.physics_model import *
from model.othermodels import *
from model.FeaturePickModel import *
from model.GraphCast import *  
from criterions.criterions import *

from utils.params import get_args
from utils.tools import save_state, load_model, get_local_rank
import pandas as pd


from model.GradientModifier import *
from dataset.cephdataset import *
Datafetcher = DataSimfetcher

import random
import traceback
import time


#########################################
############# main script ###############
#########################################



def fast_set_model_epoch(model, criterion, dataloader, optimizer, loss_scaler, epoch, args, **kargs):
    if hasattr(model, 'set_epoch'):
        model.set_epoch(**kargs)
    if hasattr(model, 'module') and hasattr(model.module, 'set_epoch'):
        model.module.set_epoch(**kargs)
    if args.distributed and args.data_epoch_shuffle:
        dataloader.sampler.set_epoch(epoch)

def step_the_scheduler(lr_scheduler, now_lr, epoch, args):
    freeze_learning_rate = (args.scheduler_min_lr and now_lr < args.scheduler_min_lr)  and (args.scheduler_inital_epochs and epoch > args.scheduler_inital_epochs)
    if (not args.more_epoch_train) and (lr_scheduler is not None) and not freeze_learning_rate:lr_scheduler.step(epoch)

def update_and_save_training_status(args, epoch, loss_information, training_system):
    if get_local_rank():return 
    ### loss_information:
    ## {'train_loss': {'now':0, 'best':0, 'save_path':.....},
    ##  'valid_loss': {'now':0, 'best':0, 'save_path':.....},
    ##  'test_loss' : {'now':0, 'best':0, 'save_path':.....}}
    
    logsys = args.logsys
    logsys.metric_dict.update({'valid_loss':loss_information['valid_loss']['now']},epoch)
    logsys.banner_show(epoch,args.SAVE_PATH,train_losses=[loss_information['train_loss']['now']])

    
    for loss_type in ['valid_loss', 'test_loss']:
        now_loss = loss_information[loss_type]['now']
        if now_loss is None:continue
        best_loss = loss_information[loss_type]['best']
        save_path = loss_information[loss_type]['save_path']
        ####### save best valid model #########
        if now_loss < best_loss or best_loss is None:
            loss_information[loss_type]['best'] = best_loss = now_loss
            if epoch > args.epochs//10:
                logsys.info(f"saving best model for {loss_type}....",show=False)
                performance = dict([(name, val['best']) for name, val in loss_information])
                save_state(epoch=epoch, path=save_path, only_model=True, performance=performance, **training_system)
                logsys.info(f"done;",show=False)
            logsys.info(f"The best {loss_type} is {best_loss}", show=False)
            logsys.record(f'best_{loss_type}', best_loss, epoch, epoch_flag='epoch')
        

    ###### save runtime checkpoints #########
    #update_experiment_info(experiment_hub_path,epoch,args)
    loss_type = 'train_loss'
    now_loss = loss_information[loss_type]['now']
    best_loss = loss_information[loss_type]['best']
    save_path = loss_information[loss_type]['save_path']
    loss_information[loss_type]['best'] = loss_information[loss_type]['now']
    save_path = loss_information[loss_type]['save_path']
    if ((epoch>=args.save_warm_up) and (epoch%args.save_every_epoch==0)) or (epoch==args.epochs-1) or (epoch in args.epoch_save_list):
        logsys.info(f"saving latest model ....", show=False)
        save_model(epoch=epoch, step=0, path=save_path, only_model=False, performance=performance, **training_system)
        logsys.info(f"done ....",show=False)
        if epoch in args.epoch_save_list:
            save_model(epoch=epoch, path=save_path, path=f'{save_path}-epoch{epoch}', only_model=True, performance=performance, **training_system)
    return loss_information
    

from configs.arguments import parse_default_args, get_ckpt_path,create_logsys
from model.get_resource import build_model_and_optimizer
from dataset.utils import get_test_dataset,get_train_and_valid_dataset
def main_worker(local_rank, ngpus_per_node, args,
                result_tensor=None,
                train_dataset_tensor=None,train_record_load=None,
                valid_dataset_tensor=None,valid_record_load=None):
    """
    xxxx_dataset_tensor used for shared-in-memory dataset among DDP
    """
    if local_rank==0:print(f"we are at mode={args.mode}")
    ##### locate the checkpoint dir ###########
    args.gpu       = args.local_rank = gpu  = local_rank
    ##### parse args: dataset_kargs / model_kargs / train_kargs  ###########
    args            = parse_default_args(args)
    SAVE_PATH       = get_ckpt_path(args)
    args.SAVE_PATH  = str(SAVE_PATH)
    ########## inital log ###################
    logsys = create_logsys(args)
    

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            # For multiprocessing distributed training, rank needs to be the
            # global rank among all the processes
            args.rank = args.rank * ngpus_per_node + local_rank
        logsys.info(f"start init_process_group,backend={args.dist_backend}, init_method={args.dist_url},world_size={args.world_size}, rank={args.rank}")
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,world_size=args.world_size, rank=args.rank)


    model, optimizer, lr_scheduler, criterion, loss_scaler = build_model_and_optimizer(args)
    
    # =======================> start training <==========================
    logsys.info(f"entering {args.mode} training in {next(model.parameters()).device}")
    
    if args.mode=='fourcast':
        test_dataset,  test_dataloader = get_test_dataset(args,test_dataset_tensor=train_dataset_tensor,test_record_load=train_record_load)
        run_fourcast(args, model,logsys,test_dataloader)
        return logsys.close()
    elif args.mode=='fourcast_for_snap_nodal_loss':
        test_dataset,  test_dataloader = get_test_dataset(args,test_dataset_tensor=train_dataset_tensor,test_record_load=train_record_load)
        run_nodalosssnap(args, model,logsys,test_dataloader)
        return logsys.close()
    
    ####### Training Stage #########
    
    test_dataloader        = None
    start_step             = args.start_step
    

    train_dataset, val_dataset, train_dataloader,val_dataloader = get_train_and_valid_dataset(args,
                    train_dataset_tensor=train_dataset_tensor,train_record_load=train_record_load,
                    valid_dataset_tensor=valid_dataset_tensor,valid_record_load=valid_record_load)
    test_dataloader = None 


    logsys.info(f"use dataset ==> {train_dataset.__class__.__name__}")
    logsys.info(f"Start training for {args.epochs} epochs")
    
    master_bar = logsys.create_master_bar(np.arange(-1, args.epochs))
    accu_list = ['valid_loss']
    metric_dict = logsys.initial_metric_dict(accu_list)
    banner = logsys.banner_initial(args.epochs, args.SAVE_PATH)
    logsys.banner_show(0, args.SAVE_PATH)

    if args.tracemodel:logsys.wandb_watch(model,log_freq=100)


    loss_information = {'train_loss': {'now':None,'best':np.inf, 'save_path':os.path.join(args.SAVE_PATH,'pretrain_latest.pt')},
                        'test_loss' : {'now': None, 'best': np.inf, 'save_path': os.path.join(args.SAVE_PATH, 'fourcast.best.pt')},
                        'valid_loss': {'now': None, 'best': args.min_loss, 'save_path': os.path.join(args.SAVE_PATH, 'backbone.best.pt')},
                        }
    for epoch in master_bar:
        if epoch < start_epoch:continue

        ## skip fisrt training, and the epoch should be -1
        if epoch >=0: ###### main training loop #########
            fast_set_model_epoch(model, criterion, train_dataloader, optimizer, loss_scaler,epoch, args, epoch_total=args.epochs, eval_mode=False)
            train_loss = run_one_epoch(epoch, start_step, model, criterion, train_dataloader, optimizer, loss_scaler,logsys,'train')
            logsys.record('train', train_loss, epoch, epoch_flag='epoch')
            loss_information['train_loss']['now'] = train_loss

            learning_rate = optimizer.param_groups[0]['lr']
            logsys.record('learning rate',learning_rate,epoch, epoch_flag='epoch')
            step_the_scheduler(lr_scheduler, optimizer.param_groups[0]['lr'], epoch, args)  # make sure the scheduler is load from state

        
        if args.fourcast_during_train and (epoch % args.fourcast_during_train == 0 or (epoch == -1 and args.force_do_first_fourcast) or (epoch == args.epoch - 1)):
            fast_set_model_epoch(model,criterion, val_dataloader, optimizer, loss_scaler,epoch, args, epoch_total=args.epochs,eval_mode=True)
            test_loss, test_dataloader = run_fourcast_during_training(args, epoch, logsys, model, test_dataloader)  # will
            logsys.record('test', test_loss, epoch, epoch_flag='epoch') 
            
        if args.valid_every_epoch and (epoch % args.valid_every_epoch == 0 or (epoch == -1 and not args.skip_first_valid) or (epoch == args.epoch - 1)):
            fast_set_model_epoch(model,criterion, val_dataloader, optimizer, loss_scaler,epoch, args, epoch_total=args.epochs,eval_mode=True)
            val_loss   = run_one_epoch(epoch, start_step, model, criterion, val_dataloader, optimizer, loss_scaler,logsys,'valid')
            logsys.record('valid', val_loss, epoch, epoch_flag='epoch')

        loss_information = update_and_save_training_status(args, epoch, loss_information, {'model':model, 'criterion':criterion, 
                                                                                           'optimizer':optimizer,'loss_scaler':loss_scaler})
        logsys.info(f"Epoch {epoch} | Train loss: {train_loss:.6f}, Val loss: {val_loss:.6f}", show=False)
        
    best_valid_ckpt_path = loss_information['valid_loss']['save_path']
    if os.path.exists(best_valid_ckpt_path) and args.do_final_fourcast:
        logsys.info(f"we finish training, then start test on the best checkpoint {best_valid_ckpt_path}")
        args.mode = 'fourcast'
        args.time_step = 22
        start_epoch, start_step, min_loss = load_model(model.module if args.distributed else model, path=best_valid_ckpt_path, only_model=True, loc='cuda:{}'.format(args.gpu))
        run_fourcast(args, model,logsys)
        
    if result_tensor is not None and local_rank==0:result_tensor[local_rank] = min_loss
    return logsys.close()


def create_memory_templete(args):
    train_dataset_tensor = valid_dataset_tensor = train_record_load = valid_record_load = None
    if args.use_inmemory_dataset:
        assert args.dataset_type
        print("======== loading data as shared memory==========")
        if not ('fourcast' in args.mode):
            print(f"create training dataset template, .....")
            train_dataset_tensor, train_record_load = eval(args.dataset_type).create_offline_dataset_templete(split='train' if not args.debug else 'test',
                                                                                                              root=args.data_root, use_offline_data=args.use_offline_data, dataset_flag=args.dataset_flag)
            train_dataset_tensor = train_dataset_tensor.share_memory_()
            train_record_load = train_record_load.share_memory_()
            print(
                f"done! -> train template shape={train_dataset_tensor.shape}")

            print(f"create validing dataset template, .....")
            valid_dataset_tensor, valid_record_load = eval(args.dataset_type).create_offline_dataset_templete(split='valid' if not args.debug else 'test',
                                                                                                              root=args.data_root, use_offline_data=args.use_offline_data, dataset_flag=args.dataset_flag)
            valid_dataset_tensor = valid_dataset_tensor.share_memory_()
            valid_record_load = valid_record_load.share_memory_()
            print(
                f"done! -> train template shape={valid_dataset_tensor.shape}")
        else:
            print(f"create testing dataset template, .....")
            train_dataset_tensor, train_record_load = eval(args.dataset_type).create_offline_dataset_templete(split='test',
                                                                                                              root=args.data_root, use_offline_data=args.use_offline_data, dataset_flag=args.dataset_flag)
            train_dataset_tensor = train_dataset_tensor.share_memory_()
            train_record_load = train_record_load.share_memory_()
            print(f"done! -> test template shape={train_dataset_tensor.shape}")
            valid_dataset_tensor = valid_record_load = None
        print("========      done        ==========")
    return train_dataset_tensor, valid_dataset_tensor, train_record_load, valid_record_load

def distributing_main(args=None):
    
    if args is None:args = get_args()
    
    args = distributed_initial(args)
    train_dataset_tensor,valid_dataset_tensor,train_record_load,valid_record_load = create_memory_templete(args)
    result_tensor = torch.zeros(1).share_memory_()
    if args.multiprocessing_distributed:
        print("======== entering  multiprocessing train ==========")
        args.world_size = args.ngpus_per_node * args.world_size
        torch.multiprocessing.spawn(main_worker, nprocs=args.ngpus_per_node, args=(args.ngpus_per_node, args,result_tensor,
                                    train_dataset_tensor,train_record_load,
                                    valid_dataset_tensor,valid_record_load))
    else:
        print("======== entering  single gpu train ==========")
        main_worker(0, args.ngpus_per_node, args,result_tensor, train_dataset_tensor,train_record_load,valid_dataset_tensor,valid_record_load)
    return result_tensor


if __name__ == '__main__':
    main()
