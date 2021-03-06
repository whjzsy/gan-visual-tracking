import argparse
import json
import os
import pickle
import time

import numpy as np
import torch
from torch import nn
from torch.autograd import Variable

from ..modules.data_prov import RegionDataset
from ..modules.model import MDNet, BinaryLoss, Precision
from ..modules.roi_align.modules.roi_align import RoIAlignAdaMax
from ..modules.utils import set_optimizer


def train_mdnet(pretrain_opts):
    # set image directory
    if pretrain_opts['set_type'] == 'OTB':
        img_home = os.environ['TRK_WD'] + '/datasets/OTB/'
        data_path = 'pretrain/data/otb-vot15.pkl'
    if pretrain_opts['set_type'] == 'VOT':
        img_home = os.environ['TRK_WD'] + '/datasets/vot/'
        data_path = 'pretrain/data/vot-otb.pkl'
    if pretrain_opts['set_type'] == 'IMAGENET':
        img_home = os.environ['TRK_WD'] + '/datasets/imagenet/ILSVRC2015/Data/VID/train/'
        data_path = 'pretrain/data/imagenet_vid.pkl'

    # Init dataset
    with open(data_path, 'rb') as fp:
        data = pickle.load(fp)

    K = len(data)

    # Init model
    model = MDNet(pretrain_opts['init_model_path'], K)
    #model = MDNet(None, K)
    if pretrain_opts['adaptive_align']:
        align_h = model.roi_align_model.aligned_height
        align_w = model.roi_align_model.aligned_width
        spatial_s = model.roi_align_model.spatial_scale
        model.roi_align_model = RoIAlignAdaMax(align_h, align_w, spatial_s)

    if pretrain_opts['use_gpu']:
        model = model.cuda()
    model.set_learnable_params(pretrain_opts['ft_layers'])
    model.train()

    dataset = [None] * K
    for k, (seq_name, seq) in enumerate(data.items()):
        img_list = seq['images']
        gt = seq['gt']
        if pretrain_opts['set_type'] == 'OTB':
            img_dir = os.path.join(img_home, seq_name, 'img')
        if pretrain_opts['set_type'] == 'VOT':
            img_dir = os.path.join(img_home, seq_name)
        if pretrain_opts['set_type'] == 'IMAGENET':
            img_dir = os.path.join(img_home, seq_name)
        dataset[k] = RegionDataset(img_dir, img_list, gt, model.receptive_field, pretrain_opts)

    # Init criterion and optimizer
    binaryCriterion = BinaryLoss()
    interDomainCriterion = nn.CrossEntropyLoss()
    evaluator = Precision()
    optimizer = set_optimizer(model, pretrain_opts['lr'], lr_mult=pretrain_opts['lr_mult'],
                              momentum=pretrain_opts['momentum'],
                              w_decay=pretrain_opts['w_decay'])

    best_score = 0.
    batch_cur_idx = 0
    for i in range(pretrain_opts['n_cycles']):
        print("==== Start Cycle %d ====" % (i))
        k_list = np.random.permutation(K)
        prec = np.zeros(K)
        totalTripleLoss = np.zeros(K)
        totalInterClassLoss = np.zeros(K)
        for j, k in enumerate(k_list):
            tic = time.time()
            try:
                cropped_scenes, pos_rois, neg_rois = next(dataset[k])
            except:
                continue

            try:
                for sidx in range(0, len(cropped_scenes)):
                    cur_scene = cropped_scenes[sidx]
                    cur_pos_rois = pos_rois[sidx]
                    cur_neg_rois = neg_rois[sidx]

                    cur_scene = Variable(cur_scene)
                    cur_pos_rois = Variable(cur_pos_rois)
                    cur_neg_rois = Variable(cur_neg_rois)
                    if pretrain_opts['use_gpu']:
                        cur_scene = cur_scene.cuda()
                        cur_pos_rois = cur_pos_rois.cuda()
                        cur_neg_rois = cur_neg_rois.cuda()
                    cur_feat_map = model(cur_scene, k, out_layer='conv3')

                    cur_pos_feats = model.roi_align_model(cur_feat_map, cur_pos_rois)
                    cur_pos_feats = cur_pos_feats.view(cur_pos_feats.size(0), -1)
                    cur_neg_feats = model.roi_align_model(cur_feat_map, cur_neg_rois)
                    cur_neg_feats = cur_neg_feats.view(cur_neg_feats.size(0), -1)

                    if sidx == 0:
                        pos_feats = [cur_pos_feats]
                        neg_feats = [cur_neg_feats]
                    else:
                        pos_feats.append(cur_pos_feats)
                        neg_feats.append(cur_neg_feats)
                feat_dim = cur_neg_feats.size(1)
                pos_feats = torch.stack(pos_feats, dim=0).view(-1, feat_dim)
                neg_feats = torch.stack(neg_feats, dim=0).view(-1, feat_dim)
            except:
                continue

            pos_score = model(pos_feats, k, in_layer='fc4')
            neg_score = model(neg_feats, k, in_layer='fc4')

            cls_loss = binaryCriterion(pos_score, neg_score)

            # inter frame classification

            interclass_label = Variable(torch.zeros((pos_score.size(0))).long())
            if pretrain_opts['use_gpu']:
                interclass_label = interclass_label.cuda()
            total_interclass_score = pos_score[:, 1].contiguous()
            total_interclass_score = total_interclass_score.view((pos_score.size(0), 1))

            K_perm = np.random.permutation(K)
            K_perm = K_perm[0:100]
            for cidx in K_perm:
                if k == cidx:
                    continue
                else:
                    interclass_score = model(pos_feats, cidx, in_layer='fc4')
                    total_interclass_score = torch.cat((total_interclass_score,
                                                        interclass_score[:, 1].contiguous().view(
                                                            (interclass_score.size(0), 1))), dim=1)

            interclass_loss = interDomainCriterion(total_interclass_score, interclass_label)
            totalInterClassLoss[k] = interclass_loss.data.item()

            (cls_loss + 0.1 * interclass_loss).backward()

            batch_cur_idx += 1
            if (batch_cur_idx % pretrain_opts['seqbatch_size']) == 0:
                torch.nn.utils.clip_grad_norm(model.parameters(), pretrain_opts['grad_clip'])
                optimizer.step()
                model.zero_grad()
                batch_cur_idx = 0

            # evaulator
            prec[k] = evaluator(pos_score, neg_score)
            # computation latency
            toc = time.time() - tic

            print("Cycle %2d, K %2d (%2d), BinLoss %.3f, Prec %.3f, interLoss %.3f, Time %.3f" %
                  (i, j, k, cls_loss.data.item(), prec[k], totalInterClassLoss[k], toc))

        cur_score = prec.mean()
        #try:
        #    total_miou = sum(total_iou) / len(total_iou)
        #except:
        #    total_miou = 0.
        #print("Mean Precision: %.3f Triple Loss: %.3f Inter Loss: %.3f IoU: %.3f" % (
        #    prec.mean(), cur_triple_loss, totalInterClassLoss.mean(), total_miou))
        if cur_score > best_score:
            best_score = cur_score
            if pretrain_opts['use_gpu']:
                model = model.cpu()
            states = {'shared_layers': model.layers.state_dict()}
            print("Save model to %s" % pretrain_opts['model_path'])
            torch.save(states, pretrain_opts['model_path'])
            if pretrain_opts['use_gpu']:
                model = model.cuda()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-set_type", default = 'VOT' )
    parser.add_argument("-padding_ratio", default = 5., type =float)
    parser.add_argument("-model_path", default ="./models/rt_mdnet.pth", help = "model path")
    parser.add_argument("-frame_interval", default = 1, type=int, help="frame interval in batch. ex) interval=1 -> [1 2 3 4 5], interval=2 ->[1 3 5]")
    parser.add_argument("-init_model_path", default="./models/imagenet-vgg-m.mat")
    parser.add_argument("-batch_frames", default = 8, type = int)
    parser.add_argument("-lr", default=0.0001, type = float)
    parser.add_argument("-batch_pos",default = 64, type = int)
    parser.add_argument("-batch_neg", default = 196, type = int)
    parser.add_argument("-n_cycles", default = 1000, type = int )
    parser.add_argument("-adaptive_align", default = True, action = 'store_false')
    parser.add_argument("-seqbatch_size", default=50, type=int)

    args = parser.parse_args()

    if args.set_type == "VOT":
        with open('pretrain/pretrain_vot_options.json', 'r') as fh:
            pretrain_opts = json.load(fh)
    elif args.set_type == "IMAGENET":
        with open('pretrain/pretrain_imagenet_options.json', 'r') as fh:
            pretrain_opts = json.load(fh)

    ##################################################################################
    #########################Just modify opts in this script.#########################
    ######################Becuase of synchronization of options#######################
    ##################################################################################
    ##option setting
    pretrain_opts['set_type'] = args.set_type
    pretrain_opts['padding_ratio']=args.padding_ratio
    pretrain_opts['padded_img_size']=pretrain_opts['img_size']*int(pretrain_opts['padding_ratio'])
    pretrain_opts['model_path']=args.model_path
    pretrain_opts['frame_interval'] = args.frame_interval
    pretrain_opts['init_model_path'] = args.init_model_path
    pretrain_opts['batch_frames'] = args.batch_frames
    pretrain_opts['lr'] = args.lr
    pretrain_opts['batch_pos'] = args.batch_pos  # original = 64
    pretrain_opts['batch_neg'] = args.batch_neg  # original = 192
    pretrain_opts['n_cycles'] = args.n_cycles
    pretrain_opts['adaptive_align']=args.adaptive_align
    pretrain_opts['seqbatch_size'] = args.seqbatch_size
    ##################################################################################
    ############################Do not modify opts anymore.###########################
    ######################Becuase of synchronization of options#######################
    ##################################################################################

    print(pretrain_opts)
    train_mdnet(pretrain_opts)


if __name__ == "__main__":
    main()
