import argparse, time
import numpy as np
import seaborn as sns
import json
import os
import itertools
import string
import networkx as nx
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from dgl import DGLGraph

from load_graph_data import register_data_args


def accuracy(logits, labels, mask=None):
    if mask is not None:
        logits = logits[mask]
        labels = labels[mask]
    _, indices = torch.max(logits, dim=1)
    correct = torch.sum(indices == labels)
    acc = correct.item() * 1.0 / len(labels)
    return acc


def loss_scalar(logits, labels, mask, loss_fcn):
    if mask is not None:
        logits = logits[mask]
        labels = labels[mask]
    return loss_fcn(logits, labels).cpu().numpy().mean()


def evaluate(model, features, labels, mask, loss_fcn=None):
    model.eval()
    with torch.no_grad():
        logits = model(features)
        acc = accuracy(logits, labels, mask)
        if loss_fcn is None:
            return acc
        else:
            return acc, loss_scalar(logits, labels, mask, loss_fcn)


printable = set(string.printable)
def strip_to_ascii(s):
    return ''.join(filter(lambda x: x in printable, s))


def compile_metadata(data, split_idx, text_metadata=None):
    labels = data.labels.cpu().tolist()
    splits = ['train' if data.train_masks[split_idx][i] else
              'stopping' if data.stopping_masks[split_idx][i] else
              'val' if data.val_masks[split_idx][i] else
              'test' for i in range(len(data.features))]
    ids = range(len(data.features))
    metadata_header = ['id', 'label_id', 'split']
    if text_metadata is not None:
        label_names = [text_metadata['labels'][str(lab)] for lab in labels]
        node_names = [strip_to_ascii(text_metadata['nodes'][id]['title'])
                        for id in ids]
        metadata_header += ['label_names', 'node_names']
        return (metadata_header,
            list(zip(ids, labels, splits, label_names, node_names)))
    else:
        return (metadata_header, list(zip(ids, labels, splits)))


def train_and_eval_once(data, model, split_idx, stopping_patience, lr,
                weight_decay, output_dir, output_preds=False,
                output_model=False, test=False,
                embedding_log_freq=40, text_metadata=None):
    max_acc = 0
    patience_left = stopping_patience
    best_vars = None
    epoch = 0

    loss_fcn = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(),
                                 lr=lr,
                                 weight_decay=weight_decay)

    metadata_header, metadata = compile_metadata(
        data, split_idx, text_metadata)

    writer = SummaryWriter(output_dir)
    writer.add_graph(model, data.features)
    writer.add_embedding(data.features, metadata_header=metadata_header,
                        metadata=metadata, tag='features')


    while patience_left > 0:
        model.train()
        logits = model(data.features)
        loss = loss_fcn(logits[data.train_masks[split_idx]],
                        data.labels[data.train_masks[split_idx]])

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        train_acc = accuracy(logits, data.labels,
        mask=data.train_masks[split_idx])
        train_loss = loss.cpu().detach().numpy().mean()

        model.eval()
        with torch.no_grad():
            eval_logits = model(data.features)
            stopping_acc = accuracy(eval_logits, data.labels,
                                    data.stopping_masks[split_idx])
            stopping_loss = loss_scalar(eval_logits, data.labels,
                                        data.stopping_masks[split_idx],
                                        loss_fcn)
            val_acc = accuracy(eval_logits, data.labels,
                                data.val_masks[split_idx])
            val_loss = loss_scalar(eval_logits, data.labels,
                                   data.val_masks[split_idx], loss_fcn)
            if test:
                test_acc = accuracy(eval_logits, data.labels, data.test_mask)
                test_loss = loss_scalar(eval_logits, data.labels,
                                        data.test_mask, loss_fcn)

        if stopping_acc > max_acc:
            max_acc = stopping_acc
            patience_left = stopping_patience
            best_vars = {
                key: value.clone()
                for key, value in model.state_dict().items()
            }
        else:
            patience_left -= 1

        writer.add_scalar('loss/train', train_loss, epoch)
        writer.add_scalar('loss/stopping', stopping_loss, epoch)
        writer.add_scalar('loss/val', val_loss, epoch)
        writer.add_scalar('accuracy/train', train_acc, epoch)
        writer.add_scalar('accuracy/stopping', stopping_acc, epoch)
        writer.add_scalar('accuracy/val', val_acc, epoch)
        if test:
            writer.add_scalar('loss/test', test_loss, epoch)
            writer.add_scalar('accuracy/test', test_acc, epoch)
        if (epoch % embedding_log_freq == 0 and
            hasattr(model, 'get_last_embeddings')):
            writer.add_embedding(model.get_last_embeddings(),
                                 metadata=metadata,
                                 metadata_header=metadata_header,
                                 global_step=epoch,
                                 tag='out_embeddings')
        epoch += 1

    model.load_state_dict(best_vars)
    result = { 'epochs': epoch }
    result['train_acc'], result['train_loss'] = evaluate(
        model, data.features, data.labels,
        data.train_masks[split_idx], loss_fcn
    )
    if test:
        result['val_acc'], result['val_loss'] = evaluate(
            model, data.features, data.labels,
            data.test_mask, loss_fcn
        )
    else:
        result['val_acc'], result['val_loss'] = evaluate(
            model, data.features, data.labels,
            data.val_masks[split_idx], loss_fcn
        )

    if hasattr(model, 'get_last_embeddings'):
        writer.add_embedding(model.get_last_embeddings(),
                             metadata=metadata,
                             metadata_header=metadata_header,
                             global_step=epoch,
                             tag='final_out_embeddings')

    if output_preds:
        logits = model(data.features)
        _, preds = torch.max(logits, dim=1)
        preds = preds*~data.test_mask - 1*data.test_mask
        with open(os.path.join(output_dir, 'preds.json'), 'w') as out:
            json.dump(preds.tolist(), out)

    if output_model:
        torch.save(model.state_dict(), os.path.join(output_dir, 'model.pt'))

    return result


def mean_with_uncertainty(values, n_boot, conf_threshold):
    values = np.array(values)
    avg = values.mean()
    bootstrap = sns.algorithms.bootstrap(
        values, func=np.mean, n_boot=n_boot)
    conf_int = sns.utils.ci(bootstrap, conf_threshold)
    return avg, np.max(np.abs(conf_int - avg))


def train_and_eval(model_fn, data, args, result_callback=None):
    train_accs = []
    train_losses = []
    val_accs = []
    val_losses = []
    epoch_counts = []

    text_metadata = None
    if args.metadata_file is not None:
        with open(args.metadata_file) as inp:
            text_metadata = json.load(inp)

    if args.max_splits is None or len(data.train_masks) <= args.max_splits:
        splits = len(data.train_masks)
    else:
        splits = args.max_splits
    for split_idx in range(splits):
        for run_idx in range(args.runs_per_split):
            run_dir = os.path.join(args.output_dir,
                                    'split_' + str(split_idx) +
                                    '_run_' + str(run_idx))
            model = model_fn(args, data)
            if args.gpu >= 0:
                model.cuda()
            res = train_and_eval_once(
                data, model, split_idx,
                args.patience, args.lr, args.weight_decay,
                run_dir,
                output_preds = args.output_preds,
                output_model = args.output_model,
                test = args.test,
                embedding_log_freq = args.embedding_log_freq,
                text_metadata = text_metadata
            )
            train_accs.append([res['train_acc']])
            train_losses.append(res['train_loss'])
            val_accs.append(res['val_acc'])
            val_losses.append(res['val_loss'])
            epoch_counts.append(res['epochs'])
            print('Split {} run {} accuracy: {:.2%}'
                    .format(split_idx, run_idx, res['val_acc']))
    mean_val_acc, val_acc_uncertainty = mean_with_uncertainty(val_accs,
        args.n_boot, args.conf_int)
    mean_val_loss, val_loss_uncertainty = mean_with_uncertainty(val_losses,
        args.n_boot, args.conf_int)

    print('{} accuracy: {:.2%} ± {:.2%}'.format(
        'Test' if args.test else 'Validation',
        mean_val_acc, val_acc_uncertainty))

    type = 'test' if args.test else 'val'
    results = {
        'train_acc': np.array(train_accs).mean(),
        'train_loss': np.array(train_losses).mean(),
        'epochs': np.array(epoch_counts).mean(),
        (type+'_acc'): mean_val_acc,
        (type+'_acc_uncertainty'): val_acc_uncertainty,
        (type+'_loss'): mean_val_loss,
        (type+'_loss_uncertainty'): val_loss_uncertainty
    }
    with open(os.path.join(args.output_dir, 'eval_summary.txt'), 'w') as out:
        json.dump({k: str(v) for k,v in results.items()}, out, indent=2)
    if result_callback is not None:
        result_callback(objective=mean_val_acc,
                        context=results)


def register_general_args(parser):
    register_data_args(parser)
    parser.add_argument('--metadata-file',
                        help='Mapping label and node IDs to readable names')
    parser.add_argument('--patience', type=int, default=100,
            help='epochs to train before giving up if accuracy does not '
                 'improve')
    parser.add_argument('--test', action='store_true',
            help='evaluate on test set after training (default=False)')
    parser.add_argument('--runs-per-split', type=int, default=5,
            help='how many times to train and eval on each split in full eval')
    parser.add_argument('--n-boot', type=int, default=1000,
            help='resampling count for bootstrap confidence interval '
                 'calculation in full eval')
    parser.add_argument('--conf-int', type=int, default=95,
            help='confidence interval probability for full eval')
    parser.add_argument('--max-splits', type=int,
            help='maximum number of different training splits to evaluate on. '
                 'Unbounded by default so all splits in dataset will be used')
    parser.add_argument('--output-dir',
                        help='Directory to write Tensorboard logs and eval '
                             'results to')
    parser.add_argument('--embedding-log-freq', type=int, default=40,
            help='how many epochs between writing embeddings to tensorboard')
    parser.add_argument('--output-preds', action='store_true',
            help='write predictions on train/validation set to file for'
                 'analysis')
    parser.add_argument('--output-model', action='store_true',
            help='write weights of trained model to file')
    parser.add_argument('--lr', type=float, default=1e-2,
            help='learning rate')
    parser.add_argument('--weight-decay', type=float, default=5e-4,
            help='Weight for L2 loss')
