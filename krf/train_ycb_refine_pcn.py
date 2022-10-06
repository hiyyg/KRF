from __future__ import (
    division,
    absolute_import,
    with_statement,
    print_function,
    unicode_literals,
)
import os
import time
import tqdm
import shutil
import argparse
import resource
import numpy as np
import cv2
import pickle as pkl
from collections import namedtuple

import torch
import torch.optim as optim
import torch.optim.lr_scheduler as lr_sched
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CyclicLR
import torch.backends.cudnn as cudnn
from tensorboardX import SummaryWriter

from utils.basic_utils import Basic_Utils
from common import Config, ConfigRandLA

config = Config()
bs_utils = Basic_Utils(config)
writer = SummaryWriter(log_dir=config.log_traininfo_dir)

rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (30000, rlimit[1]))

color_lst = [(0, 0, 0)]
for i in range(config.n_objects):
    col_mul = (255 * 255 * 255) // (i+1)
    color = (col_mul//(255*255), (col_mul//255) % 255, col_mul % 255)
    color_lst.append(color)


parser = argparse.ArgumentParser(description="Arg parser")
parser.add_argument(
    "-weight_decay", type=float, default=0,
    help="L2 regularization coeff [default: 0.0]",
)
parser.add_argument(
    "-lr", type=float, default=1e-2,
    help="Initial learning rate [default: 1e-2]"
)
parser.add_argument(
    "-lr_decay", type=float, default=0.5,
    help="Learning rate decay gamma [default: 0.5]",
)
parser.add_argument(
    "-decay_step", type=float, default=2e5,
    help="Learning rate decay step [default: 20]",
)
parser.add_argument(
    "-bn_momentum", type=float, default=0.9,
    help="Initial batch norm momentum [default: 0.9]",
)
parser.add_argument(
    "-bn_decay", type=float, default=0.5,
    help="Batch norm momentum decay gamma [default: 0.5]",
)
parser.add_argument(
    "-checkpoint", type=str, default=None,
    help="Checkpoint to start from"
)
parser.add_argument(
    "-epochs", type=int, default=1000, help="Number of epochs to train for"
)
parser.add_argument(
    "-eval_net", action='store_true', help="whether is to eval net."
)
parser.add_argument("-test", action="store_true")
parser.add_argument("-test_pose", action="store_true")
parser.add_argument("-test_gt", action="store_true")
parser.add_argument("-cal_metrics", action="store_true")
parser.add_argument("-view_dpt", action="store_true")
parser.add_argument('-debug', action='store_true')

parser.add_argument('--local_rank', type=int, default=0)
parser.add_argument('--gpu_id', type=list, default=[0, 1, 2, 3, 4, 5, 6, 7])
parser.add_argument('-n', '--nodes', default=1, type=int, metavar='N')
parser.add_argument('-g', '--gpus', default=8, type=int,
                    help='number of gpus per node')
parser.add_argument('-nr', '--nr', default=0, type=int,
                    help='ranking within the nodes')
parser.add_argument('--gpu', type=str, default="2,3,4,5,6,7")
parser.add_argument('--deterministic', action='store_true')
parser.add_argument('--keep_batchnorm_fp32', default=True)
parser.add_argument('--opt_level', default="O0", type=str,
                    help='opt level of apex mix presision trainig.')

args = parser.parse_args()

os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu


lr_clip = 1e-5
bnm_clip = 1e-2




import datasets.ycb.ycb_refine_8kps_dataset as dataset_desc
import models.pytorch_utils as pt_utils
from models.ffb6d_refine_pcn import FFB6D_REFINE
from models.loss import OFLoss, FocalLoss, ChamferDistance

from apex.parallel import DistributedDataParallel
from apex.parallel import convert_syncbn_model
from apex import amp
from apex.multi_tensor_apply import multi_tensor_applier
import warnings
warnings.filterwarnings("ignore")

def worker_init_fn(worker_id):
    np.random.seed(np.random.get_state()[1][0] + worker_id)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def checkpoint_state(model=None, optimizer=None, best_prec=None, epoch=None, it=None):
    optim_state = optimizer.state_dict() if optimizer is not None else None
    if model is not None:
        if isinstance(model, torch.nn.DataParallel) or \
                isinstance(model, torch.nn.parallel.DistributedDataParallel):
            model_state = model.module.state_dict()
        else:
            model_state = model.state_dict()
    else:
        model_state = None

    return {
        "epoch": epoch,
        "it": it,
        "best_prec": best_prec,
        "model_state": model_state,
        "optimizer_state": optim_state,
        "amp": amp.state_dict(),
    }


def save_checkpoint(
        state, is_best, filename="checkpoint", bestname="model_best",
        bestname_pure='ffb6d_best'
):
    filename = "{}.pth.tar".format(filename)
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, "{}.pth.tar".format(bestname))
        shutil.copyfile(filename, "{}.pth.tar".format(bestname_pure))


def load_checkpoint(model=None, optimizer=None, filename="checkpoint", device = 'default'):
    filename = "{}.pth.tar".format(filename)

    if os.path.isfile(filename):
        print("==> Loading from checkpoint '{}'".format(filename))
        checkpoint = torch.load(filename, map_location=device)
        epoch = checkpoint["epoch"]
        it = checkpoint.get("it", 0.0)
        best_prec = checkpoint["best_prec"]
        if model is not None and checkpoint["model_state"] is not None:
            ck_st = checkpoint['model_state']
            if 'module' in list(ck_st.keys())[0]:
                tmp_ck_st = {}
                for k, v in ck_st.items():
                    tmp_ck_st[k.replace("module.", "")] = v
                ck_st = tmp_ck_st
            model.load_state_dict(ck_st)
        if optimizer is not None and checkpoint["optimizer_state"] is not None:
            optimizer.load_state_dict(checkpoint["optimizer_state"])
        amp.load_state_dict(checkpoint["amp"])
        print(device)
        print("==> Done")
        return it, epoch, best_prec
    else:
        print("==> Checkpoint '{}' not found".format(filename))
        return None

def view_labels(rgb_chw, cld_cn, labels, K=config.intrinsic_matrix['ycb_K1']):
    rgb_hwc = np.transpose(rgb_chw[0].numpy(), (1, 2, 0)).astype("uint8").copy()
    cld_nc = np.transpose(cld_cn.numpy(), (1, 0)).copy()
    p2ds = bs_utils.project_p3d(cld_nc, 1.0, K).astype(np.int32)
    labels = labels.squeeze().contiguous().cpu().numpy()
    colors = []
    h, w = rgb_hwc.shape[0], rgb_hwc.shape[1]
    rgb_hwc = np.zeros((h, w, 3), "uint8")
    for lb in labels:
        if int(lb) == 0:
            c = (0, 0, 0)
        else:
            c = color_lst[int(lb)]
        colors.append(c)
    show = bs_utils.draw_p2ds(rgb_hwc, p2ds, 3, colors)
    return show


def model_fn_decorator(
    criterion, criterion_of, criterion_cd, test=False,
):
    def model_fn(
        model, data, it=0, epoch=0, is_eval=False
    ):
        if is_eval:
            model.eval()
        with torch.set_grad_enabled(not is_eval):
            cu_dt = {}
            # device = torch.device('cuda:{}'.format(args.local_rank))
            for key in data.keys():
                if data[key].dtype in [np.float32, np.uint8]:
                    cu_dt[key] = torch.from_numpy(data[key].astype(np.float32)).cuda()
                elif data[key].dtype in [np.int32, np.uint32]:
                    cu_dt[key] = torch.LongTensor(data[key].astype(np.int32)).cuda()
                elif data[key].dtype in [torch.uint8, torch.float32]:
                    cu_dt[key] = data[key].float().cuda()
                elif data[key].dtype in [torch.int32, torch.int16]:
                    cu_dt[key] = data[key].long().cuda()

            end_points = model(cu_dt)
            pred_labels = cu_dt['pred_labels']
            loss_kp_of = criterion_of(
                end_points['pred_kp_ofs'], cu_dt['kp_targ_ofst'], pred_labels
            ).sum()
            loss_ctr_of = criterion_of(
                end_points['pred_ctr_ofs'], cu_dt['ctr_targ_ofst'], pred_labels
            ).sum()
            loss_pcn_c = criterion_cd(end_points['coarse'].permute(0,2,1), cu_dt['pts_gt']) 
            loss_pcn_d = criterion_cd(end_points['detail'].permute(0,2,1), cu_dt["pts_gt"])

            loss_lst = [
                (loss_pcn_c, 10.0), (loss_pcn_d, 10.0), (loss_kp_of, 1.0), (loss_ctr_of, 1.0),
            ]
            loss = sum([ls * w for ls, w in loss_lst])
            loss_dict = {
                'loss_pcn_c': loss_pcn_c.item(),
                'loss_pcn_d': loss_pcn_d.item(),
                'loss_kp_of': loss_kp_of.item(),
                'loss_ctr_of': loss_ctr_of.item(),
                'loss_all': loss.item(),
                'loss_target': loss_kp_of.item() + loss_ctr_of.item()
            }
            info_dict = loss_dict.copy()
            if not is_eval:
                if args.local_rank == 0:
                    writer.add_scalars('loss', loss_dict, it)
        return (
            end_points, loss, info_dict
        )
    return model_fn


class Trainer(object):
    r"""
        Reasonably generic trainer for pytorch models

    Parameters
    ----------
    model : pytorch model
        Model to be trained
    model_fn : function (model, inputs, labels) -> preds, loss, accuracy
    optimizer : torch.optim
        Optimizer for model
    checkpoint_name : str
        Name of file to save checkpoints to
    best_name : str
        Name of file to save best model to
    lr_scheduler : torch.optim.lr_scheduler
        Learning rate scheduler.  .step() will be called at the start of every epoch
    bnm_scheduler : BNMomentumScheduler
        Batchnorm momentum scheduler.  .step() will be called at the start of every epoch
    """

    def __init__(
        self,
        model,
        model_fn,
        optimizer,
        checkpoint_name="ckpt",
        best_name="best",
        lr_scheduler=None,
        bnm_scheduler=None,
    ):
        self.model, self.model_fn, self.optimizer, self.lr_scheduler, self.bnm_scheduler = (
            model,
            model_fn,
            optimizer,
            lr_scheduler,
            bnm_scheduler,
        )

        self.checkpoint_name, self.best_name = checkpoint_name, best_name

        self.training_best, self.eval_best = {}, {}

    def eval_epoch(self, d_loader,it=0):
        self.model.eval()
        eval_dict = {}
        total_loss = 0.0
        count = 1
        for i, data in tqdm.tqdm(
            enumerate(d_loader), leave=False, desc="val"
        ):
            count += 1
            self.optimizer.zero_grad()
            _, loss, eval_res = self.model_fn(self.model, data, is_eval=True)

            if 'loss_target' in eval_res.keys():
                total_loss += eval_res['loss_target']
            else:
                total_loss += loss.item()
            for k, v in eval_res.items():
                if v is not None:
                    eval_dict[k] = eval_dict.get(k, []) + [v]

        mean_eval_dict = {}
        for k, v in eval_dict.items():
            per = 100 if 'acc' in k else 1
            mean_eval_dict[k] = np.array(v).mean() * per
        for k, v in mean_eval_dict.items():
            print(k, v)
        return total_loss / count

    def train(
        self,
        start_it,
        start_epoch,
        n_epochs,
        train_loader,
        train_sampler,
        test_loader=None,
        best_loss=0.0,
        log_epoch_f=None,
        tot_iter=1,
        clr_div=6,
    ):
        r"""
           Call to begin training the model

        Parameters
        ----------
        start_epoch : int
            Epoch to start at
        n_epochs : int
            Number of epochs to train for
        test_loader : torch.utils.data.DataLoader
            DataLoader of the test_data
        train_loader : torch.utils.data.DataLoader
            DataLoader of training data
        best_loss : float
            Testing loss of the best model
        """

        print("Totally train %d iters per gpu." % tot_iter)

        def is_to_eval(epoch, it):
            # Eval after first 100 iters to test eval function.
            if it == 100:
                return True, 1
            wid = tot_iter // clr_div
            if (it // wid) % 2 == 1:
                eval_frequency = wid // 15
            else:
                eval_frequency = wid // 6
            to_eval = (it % eval_frequency) == 0
            return to_eval, eval_frequency

        it = start_it
        _, eval_frequency = is_to_eval(0, it)

        with tqdm.tqdm(range(config.n_total_epoch), desc="epochs") as tbar, tqdm.tqdm(
            total=eval_frequency, leave=False, desc="train"
        ) as pbar:
            for epoch in tbar:
                if epoch > config.n_total_epoch:
                    break
                if train_sampler is not None:
                    train_sampler.set_epoch(epoch)
                # Reset numpy seed.
                # REF: https://github.com/pytorch/pytorch/issues/5059
                np.random.seed()
                if log_epoch_f is not None:
                    os.system("echo {} > {}".format(epoch, log_epoch_f))
                for batch in train_loader:
                    self.model.train()

                    self.optimizer.zero_grad()
                    _, loss, res = self.model_fn(self.model, batch, it=it)

                    with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                        scaled_loss.backward()
                    lr = get_lr(self.optimizer)
                    if args.local_rank == 0:
                        writer.add_scalar('lr/lr', lr, it)

                    self.optimizer.step()

                    if self.lr_scheduler is not None:
                        self.lr_scheduler.step(it)

                    if self.bnm_scheduler is not None:
                        self.bnm_scheduler.step(it)

                    it += 1

                    pbar.update()
                    pbar.set_postfix(dict(total_it=it))
                    tbar.refresh()


                    eval_flag, eval_frequency = is_to_eval(epoch, it)
                    if eval_flag:
                        pbar.close()

                        if test_loader is not None:
                            val_loss = self.eval_epoch(test_loader, it)
                            if args.local_rank == 0:
                                writer.add_scalars('val_loss', {'val_loss':val_loss}, it)
                            print("val_loss", val_loss)

                            is_best = (val_loss - best_loss) / best_loss < 0.1
                            best_loss = min(best_loss, val_loss)
                            if args.local_rank == 0:
                                save_checkpoint(
                                    checkpoint_state(
                                        self.model, self.optimizer, val_loss, epoch, it
                                    ),
                                    is_best,
                                    filename=self.checkpoint_name,
                                    bestname=self.best_name+'_%.4f' % val_loss,
                                    bestname_pure=self.best_name
                                )
                                info_p = self.checkpoint_name.replace(
                                    '.pth.tar', '_epoch.txt'
                                )
                                os.system(
                                    'echo {} {} >> {}'.format(
                                        it, val_loss, info_p
                                    )
                                )

                        pbar = tqdm.tqdm(
                            total=eval_frequency, leave=False, desc="train"
                        )
                        pbar.set_postfix(dict(total_it=it))

            if args.local_rank == 0:
                writer.export_scalars_to_json("./all_scalars.json")
                writer.close()
        return best_loss


def train():
    print("local_rank:", args.local_rank)
    cudnn.benchmark = True
    if args.deterministic:
        cudnn.benchmark = False
        cudnn.deterministic = True
        torch.manual_seed(args.local_rank)
        torch.set_printoptions(precision=10)
    torch.cuda.set_device(args.local_rank)
    torch.distributed.init_process_group(
        backend='nccl',
        init_method='env://',
    )
    torch.manual_seed(0)

    if not args.eval_net:
        train_ds = dataset_desc.Dataset('train')
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_ds)
        train_loader = torch.utils.data.DataLoader(
            train_ds, batch_size=config.mini_batch_size, shuffle=False,
            drop_last=True, num_workers=4, sampler=train_sampler, pin_memory=True
        )

        val_ds = dataset_desc.Dataset('test')
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_ds)
        val_loader = torch.utils.data.DataLoader(
            val_ds, batch_size=config.val_mini_batch_size, shuffle=False,
            drop_last=False, num_workers=4, sampler=val_sampler
        )
    else:
        test_ds = dataset_desc.Dataset('test')
        test_loader = torch.utils.data.DataLoader(
            test_ds, batch_size=config.test_mini_batch_size, shuffle=False,
            num_workers=20
        )

    rndla_cfg = ConfigRandLA
    model = FFB6D_REFINE(
        n_classes=config.n_objects, n_pts=config.n_sample_points, rndla_cfg=rndla_cfg,
        n_kps=config.n_keypoints
    )
    # model = PVN3D(
    #     num_classes=config.n_objects, pcld_input_channels=6, pcld_use_xyz=True,
    #     num_kps=config.n_keypoints, num_points=config.n_sample_points
    # )
    model = convert_syncbn_model(model)
    device = torch.device('cuda:{}'.format(args.local_rank))
    model.to(device)
    optimizer = optim.Adam(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay
    )
    opt_level = args.opt_level
    model, optimizer = amp.initialize(
        model, optimizer, opt_level=opt_level,
    )

    # default value
    it = -1  # for the initialize value of `LambdaLR` and `BNMomentumScheduler`
    best_loss = 1e10
    start_epoch = 1

    # load status from checkpoint
    if args.checkpoint is not None:
        checkpoint_status = load_checkpoint(
            model, optimizer, filename=args.checkpoint[:-8], device=device
        )
        if checkpoint_status is not None:
            it, start_epoch, best_loss = checkpoint_status
        if args.eval_net:
            assert checkpoint_status is not None, "Failed loadding model."

    if not args.eval_net:
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[args.local_rank], output_device=args.local_rank,
            find_unused_parameters=True
        )
        clr_div = 8
        lr_scheduler = CyclicLR(
            optimizer, base_lr=1e-5, max_lr=1e-3,
            cycle_momentum=False,
            step_size_up=config.n_total_epoch * train_ds.minibatch_per_epoch // clr_div // args.gpus,
            step_size_down=config.n_total_epoch * train_ds.minibatch_per_epoch // clr_div // args.gpus,
            mode='triangular'
        )
    else:
        lr_scheduler = None

    bnm_lmbd = lambda it: max(
        args.bn_momentum * args.bn_decay ** (int(it * config.mini_batch_size / args.decay_step)),
        bnm_clip,
    )
    bnm_scheduler = pt_utils.BNMomentumScheduler(
        model, bn_lambda=bnm_lmbd, last_epoch=it
    )

    it = max(it, 0)  # for the initialize value of `trainer.train`

    if args.eval_net:
        model_fn = model_fn_decorator(
            FocalLoss(gamma=2), OFLoss(), ChamferDistance(),
            args.test,
        )
    else:
        model_fn = model_fn_decorator(
            FocalLoss(gamma=2).to(device), OFLoss().to(device), ChamferDistance().to(device),
            args.test,
        )

    checkpoint_fd = config.log_model_dir

    trainer = Trainer(
        model,
        model_fn,
        optimizer,
        checkpoint_name=os.path.join(checkpoint_fd, "FFB6D_REFINE"),
        best_name=os.path.join(checkpoint_fd, "FFB6D_REFINE_best"),
        # checkpoint_name=os.path.join(checkpoint_fd, "PVN3D_REFINE"),
        # best_name=os.path.join(checkpoint_fd, "PVN3D_REFINE_best"),
        lr_scheduler=lr_scheduler,
        bnm_scheduler=bnm_scheduler,
    )

    if args.eval_net:
        start = time.time()
        val_loss, res = trainer.eval_epoch(test_loader)
        end = time.time()
        print("\nUse time: ", end - start, 's')
    else:
        trainer.train(
            it, start_epoch, config.n_total_epoch, train_loader, None,
            val_loader, best_loss=best_loss,
            tot_iter=config.n_total_epoch * train_ds.minibatch_per_epoch // args.gpus,
            clr_div=clr_div
        )

        if start_epoch == config.n_total_epoch:
            _ = trainer.eval_epoch(val_loader)


if __name__ == "__main__":
    args.world_size = args.gpus * args.nodes
    train()
