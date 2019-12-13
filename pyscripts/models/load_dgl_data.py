import numpy as np
import json
import itertools
import torch
import networkx as nx
import dgl.data
from dgl import DGLGraph

class NodeClassificationDataset:
    def __init__(self, graph, features, labels, train_masks, stopping_masks,
                        val_masks, test_mask, n_edges, n_classes, n_feats):
        self.graph = graph
        self.features = features
        self.labels = labels
        self.train_masks = train_masks
        self.stopping_masks = stopping_masks
        self.val_masks = val_masks
        self.test_mask = test_mask
        self.n_edges = n_edges
        self.n_classes = n_classes
        self.n_feats = n_feats


def from_file(filename):
    data = json.load(open(filename))
    features = torch.FloatTensor(np.array(data['features']))
    labels = torch.LongTensor(np.array(data['labels']))
    if hasattr(torch, 'BoolTensor'):
        train_masks = [torch.BoolTensor(tr) for tr in data['train_masks']]
        val_masks = [torch.BoolTensor(val) for val in data['val_masks']]
        stopping_masks = [torch.BoolTensor(st) for st in data['stopping_masks']]
        test_mask = torch.BoolTensor(data['test_mask'])
    else:
        train_masks = [torch.ByteTensor(tr) for tr in data['train_masks']]
        val_masks = [torch.ByteTensor(val) for val in data['val_masks']]
        stopping_masks = [torch.ByteTensor(st) for st in data['stopping_masks']]
        test_mask = torch.ByteTensor(data['test_mask'])
    n_feats = features.shape[1]
    n_classes = len(set(data['labels']))

    g = DGLGraph()
    g.add_nodes(len(data['features']))
    edge_list = list(itertools.chain(*[[(i, nb) for nb in nbs] for i,nbs in enumerate(data['links'])]))
    n_edges = len(edge_list)
    # add edges two lists of nodes: src and dst
    src, dst = tuple(zip(*edge_list))
    g.add_edges(src, dst)
    # edges are directional in DGL; make them bi-directional
    g.add_edges(dst, src)
    return NodeClassificationDataset(g, features, labels, train_masks, stopping_masks,
                                    val_masks, test_mask, n_edges, n_classes, n_feats)


def from_builtin(args):
    data = dgl.data.load_data(args)
    features = torch.FloatTensor(data.features)
    labels = torch.LongTensor(data.labels)
    if hasattr(torch, 'BoolTensor'):
        train_masks = [torch.BoolTensor(data.train_mask)]
        val_masks = [torch.BoolTensor(data.val_mask *
            [i % 2 == 0 for i in range(len(data.val_mask))])]
        stopping_masks = [torch.BoolTensor(data.val_mask *
            [i % 2 == 0 for i in range(len(data.val_mask))])]
        test_mask = torch.BoolTensor(data.test_mask)
    else:
        train_masks = [torch.ByteTensor(data.train_mask)]
        val_masks = [torch.ByteTensor(data.val_mask *
            [i % 2 == 0 for i in range(len(data.val_mask))])]
        stopping_masks = [torch.ByteTensor(data.val_mask *
            [i % 2 == 0 for i in range(len(data.val_mask))])]
        test_mask = torch.ByteTensor(data.test_mask)
    n_feats = features.shape[1]
    n_classes = data.num_labels
    n_edges = data.graph.number_of_edges()
    # graph preprocess
    g = data.graph
    # add self loop
    if args.self_loop:
        g.remove_edges_from(nx.selfloop_edges(g))
        g.add_edges_from(zip(g.nodes(), g.nodes()))
    g = DGLGraph(g)
    return NodeClassificationDataset(g, features, labels, train_masks, stopping_masks,
                                    val_masks, test_mask, n_edges, n_classes, n_feats)


def load(args):
    try:
        data = from_builtin(args)
    except ValueError:
        data = from_file(args.dataset)

    print("""----Data statistics------'
      #Edges %d
      #Classes %d
      #Train samples %d
      #Val samples %d
      #Stopping samples %d
      #Test samples %d""" %
          (data.n_edges, data.n_classes,
              data.train_masks[0].int().sum().item(),
              data.val_masks[0].int().sum().item(),
              data.stopping_masks[0].int().sum().item(),
              data.test_mask.int().sum().item()))

    # Preprocess graph
    if args.gpu < 0:
        cuda = False
    else:
        cuda = True
        torch.cuda.set_device(args.gpu)
        data.features = data.features.cuda()
        data.labels = data.labels.cuda()
        data.train_mask = data.train_mask.cuda()
        data.val_mask = data.val_mask.cuda()
        data.test_mask = data.test_mask.cuda()

    # graph normalization
    degs = data.graph.in_degrees().float()
    norm = torch.pow(degs, -0.5)
    norm[torch.isinf(norm)] = 0
    if cuda:
        norm = norm.cuda()
    data.graph.ndata['norm'] = norm.unsqueeze(1)
    return data
