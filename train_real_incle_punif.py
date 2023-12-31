import torch
import itertools
import torch.nn as nn
from torch.autograd import Variable
from torch.nn import Parameter
import torch.nn.functional as F
import torch.optim as optim
from collections import Counter
from torch.utils.data import DataLoader, TensorDataset, WeightedRandomSampler
from layers import ZINBLoss, MeanAct, DispAct, GaussianNoise
import numpy as np
from sklearn.cluster import KMeans
import math, os
from sklearn import metrics
from preprocessing import *
import argparse
import random
from itertools import cycle
from scipy.optimize import linear_sum_assignment
from sklearn.preprocessing import OneHotEncoder
from sklearn.metrics import confusion_matrix
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score, calinski_harabasz_score
import pandas as pd
from augmentation import *
import anndata


class AverageMeter(object):
    def __init__(self, name, fmt=':f'):
        self.name = name
        self.fmt = fmt
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)


class SupConLoss(nn.Module):
    """Supervised Contrastive Learning: https://arxiv.org/pdf/2004.11362.pdf.
    It also supports the unsupervised contrastive loss in SimCLR"""
    def __init__(self, temperature=0.07, contrast_mode='all'):
        super(SupConLoss, self).__init__()
        self.temperature = temperature
        self.contrast_mode = contrast_mode

    def forward(self, features, device, labels=None, mask=None):
        """Compute loss for model. If both `labels` and `mask` are None,
        it degenerates to SimCLR unsupervised loss:
        https://arxiv.org/pdf/2002.05709.pdf

        Args:
            features: hidden vector of shape [bsz, n_views, ...].
            labels: ground truth of shape [bsz].
            mask: contrastive mask of shape [bsz, bsz], mask_{i,j}=1 if sample j
                has the same class as sample i. Can be asymmetric.
        Returns:
            A loss scalar.
        """
        # device = (torch.device('cuda')
        #           if features.is_cuda
        #           else torch.device('cpu'))

        if len(features.shape) < 3:
            raise ValueError('`features` needs to be [bsz, n_views, ...],'
                             'at least 3 dimensions are required')
        if len(features.shape) > 3:
            features = features.view(features.shape[0], features.shape[1], -1)

        batch_size = features.shape[0]
        if labels is not None and mask is not None:
            raise ValueError('Cannot define both `labels` and `mask`')
        elif labels is None and mask is None:
            mask = torch.eye(batch_size, dtype=torch.float32).to(device)
        elif labels is not None:
            labels = labels.contiguous().view(-1, 1)
            if labels.shape[0] != batch_size:
                raise ValueError('Num of labels does not match num of features')
            mask = torch.eq(labels, labels.T).float().to(device)
        else:
            mask = mask.float().to(device)

        contrast_count = features.shape[1]
        contrast_feature = torch.cat(torch.unbind(features, dim=1), dim=0)
        if self.contrast_mode == 'one':
            anchor_feature = features[:, 0]
            anchor_count = 1
        elif self.contrast_mode == 'all':
            anchor_feature = contrast_feature
            anchor_count = contrast_count
        elif self.contrast_mode == 'proxy':
            anchor_feature = features[:, 0]
            contrast_feature = features[:, 1]
            anchor_count = 1
            contrast_count = 1
        else:
            raise ValueError('Unknown mode: {}'.format(self.contrast_mode))

        # compute logits
        anchor_dot_contrast = torch.div(
            torch.matmul(anchor_feature, contrast_feature.T),
            self.temperature)
        # for numerical stability
        logits_max, _ = torch.max(anchor_dot_contrast, dim=1, keepdim=True)
        logits = anchor_dot_contrast - logits_max.detach()

        # tile mask
        mask = mask.repeat(anchor_count, contrast_count)
        # mask-out self-contrast cases
        logits_mask = torch.scatter(
            torch.ones_like(mask),
            1,
            torch.arange(batch_size * anchor_count).view(-1, 1).to(device),
            0
        )

        # compute log_prob
        if self.contrast_mode == 'proxy':
            exp_logits = torch.exp(logits)
        else:
            mask = mask * logits_mask
            exp_logits = torch.exp(logits) * logits_mask
        log_prob = logits - torch.log(exp_logits.sum(1, keepdim=True))

        # compute mean of log-likelihood over positive
        mean_log_prob_pos = (mask * log_prob).sum(1) / mask.sum(1)

        # loss
        loss = -1 * mean_log_prob_pos
        loss = loss.view(anchor_count, batch_size).mean()

        return loss


def accuracy(output, target):
    num_correct = np.sum(output == target)
    res = num_correct / len(target)
    return res


def cluster_acc(y_pred, y_true):
    """
    Calculate clustering accuracy. Require scikit-learn installed
    # Arguments
        y: true labels, numpy.array with shape `(n_samples,)`
        y_pred: predicted labels, numpy.array with shape `(n_samples,)`
    # Return
        accuracy, in [0,1]
    """
    y_true = y_true.astype(np.int64)
    assert y_pred.size == y_true.size
    D = max(y_pred.max(), y_true.max()) + 1
    w = np.zeros((D, D), dtype=np.int64)
    for i in range(y_pred.size):
        w[y_pred[i], y_true[i]] += 1
    row_ind, col_ind = linear_sum_assignment(w.max() - w)
    return w[row_ind, col_ind].sum() / y_pred.size


def auxilarly_dis(pred):
    weight = (pred ** 2) / torch.sum(pred, 0)
    return (weight.t() / torch.sum(weight, 1)).t()


def entropy(x):
    """
    Helper function to compute the entropy over the batch
    input: batch w/ shape [b, num_classes]
    output: entropy value [is ideally -log(num_classes)]
    """
    EPS = 1e-8
    x_ =  torch.clamp(x, min = EPS)
    b =  x_ * torch.log(x_)

    if len(b.size()) == 2: # Sample-wise entropy
        return - b.sum(dim = 1).mean()
    elif len(b.size()) == 1: # Distribution-wise entropy
        return - b.sum()
    else:
        raise ValueError('Input tensor is %d-Dimensional' %(len(b.size())))


def buildNetwork(layers, activation="relu", noise=False, batchnorm=False):
    net = []
    for i in range(1, len(layers)):
        net.append(nn.Linear(layers[i-1], layers[i]))
        if noise:
            net.append(GaussianNoise())
        if activation=="relu":
            net.append(nn.ReLU())
        elif activation=="sigmoid":
            net.append(nn.Sigmoid())
        if batchnorm:
            net.append(nn.BatchNorm1d(layers[i]))
    return nn.Sequential(*net)


class Prototype(nn.Module):
    def __init__(self, num_classes, input_size, tau=0.05):
        super(Prototype, self).__init__()
        self.fc = nn.Linear(input_size, num_classes, bias=False)
        self.tau = tau
        self.weight_norm()

    def forward(self, x):
        x = F.normalize(x)
        x = self.fc(x) / self.tau
        return x

    def weight_norm(self):
        w = self.fc.weight.data
        norm = w.norm(p=2, dim=1, keepdim=True)
        self.fc.weight.data = w.div(norm.expand_as(w))


def off_diagonal(x):
    # return a flattened view of the off-diagonal elements of a square matrix
    n, m = x.shape
    assert n == m
    return x.flatten()[:-1].view(n - 1, n + 1)[:, 1:].flatten()


def decorrelate_loss(x, y):
    _, c = x.shape
    uniq_l, uniq_c = y.unique(return_counts=True)
    loss = 0.
    n_count = 0
    eps = 1e-8
    for i, label in enumerate(uniq_l):
        if uniq_c[i] <= 1:
            continue
        x_label = x[y == label, :]
        x_label = x_label - x_label.mean(dim=0, keepdim=True)
        x_label = x_label / torch.sqrt(eps + x_label.var(dim=0, keepdim=True))

        N = x_label.shape[0]
        corr_mat = torch.matmul(x_label.t(), x_label)

        # Notice that here the implementation is a little bit different
        # from the paper as we extract only the off-diagonal terms for regularization.
        # Mathematically, these two are the same thing since diagonal terms are all constant 1.
        # However, we find that this implementation is more numerically stable.
        loss += (off_diagonal(corr_mat).pow(2)).mean()

        n_count += N

    if n_count == 0:
        # there is no effective class to compute correlation matrix
        return 0
    else:
        loss = loss / n_count
        return loss


class AutoEncoder(nn.Module):
    def __init__(self, input_dim, z_dim, encodeLayer=[], decodeLayer=[], activation="relu"):
        super(AutoEncoder, self).__init__()
        self.input_dim = input_dim
        self.z_dim = z_dim
        self.activation = activation
        self.encoder = buildNetwork([self.input_dim] + encodeLayer, activation=activation, noise=True, batchnorm=False)
        self.decoder = buildNetwork([self.z_dim] + decodeLayer, activation=activation, batchnorm=False)
        self._enc_mu = nn.Linear(encodeLayer[-1], self.z_dim)
        self._dec_mean = nn.Sequential(nn.Linear(decodeLayer[-1], self.input_dim), MeanAct())
        self._dec_disp = nn.Sequential(nn.Linear(decodeLayer[-1], self.input_dim), DispAct())
        self._dec_pi = nn.Sequential(nn.Linear(decodeLayer[-1], self.input_dim), nn.Sigmoid())

    def forward(self, x):
        h = self.encoder(x)
        z = self._enc_mu(h)
        h = self.decoder(z)
        mean = self._dec_mean(h)
        disp = self._dec_disp(h)
        pi = self._dec_pi(h)
        return z, mean, disp, pi


def extractor(model, test_loader, device):
    model.eval()
    test_embedding = []
    test_label = []
    test_index = []
    with torch.no_grad():
        for _, data in enumerate(test_loader):
            x_t, label_t, index_t = data[0].to(device), data[3].to(device), data[4].to(device)
            z_t, _, _, _ = model(x_t)
            test_embedding.append(z_t.detach())
            test_label.append(label_t)
            test_index.append(index_t)
    test_embedding = torch.cat(test_embedding, dim=0)
    test_label = torch.cat(test_label)
    test_index = torch.cat(test_index)
    _, test_indexes = torch.sort(test_index, descending=False)
    test_embedding = test_embedding[test_indexes]
    test_label = test_label[test_indexes]
    test_embedding = test_embedding.cpu().numpy()
    test_label = test_label.cpu().numpy()
    return test_embedding, test_label


def test(model, labeled_num, device, test_loader, cluster_mapping, epoch):
    model.eval()
    preds = np.array([])
    preds_open = np.array([])
    targets = np.array([])
    confs = np.array([])
    confs_open = np.array([])
    with torch.no_grad():
        for _, data in enumerate(test_loader):
            x_t, label_t, index_t, batch_t = data[0].to(device), data[3].to(device), data[4].to(device), data[5].to(device)
            z, _, _, _, _, output_s, output_t = model(x_t, batch_t)
            conf, pred = output_t.max(1)
            targets = np.append(targets, label_t.cpu().numpy())
            preds = np.append(preds, pred.cpu().numpy())
            preds_open = np.append(preds_open, pred.cpu().numpy())
            confs = np.append(confs, conf.cpu().numpy())
            confs_open = np.append(confs_open, conf.cpu().numpy())
    for i in range(len(cluster_mapping)):
        preds[preds == cluster_mapping[i]] = i
    k = 0
    for j in np.unique(preds_open):
        if j not in cluster_mapping:
            preds[preds == j] = len(cluster_mapping) + k
            k += 1
    targets = targets.astype(int)
    preds = preds.astype(int)
    preds_open = preds_open.astype(int)
    seen_mask = targets < labeled_num
    unseen_mask = ~seen_mask
    overall_acc = cluster_acc(preds, targets)
    overall_acc2 = cluster_acc(preds_open, targets)
    seen_acc = accuracy(preds[seen_mask], targets[seen_mask])
    seen_acc2 = cluster_acc(preds_open[seen_mask], targets[seen_mask])
    unseen_acc = cluster_acc(preds[unseen_mask], targets[unseen_mask])
    unseen_acc2 = cluster_acc(preds_open[unseen_mask], targets[unseen_mask])
    print('In the old {}-th epoch, Test overall acc {:.4f}, seen acc {:.4f}, unseen acc {:.4f}'.format(epoch, overall_acc,
                                                                                                   seen_acc,
                                                                                                   unseen_acc))
    print('In the old {}-th epoch, Test overall acc2 {:.4f}, seen acc2 {:.4f}, unseen acc2 {:.4f}'.format(epoch, overall_acc2,
                                                                                                    seen_acc2,
                                                                                                    unseen_acc2))
    return overall_acc, seen_acc, unseen_acc, overall_acc2, seen_acc2, unseen_acc2


def dataset_spliting(X, count_X, cellname, size_factor, cell_number_list, class_set_list, labeled_ratio=0.5, random_seed=8888):
    train_X_set = []
    train_count_X_set = []
    train_cellname_set = []
    train_size_factor_set = []
    train_Y_set = []
    test_X_set = []
    test_count_X_set = []
    test_cellname_set = []
    test_size_factor_set = []
    test_Y_set = []
    unique_class_set_list = []

    for i in range(len(cell_number_list)):
        if i == 0:
            X1 = X[:cell_number_list[i]]
            count_X1 = count_X[:cell_number_list[i]]
            cellname1 = cellname[:cell_number_list[i]]
            size_factor1 = size_factor[:cell_number_list[i]]
        else:
            X1 = X[cell_number_list[i-1]:cell_number_list[i]]
            count_X1 = count_X[cell_number_list[i-1]:cell_number_list[i]]
            cellname1 = cellname[cell_number_list[i-1]:cell_number_list[i]]
            size_factor1 = size_factor[cell_number_list[i-1]:cell_number_list[i]]
        train_index1 = []
        test_index1 = []
        np.random.seed(random_seed)

        for j in range(X1.shape[0]):
            if np.random.rand() < labeled_ratio:
                train_index1.append(j)
            else:
                test_index1.append(j)

        train_X1 = X1[train_index1]
        train_count_X1 = count_X1[train_index1]
        train_cellname1 = cellname1[train_index1]
        train_size_factor1 = size_factor1[train_index1]
        test_X1 = X1[test_index1]
        test_count_X1 = count_X1[test_index1]
        test_cellname1 = cellname1[test_index1]
        test_size_factor1 = size_factor1[test_index1]
        train_Y1 = np.array([0] * len(train_cellname1))
        test_Y1 = np.array([0] * len(test_cellname1))

        current_class_set = class_set_list[i]
        for k in range(len(current_class_set)):
            if current_class_set[k] in unique_class_set_list:
                train_Y1[train_cellname1 == current_class_set[k]] = unique_class_set_list.index(current_class_set[k])
                test_Y1[test_cellname1 == current_class_set[k]] = unique_class_set_list.index(current_class_set[k])
            else:
                unique_class_set_list.append(current_class_set[k])
                train_Y1[train_cellname1 == current_class_set[k]] = len(unique_class_set_list) - 1
                test_Y1[test_cellname1 == current_class_set[k]] = len(unique_class_set_list) - 1
        print("For the {}-th stage, the train cell class number is {} and the test class number "
              "is {}".format(i + 1, len(np.unique(train_Y1)), len(np.unique(test_Y1))))
        train_X_set.append(train_X1)
        train_count_X_set.append(train_count_X1)
        train_cellname_set.append(train_cellname1)
        train_size_factor_set.append(train_size_factor1)
        train_Y_set.append(train_Y1)
        test_X_set.append(test_X1)
        test_count_X_set.append(test_count_X1)
        test_cellname_set.append(test_cellname1)
        test_size_factor_set.append(test_size_factor1)
        test_Y_set.append(test_Y1)

    return train_X_set, train_count_X_set, train_cellname_set, train_size_factor_set, train_Y_set, \
           test_X_set, test_count_X_set, test_cellname_set, test_size_factor_set, test_Y_set, unique_class_set_list


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='scUDA')
    parser.add_argument('--random-seed', type=int, default=8888, metavar='S')
    parser.add_argument('--gpu-id', default='0', type=int)
    parser.add_argument('--num', default=0, type=int)
    parser.add_argument('--ra', type=float, default=0.5)
    parser.add_argument('--stage', default=4, type=int)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--highly-genes', type=int, default=2000)
    parser.add_argument('--tau', type=float, default=1.0)
    parser.add_argument('--pretrain', type=int, default=200)
    parser.add_argument('--finetune', type=int, default=200)
    parser.add_argument('--interval', type=int, default=10)
    parser.add_argument('--lr', type=float, default=0.0001)
    parser.add_argument('--top-k', type=int, default=20)
    parser.add_argument('--structure', type=int, default=1)

    args = parser.parse_args()
    torch.manual_seed(args.random_seed)
    torch.cuda.manual_seed_all(args.random_seed)
    np.random.seed(args.random_seed)
    random.seed(args.random_seed)
    torch.backends.cudnn.deterministic = True

    device = torch.device('cuda' if torch.cuda.is_available() else "cpu", args.gpu_id)

    filename_set = [["Cao_2020_Eye", "Cao_2020_Intestine", "Cao_2020_Pancreas", "Cao_2020_Stomach"],
                    ["Cao_2020_Intestine", "Cao_2020_Pancreas", "Cao_2020_Stomach", "Cao_2020_Eye"],
                    ["Cao_2020_Pancreas", "Cao_2020_Stomach", "Cao_2020_Eye", "Cao_2020_Intestine"],
                    ["Cao_2020_Stomach", "Cao_2020_Eye", "Cao_2020_Intestine", "Cao_2020_Pancreas"]]

    filename_simply_set = [["Eye", "Intestine", "Pancreas", "Stomach"],
                           ["Intestine", "Pancreas", "Stomach", "Eye"],
                           ["Pancreas", "Stomach", "Eye", "Intestine"],
                           ["Stomach", "Eye", "Intestine", "Pancreas"]]

    # filename_set = [["He_Lone_Bone", "Madissoon_Lung", "Stewart_Fetal", "Vento-Tormo_10x"],
    #                 ["Madissoon_Lung", "Stewart_Fetal", "Vento-Tormo_10x", "He_Lone_Bone"],
    #                 ["Stewart_Fetal", "Vento-Tormo_10x", "He_Lone_Bone", "Madissoon_Lung"],
    #                 ["Vento-Tormo_10x", "He_Lone_Bone", "Madissoon_Lung", "Stewart_Fetal"]]
    #
    # filename_simply_set = [["He", "Madissoon", "Stewart", "Vento"],
    #                        ["Madissoon", "Stewart", "Vento", "He"],
    #                        ["Stewart", "Vento", "He", "Madissoon"],
    #                        ["Vento", "He", "Madissoon", "Stewart"]]

    result_list = []

    for i in range(0, len(filename_set)):
        # for i in range(args.num, args.num + 1):
        cell_number_list = []
        class_set_list = []
        filename = filename_set[i]
        filename_simply = filename_simply_set[i]
        dataname1 = filename[0]
        if dataname1 in ["Quake_10x", "Cao", "Vento-Tormo_10x"]:
            X1, cell_name1, gene_name1 = read_real_with_genes(dataname1, batch=False)
        else:
            X1, cell_name1, gene_name1 = read_real_with_genes_new(dataname1, batch=False)
        print(gene_name1)
        class_set1 = class_splitting_single(dataname1)
        index1 = []
        for i in range(len(cell_name1)):
            if cell_name1[i] in class_set1:
                index1.append(i)
        X1 = X1[index1]
        cell_name1 = cell_name1[index1]
        count_X1 = X1.astype(np.int)
        cell_number1 = X1.shape[0]
        print("for {} dataset, its cell number is {} and gene number is {}".format(dataname1, cell_number1,
                                                                                   len(gene_name1)))
        adata1 = anndata.AnnData(X1, var=pd.DataFrame(index=list(gene_name1)))
        adata1.obs["cellname"] = cell_name1
        cell_number_list.append(cell_number1)
        class_set_list.append(class_set1)

        dataname2 = filename[1]
        if dataname2 in ["Quake_10x", "Cao", "Vento-Tormo_10x"]:
            X2, cell_name2, gene_name2 = read_real_with_genes(dataname2, batch=False)
        else:
            X2, cell_name2, gene_name2 = read_real_with_genes_new(dataname2, batch=False)
        print(gene_name2)
        class_set2 = class_splitting_single(dataname2)
        index2 = []
        for i in range(len(cell_name2)):
            if cell_name2[i] in class_set2:
                index2.append(i)
        X2 = X2[index2]
        cell_name2 = cell_name2[index2]
        count_X2 = X2.astype(np.int)
        cell_number2 = X2.shape[0]
        print("for {} dataset, its cell number is {} and gene number is {}".format(dataname2, cell_number2,
                                                                                   len(gene_name2)))
        adata2 = anndata.AnnData(X2, var=pd.DataFrame(index=list(gene_name2)))
        adata2.obs["cellname"] = cell_name2
        cell_number_list.append(cell_number1 + cell_number2)
        class_set_list.append(class_set2)

        dataname3 = filename[2]
        if dataname3 in ["Quake_10x", "Cao", "Vento-Tormo_10x"]:
            X3, cell_name3, gene_name3 = read_real_with_genes(dataname3, batch=False)
        else:
            X3, cell_name3, gene_name3 = read_real_with_genes_new(dataname3, batch=False)
        print(gene_name3)
        class_set3 = class_splitting_single(dataname3)
        index3 = []
        for i in range(len(cell_name3)):
            if cell_name3[i] in class_set3:
                index3.append(i)
        X3 = X3[index3]
        cell_name3 = cell_name3[index3]
        count_X3 = X3.astype(np.int)
        cell_number3 = X3.shape[0]
        print("for {} dataset, its cell number is {} and gene number is {}".format(dataname3, cell_number3,
                                                                                   len(gene_name3)))
        adata3 = anndata.AnnData(X3, var=pd.DataFrame(index=list(gene_name3)))
        adata3.obs["cellname"] = cell_name3
        cell_number_list.append(cell_number1 + cell_number2 + cell_number3)
        class_set_list.append(class_set3)

        dataname4 = filename[3]
        if dataname4 in ["Quake_10x", "Cao", "Vento-Tormo_10x"]:
            X4, cell_name4, gene_name4 = read_real_with_genes(dataname4, batch=False)
        else:
            X4, cell_name4, gene_name4 = read_real_with_genes_new(dataname4, batch=False)
        print(gene_name4)
        class_set4 = class_splitting_single(dataname4)
        index4 = []
        for i in range(len(cell_name4)):
            if cell_name4[i] in class_set4:
                index4.append(i)
        X4 = X4[index4]
        cell_name4 = cell_name4[index4]
        count_X4 = X4.astype(np.int)
        cell_number4 = X4.shape[0]
        print("for {} dataset, its cell number is {} and gene number is {}".format(dataname4, cell_number4,
                                                                                   len(gene_name4)))
        adata4 = anndata.AnnData(X4, var=pd.DataFrame(index=list(gene_name4)))
        adata4.obs["cellname"] = cell_name4
        cell_number_list.append(cell_number1 + cell_number2 + cell_number3 + cell_number4)
        class_set_list.append(class_set4)

        adata = anndata.concat([adata1, adata2, adata3, adata4], join="inner")
        count_X = adata.X.astype(np.int)
        print("for mixed dataset, its cell number is {} and gene number is {}".format(count_X.shape[0], count_X.shape[1]))
        adata = normalize(adata, highly_genes=args.highly_genes, size_factors=True, normalize_input=True,
                          logtrans_input=True)
        X = adata.X.astype(np.float32)
        cell_name = np.array(adata.obs["cellname"])
        print("after preprocessing, the cell number is {} and the gene dimension is {}".format(len(cell_name), X.shape[1]))

        if args.highly_genes != None:
            adata_index = list(np.array(adata.var.index))
            high_variable = list(np.array(adata.var.highly_variable.index))
            index = []
            for i in range(len(high_variable)):
                index.append(adata_index.index(high_variable[i]))
            index = np.array(index, dtype=np.int)
            count_X = count_X[:, index]
        assert X.shape == count_X.shape
        size_factor = np.array(adata.obs.size_factors).reshape(-1, 1).astype(np.float32)

        labeled_ratio = args.ra  # 0.5
        stage_number = args.stage
        source_X_set, source_count_X_set, source_cellname_set, source_size_factor_set, source_Y_set, \
        target_X_set, target_count_X_set, target_cellname_set, target_size_factor_set, target_Y_set, unique_class_set_list \
            = dataset_spliting(X, count_X, cell_name, size_factor, cell_number_list, class_set_list,
                               labeled_ratio=labeled_ratio, random_seed=args.random_seed)
        print("we have finished the dataset splitting process!!!")

        source_batchname_set = []
        target_batchname_set = []
        for i in range(len(source_X_set)):
            source_batchname_set.append([filename_simply[i]] * len(source_Y_set[i]))
        for j in range(len(target_X_set)):
            target_batchname_set.append([filename_simply[j]] * len(target_Y_set[j]))

        if args.structure == 0:
            model = AutoEncoder(X.shape[1], 32, encodeLayer=[256, 64], decodeLayer=[64, 256], activation="relu")
        else:
            model = AutoEncoder(X.shape[1], 128, encodeLayer=[512, 256], decodeLayer=[256, 512], activation="relu")
        model = model.to(device)

        class_number_set = [0]
        current_result = filename
        for current_stage in range(stage_number):
            source_x = source_X_set[current_stage]
            source_raw_x = source_count_X_set[current_stage]
            source_cellname = source_cellname_set[current_stage]
            source_batchname = source_batchname_set[current_stage]
            source_sf = source_size_factor_set[current_stage]
            source_y = source_Y_set[current_stage]

            target_x = target_X_set[current_stage]
            target_raw_x = target_count_X_set[current_stage]
            target_cellname = target_cellname_set[current_stage]
            target_batchname = target_batchname_set[current_stage]
            target_sf = target_size_factor_set[current_stage]
            target_y = target_Y_set[current_stage]

            if current_stage == 0:
                unified_target_x = target_x
                unified_target_raw_x = target_raw_x
                unified_target_cellname = target_cellname
                unified_target_batchname = target_batchname
                unified_target_sf = target_sf
                unified_target_y = target_y
                class_number_set.append(len(np.unique(unified_target_y)))
                print("the class set is {}".format(class_number_set))
            else:
                last_target_x = unified_target_x
                last_target_raw_x = unified_target_raw_x
                last_target_cellname = unified_target_cellname
                last_target_batchname = unified_target_batchname
                last_target_sf = unified_target_sf
                last_target_y = unified_target_y

                unified_target_x = np.concatenate((unified_target_x, target_x), axis=0)
                unified_target_raw_x = np.concatenate((unified_target_raw_x, target_raw_x), axis=0)
                unified_target_sf = np.concatenate((unified_target_sf, target_sf), axis=0)
                unified_target_cellname = np.concatenate((unified_target_cellname, target_cellname))
                unified_target_batchname = np.concatenate((unified_target_batchname, target_batchname))
                unified_target_y = np.concatenate((unified_target_y, target_y))
                class_number_set.append(len(np.unique(unified_target_y)))
                print("the class set is {}".format(class_number_set))

            current_classes = len(np.unique(unified_target_y))
            if current_stage > 0:
                unified_source_x = np.concatenate((source_x, source_x_memory), axis=0)
                unified_source_raw_x = np.concatenate((source_raw_x, source_raw_x_memory), axis=0)
                unified_source_cellname = np.concatenate((source_cellname, source_cellname_memory))
                unified_source_sf = np.concatenate((source_sf, source_sf_memory), axis=0)
                unified_source_y = np.concatenate((source_y, source_y_memory))
            else:
                unified_source_x = source_x
                unified_source_raw_x = source_raw_x
                unified_source_cellname = source_cellname
                unified_source_sf = source_sf
                unified_source_y = source_y
            unified_freq = Counter(unified_source_y)
            unified_class_weight = {x: 1.0 / unified_freq[x] for x in unified_freq}
            unified_source_weights = [unified_class_weight[x] for x in unified_source_y]
            unified_sampler = WeightedRandomSampler(unified_source_weights, len(unified_source_y))

            if source_x.shape[0] < args.batch_size:
                args.batch_size = source_x.shape[0]

            if args.structure == 0:
                proto_net = Prototype(current_classes, 32, tau=args.tau)
            else:
                proto_net = Prototype(current_classes, 128, tau=args.tau)
            proto_net = proto_net.to(device)
            if current_stage > 0:
                state_dict = proto_net.state_dict()
                state_dict['fc.weight'][:class_number_set[-2]] = F.normalize(prototype_weight_store).to(device)
                proto_net.load_state_dict(state_dict)
                print("In the {}-th state, we have loaded the prototype weight in the last stage successfully".format(current_stage))
                proto_net.fc.weight[:class_number_set[-2]].detach()

            source_dataset = TensorDataset(torch.tensor(unified_source_x), torch.tensor(unified_source_raw_x), torch.tensor(unified_source_sf),
                                           torch.tensor(unified_source_y), torch.arange(unified_source_x.shape[0]))
            source_dataloader = DataLoader(source_dataset, batch_size=args.batch_size, sampler=unified_sampler, drop_last=True)
            target_dataset = TensorDataset(torch.tensor(unified_target_x), torch.tensor(unified_target_raw_x), torch.tensor(unified_target_sf),
                                           torch.tensor(unified_target_y), torch.arange(unified_target_x.shape[0]))
            target_dataloader = DataLoader(target_dataset, batch_size=args.batch_size, shuffle=True, drop_last=True)
            train_dataset = TensorDataset(torch.tensor(source_x), torch.tensor(source_raw_x), torch.tensor(source_sf),
                                           torch.tensor(source_y), torch.arange(source_x.shape[0]))
            train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=False)
            test_dataset = TensorDataset(torch.tensor(target_x), torch.tensor(target_raw_x), torch.tensor(target_sf),
                                           torch.tensor(target_y), torch.arange(target_x.shape[0]))
            test_dataloader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False)

            if current_stage > 0:
                last_test_dataset = TensorDataset(torch.tensor(last_target_x), torch.tensor(last_target_raw_x), torch.tensor(last_target_sf),
                                                   torch.tensor(last_target_y), torch.arange(last_target_x.shape[0]))
                last_test_dataloader = DataLoader(last_test_dataset, batch_size=args.batch_size, shuffle=False)

            optimizer = optim.Adam(itertools.chain(model.parameters(), proto_net.parameters()), lr=args.lr, amsgrad=True)

            bce = nn.BCELoss().to(device)
            ce = nn.CrossEntropyLoss().to(device)

            if current_stage == 0:
                for epoch in range(args.pretrain + args.finetune + 1):
                    if epoch % args.interval == 0:
                        model.eval()
                        proto_net.eval()
                        preds = np.array([])
                        targets = np.array([])
                        confs = np.array([])

                        test_embeddings = []
                        test_indexes = []

                        with torch.no_grad():
                            for _, data in enumerate(test_dataloader):
                                x_t, label_t, index_t = data[0].to(device), data[3].to(device), data[4].to(device)
                                z_t, _, _, _ = model(x_t)
                                output_t = proto_net(z_t)
                                conf, pred = output_t.max(1)
                                targets = np.append(targets, label_t.cpu().numpy())
                                preds = np.append(preds, pred.cpu().numpy())
                                confs = np.append(confs, conf.cpu().numpy())

                                test_embeddings.append(z_t.detach())
                                test_indexes.append(index_t)

                        targets = targets.astype(int)
                        preds = preds.astype(int)
                        overall_acc = accuracy(preds, targets)
                        print("In the {}-th stage and {}-th epoch, Test overall acc for this stage {:.4f}".format(current_stage, epoch, overall_acc))
                        model.train()
                        proto_net.train()
                        if epoch == args.pretrain + args.finetune:
                            current_result.extend([0., round(overall_acc, 4), round(overall_acc, 4)])

                            test_embeddings = torch.cat(test_embeddings, dim=0)
                            test_indexes = torch.cat(test_indexes)
                            _, test_indexes = torch.sort(test_indexes, descending=False)
                            test_embeddings = test_embeddings[test_indexes].cpu().numpy()
                            test_indexes = test_indexes.cpu().numpy()
                            test_true_labels = targets[test_indexes]
                            test_pred_labels = preds[test_indexes]
                            test_true_celltypes = target_cellname
                            test_true_batchname = target_batchname
                            test_pred_celltypes = target_cellname.copy()
                            unique_test_preds = np.unique(test_pred_labels)
                            for j in range(len(unique_test_preds)):
                                test_pred_celltypes[test_pred_labels == unique_test_preds[j]] = unique_class_set_list[
                                    unique_test_preds[j]]
                            test_data_infor = pd.DataFrame(
                                {"true label": test_true_labels, "true cell type": test_true_celltypes,
                                 "pred label": test_pred_labels, "pred cell type": test_pred_celltypes,
                                 "true domain": test_true_batchname})
                            test_data_infor.to_csv(
                                "case2/{}_{}_{}_{}_stage_{}_test_data_replay_and_proxy_and_uniform_sankey_information.csv".format(
                                    filename_simply[0], filename_simply[1], filename_simply[2], filename_simply[3], current_stage))
                            pd.DataFrame(test_embeddings).to_csv(
                                "case2/{}_{}_{}_{}_stage_{}_test_data_replay_and_proxy_and_uniform_visualization_feature.csv".format(
                                    filename_simply[0], filename_simply[1], filename_simply[2], filename_simply[3], current_stage))


                    recon_losses = AverageMeter('recon_loss', ':.4e')
                    pcr_losses = AverageMeter('pcr_loss', ':.4e')
                    cwd_losses = AverageMeter('cwd_loss', ':.4e')
                    model.train()
                    proto_net.train()

                    for batch_idx, (x_s, raw_x_s, sf_s, y_s, index_s) in enumerate(source_dataloader):
                        x_s, raw_x_s, sf_s, y_s, index_s = x_s.to(device), raw_x_s.to(device), \
                                                           sf_s.to(device), y_s.to(device), \
                                                           index_s.to(device)
                        z_s, mean_s, disp_s, pi_s = model(x_s)
                        recon_loss = ZINBLoss().to(device)(x=raw_x_s, mean=mean_s, disp=disp_s, pi=pi_s,
                                                           scale_factor=sf_s)

                        w_s = proto_net.fc.weight[y_s]
                        z_norm = torch.norm(z_s, p=2, dim=1).unsqueeze(1).expand_as(z_s)
                        z_normalized = z_s.div(z_norm + 0.000001)
                        w_norm = torch.norm(w_s, p=2, dim=1).unsqueeze(1).expand_as(w_s)
                        w_normalized = w_s.div(w_norm + 0.000001)
                        cos_features = torch.cat([z_normalized.unsqueeze(1), w_normalized.unsqueeze(1)], dim=1)
                        PSC = SupConLoss(temperature=args.tau, contrast_mode='proxy')
                        pcr_loss = PSC(features=cos_features, labels=y_s, device=device)

                        output_s = proto_net(z_s)
                        pui_s = torch.mm(F.normalize(output_s.t(), p=2, dim=1), F.normalize(output_s, p=2, dim=0))
                        cwd_loss = nn.CrossEntropyLoss()(pui_s, torch.arange(pui_s.size(0)).to(device))

                        if epoch < args.pretrain:
                            loss = recon_loss
                        else:
                            loss = recon_loss + pcr_loss + cwd_loss
                        recon_losses.update(recon_loss.item(), args.batch_size)
                        pcr_losses.update(pcr_loss.item(), args.batch_size)
                        cwd_losses.update(cwd_loss.item(), args.batch_size)
                        optimizer.zero_grad()
                        loss.backward()
                        optimizer.step()
                    print("In {}-th stage, Training {}/{}, zinb loss: {:.4f}, pcr loss: {:.4f}, cwd loss: {:.4f}".format(current_stage, epoch, args.pretrain + args.finetune + 1,
                                                                                                                          recon_losses.avg, pcr_losses.avg, cwd_losses.avg))

            else:
                for epoch in range(args.finetune + 1):
                    if epoch % args.interval == 0:
                        model.eval()
                        proto_net.eval()
                        preds = np.array([])
                        targets = np.array([])
                        confs = np.array([])

                        test_embeddings = []
                        test_indexes = []

                        with torch.no_grad():
                            for _, data in enumerate(test_dataloader):
                                x_t, label_t, index_t = data[0].to(device), data[3].to(device), data[4].to(device)
                                z_t, _, _, _ = model(x_t)
                                output_t = proto_net(z_t)
                                conf, pred = output_t.max(1)
                                targets = np.append(targets, label_t.cpu().numpy())
                                preds = np.append(preds, pred.cpu().numpy())
                                confs = np.append(confs, conf.cpu().numpy())

                                test_embeddings.append(z_t.detach())
                                test_indexes.append(index_t)

                        targets = targets.astype(int)
                        preds = preds.astype(int)
                        acc = accuracy(preds, targets)
                        print("In the {}-th stage and {}-th epoch, Test acc for this stage {:.4f}".format(current_stage, epoch, acc))

                        last_preds = np.array([])
                        last_targets = np.array([])

                        last_test_embeddings = []
                        last_test_indexes = []

                        with torch.no_grad():
                            for _, data in enumerate(last_test_dataloader):
                                x_t, label_t, index_t = data[0].to(device), data[3].to(device), data[4].to(device)
                                z_t, _, _, _ = model(x_t)
                                output_t = proto_net(z_t)
                                conf, pred = output_t.max(1)
                                last_targets = np.append(last_targets, label_t.cpu().numpy())
                                last_preds = np.append(last_preds, pred.cpu().numpy())

                                last_test_embeddings.append(z_t.detach())
                                last_test_indexes.append(index_t)

                        last_targets = last_targets.astype(int)
                        last_preds = last_preds.astype(int)
                        last_overall_acc = accuracy(last_preds, last_targets)
                        print("In the {}-th stage and {}-th epoch, Test acc for previous stage {:.4f}".format(
                            current_stage, epoch, last_overall_acc))

                        overall_targets = np.concatenate((targets, last_targets))
                        overall_preds = np.concatenate((preds, last_preds))
                        overall_acc = accuracy(overall_preds, overall_targets)
                        print("In the {}-th stage and {}-th epoch, Test acc for overall stage {:.4f}".format(
                            current_stage, epoch, overall_acc))

                        model.train()
                        proto_net.train()
                        if epoch == args.finetune:
                            current_result.extend([round(last_overall_acc, 4), round(acc, 4), round(overall_acc, 4)])

                            test_embeddings = torch.cat(test_embeddings, dim=0)
                            test_indexes = torch.cat(test_indexes)
                            _, test_indexes = torch.sort(test_indexes, descending=False)
                            test_embeddings = test_embeddings[test_indexes].cpu().numpy()
                            test_indexes = test_indexes.cpu().numpy()
                            test_true_labels = targets[test_indexes]
                            test_pred_labels = preds[test_indexes]
                            test_true_celltypes = target_cellname
                            test_true_batchname = target_batchname
                            test_pred_celltypes = target_cellname.copy()
                            unique_test_preds = np.unique(test_pred_labels)
                            for j in range(len(unique_test_preds)):
                                test_pred_celltypes[test_pred_labels == unique_test_preds[j]] = unique_class_set_list[
                                    unique_test_preds[j]]

                            last_test_embeddings = torch.cat(last_test_embeddings, dim=0)
                            last_test_indexes = torch.cat(last_test_indexes)
                            _, last_test_indexes = torch.sort(last_test_indexes, descending=False)
                            last_test_embeddings = last_test_embeddings[last_test_indexes].cpu().numpy()
                            last_test_indexes = last_test_indexes.cpu().numpy()
                            last_test_true_labels = last_targets[last_test_indexes]
                            last_test_pred_labels = last_preds[last_test_indexes]
                            last_test_true_celltypes = last_target_cellname
                            last_test_true_batchname = last_target_batchname
                            last_test_pred_celltypes = last_target_cellname.copy()
                            unique_last_test_preds = np.unique(last_test_pred_labels)
                            for j in range(len(unique_last_test_preds)):
                                last_test_pred_celltypes[last_test_pred_labels == unique_last_test_preds[j]] = \
                                unique_class_set_list[unique_last_test_preds[j]]

                            overall_test_embeddings = np.concatenate((last_test_embeddings, test_embeddings), axis=0)
                            overall_test_true_labels = np.concatenate((last_test_true_labels, test_true_labels))
                            overall_test_pred_labels = np.concatenate((last_test_pred_labels, test_pred_labels))
                            overall_test_true_celltypes = np.concatenate(
                                (last_test_true_celltypes, test_true_celltypes))
                            overall_test_true_batchname = np.concatenate(
                                (last_test_true_batchname, test_true_batchname))
                            overall_test_pred_celltypes = np.concatenate(
                                (last_test_pred_celltypes, test_pred_celltypes))
                            overall_test_data_infor = pd.DataFrame(
                                {"true label": overall_test_true_labels, "true cell type": overall_test_true_celltypes,
                                 "pred label": overall_test_pred_labels, "pred cell type": overall_test_pred_celltypes,
                                 "true domain": overall_test_true_batchname})
                            overall_test_data_infor.to_csv(
                                "case2/{}_{}_{}_{}_stage_{}_test_data_replay_and_proxy_and_uniform_sankey_information.csv".format(
                                    filename_simply[0], filename_simply[1], filename_simply[2], filename_simply[3], current_stage))
                            pd.DataFrame(overall_test_embeddings).to_csv(
                                "case2/{}_{}_{}_{}_stage_{}_test_data_replay_and_proxy_and_uniform_visualization_feature.csv".format(
                                    filename_simply[0], filename_simply[1], filename_simply[2], filename_simply[3], current_stage))

                    recon_losses = AverageMeter('recon_loss', ':.4e')
                    pcr_losses = AverageMeter('pcr_loss', ':.4e')
                    cwd_losses = AverageMeter('cwd_loss', ':.4e')
                    model.train()
                    proto_net.train()

                    for batch_idx, (x_s, raw_x_s, sf_s, y_s, index_s) in enumerate(source_dataloader):
                        x_s, raw_x_s, sf_s, y_s, index_s = x_s.to(device), raw_x_s.to(device), \
                                                           sf_s.to(device), y_s.to(device), \
                                                           index_s.to(device)
                        z_s, mean_s, disp_s, pi_s = model(x_s)
                        recon_loss = ZINBLoss().to(device)(x=raw_x_s, mean=mean_s, disp=disp_s, pi=pi_s,
                                                           scale_factor=sf_s)

                        w_s = proto_net.fc.weight[y_s]
                        z_norm = torch.norm(z_s, p=2, dim=1).unsqueeze(1).expand_as(z_s)
                        z_normalized = z_s.div(z_norm + 0.000001)
                        w_norm = torch.norm(w_s, p=2, dim=1).unsqueeze(1).expand_as(w_s)
                        w_normalized = w_s.div(w_norm + 0.000001)
                        cos_features = torch.cat([z_normalized.unsqueeze(1), w_normalized.unsqueeze(1)], dim=1)
                        PSC = SupConLoss(temperature=args.tau, contrast_mode='proxy')
                        pcr_loss = PSC(features=cos_features, labels=y_s, device=device)

                        output_s = proto_net(z_s)
                        pui_s = torch.mm(F.normalize(output_s.t(), p=2, dim=1), F.normalize(output_s, p=2, dim=0))
                        cwd_loss = nn.CrossEntropyLoss()(pui_s, torch.arange(pui_s.size(0)).to(device))

                        loss = recon_loss + pcr_loss + cwd_loss
                        recon_losses.update(recon_loss.item(), args.batch_size)
                        pcr_losses.update(pcr_loss.item(), args.batch_size)
                        cwd_losses.update(cwd_loss.item(), args.batch_size)
                        optimizer.zero_grad()
                        loss.backward()
                        optimizer.step()
                    print("In {}-th stage, Training {}/{}, zinb loss: {:.4f}, pcr loss: {:.4f}, cwd loss: {:.4f}".format(current_stage, epoch, args.finetune + 1,
                                                                                                                          recon_losses.avg, pcr_losses.avg, cwd_losses.avg))

            prototype_weight_store = proto_net.fc.weight.data
            source_embeddings, source_labels = extractor(model, train_dataloader, device)
            assert source_embeddings.shape[0] == source_x.shape[0]
            source_labels_unique = np.unique(source_labels)
            source_centers = np.zeros((len(source_labels_unique), source_embeddings.shape[1]))
            for i in range(len(source_labels_unique)):
                source_centers[i] = np.mean(source_embeddings[source_labels == source_labels_unique[i]], axis=0)
            select_indexes = []
            source_embeddings = torch.from_numpy(source_embeddings).to(device).float()
            source_centers = torch.from_numpy(source_centers).to(device).float()
            source_distances = torch.matmul(F.normalize(source_embeddings), F.normalize(source_centers).t())

            for j in range(len(source_labels_unique)):
                source_scores = source_distances[:, j]
                if len(source_labels[source_labels == source_labels_unique[j]]) < args.top_k:
                    topk_index = torch.topk(source_scores, k=len(source_labels[source_labels == source_labels_unique[j]]))[1]
                else:
                    topk_index = torch.topk(source_scores, k=args.top_k)[1]
                select_indexes.extend(list(topk_index.cpu().numpy()))
            if current_stage == 0:
                source_x_memory = source_x[select_indexes]
                source_raw_x_memory = source_raw_x[select_indexes]
                source_cellname_memory = source_cellname[select_indexes]
                source_sf_memory = source_sf[select_indexes]
                source_y_memory = source_y[select_indexes]
            else:
                source_x_memory = np.concatenate((source_x_memory, source_x[select_indexes]), axis=0)
                source_raw_x_memory = np.concatenate((source_raw_x_memory, source_raw_x[select_indexes]), axis=0)
                source_cellname_memory = np.concatenate((source_cellname_memory, source_cellname[select_indexes]))
                source_sf_memory = np.concatenate((source_sf_memory, source_sf[select_indexes]), axis=0)
                source_y_memory = np.concatenate((source_y_memory, source_y[select_indexes]))
        result_list.append(current_result)
        print("current result list is {}".format(result_list))
    result_list = pd.DataFrame(np.array(result_list))
    print(result_list)








