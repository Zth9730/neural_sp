#! /usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2019 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Transformer language model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import logging
import os
import random
import shutil
import torch
import torch.nn as nn

from neural_sp.models.lm.lm_base import LMBase
from neural_sp.models.modules.positinal_embedding import PositionalEncoding
from neural_sp.models.modules.transformer import TransformerDecoderBlock
from neural_sp.models.torch_utils import tensor2np
from neural_sp.utils import mkdir_join

import matplotlib
matplotlib.use('Agg')

random.seed(1)

logger = logging.getLogger(__name__)


class TransformerLM(LMBase):
    """Transformer language model."""

    def __init__(self, args, save_path=None):

        super(LMBase, self).__init__()
        logger.info(self.__class__.__name__)

        self.lm_type = args.lm_type
        self.save_path = save_path

        self.d_model = args.transformer_d_model
        self.n_layers = args.n_layers
        self.n_heads = args.transformer_n_heads
        self.lsm_prob = args.lsm_prob
        self.mem_len = args.bptt
        if args.recog_mem_len > 0:
            self.mem_len = args.recog_mem_len

        self.vocab = args.vocab
        self.eos = 2
        self.pad = 3
        # NOTE: reserved in advance

        # for cache
        self.cache_theta = 0.2  # smoothing parameter
        self.cache_lambda = 0.2  # cache weight
        self.cache_ids = []
        self.cache_keys = []
        self.cache_attn = []

        self.embed = nn.Embedding(self.vocab, self.d_model, padding_idx=self.pad)
        self.pos_enc = PositionalEncoding(self.d_model, args.dropout_in, args.transformer_pe_type,
                                          args.transformer_param_init)
        self.layers = nn.ModuleList([copy.deepcopy(TransformerDecoderBlock(
            self.d_model, args.transformer_d_ff, args.transformer_attn_type,
            self.n_heads, args.dropout_hidden, args.dropout_att,
            args.dropout_residual * (l + 1) / self.n_layers,
            args.transformer_layer_norm_eps, args.transformer_ffn_activation, args.transformer_param_init,
            src_tgt_attention=False)) for l in range(self.n_layers)])
        self.norm_out = nn.LayerNorm(self.d_model, eps=args.transformer_layer_norm_eps)

        self.adaptive_softmax = None
        self.output = None
        if args.adaptive_softmax:
            self.adaptive_softmax = nn.AdaptiveLogSoftmaxWithLoss(
                self.d_model, self.vocab,
                cutoffs=[round(self.vocab / 15), 3 * round(self.vocab / 15)],
                # cutoffs=[self.vocab // 25, 3 * self.vocab // 5],
                div_value=4.0)
        else:
            self.output = nn.Linear(self.d_model, self.vocab)
            if args.tie_embedding:
                self.output.weight = self.embed.weight

        self.reset_parameters()

    @property
    def output_dim(self):
        return self.d_model

    def reset_parameters(self):
        """Initialize parameters with Xavier uniform distribution."""
        logging.info('===== Initialize %s =====' % self.__class__.__name__)
        # see https://github.com/pytorch/fairseq/blob/master/fairseq/models/transformer.py
        # embedding
        nn.init.normal_(self.embed.weight, mean=0., std=self.d_model**-0.5)
        nn.init.constant_(self.embed.weight[self.pad], 0)
        # output layer
        nn.init.xavier_uniform_(self.output.weight)
        nn.init.constant_(self.output.bias, 0.)

    def decode(self, ys, state=None, mems=None, cache=None, incremental=False):
        """Decode function.

        Args:
            ys (LongTensor): `[B, L]`
            state (list): dummy interfance for RNNLM
            mems (list): dummy interface for TransformerXL
            cache (list): length `L`, each of which contains a FloatTensor `[B, L-1, d_model]`
            incremental (bool): ASR decoding mode
        Returns:
            logits (FloatTensor): `[B, L, vocab]`
            out (FloatTensor): `[B, L, d_model]`
            new_cache (list): length `n_layers`, each of which contains a FloatTensor `[B, L, d_model]`
            new_mems: dummy interfance for TransformerXL

        """
        # for ASR decoding
        if cache is None:
            cache = [None] * self.n_layers

        # Create the self-attention mask
        bs, ylen = ys.size()[:2]
        if incremental and cache[0] is not None:
            ylen = cache[0].size(1) + 1
        causal_mask = ys.new_ones(ylen, ylen).byte()
        causal_mask = torch.tril(causal_mask, diagonal=0, out=causal_mask).unsqueeze(0)
        causal_mask = causal_mask.repeat([bs, 1, 1])

        new_cache = [None] * self.n_layers
        out = self.pos_enc(self.embed(ys.long()))
        for l, layer in enumerate(self.layers):
            out, yy_aws = layer(out, causal_mask, cache=cache[l])[:2]
            if incremental:
                new_cache[l] = out
            if not self.training and yy_aws is not None:
                setattr(self, 'yy_aws_layer%d' % l, tensor2np(yy_aws))
        out = self.norm_out(out)
        if self.adaptive_softmax is None:
            logits = self.output(out)
        else:
            logits = out

        return logits, out, new_cache

    def plot_attention(self, n_cols=4):
        """Plot attention for each head in all layers."""
        from matplotlib import pyplot as plt
        from matplotlib.ticker import MaxNLocator

        save_path = mkdir_join(self.save_path, 'att_weights')

        # Clean directory
        if save_path is not None and os.path.isdir(save_path):
            shutil.rmtree(save_path)
            os.mkdir(save_path)

        for l in range(self.n_layers):
            if not hasattr(self, 'yy_aws_layer%d' % l):
                continue

            yy_aws = getattr(self, 'yy_aws_layer%d' % l)

            plt.clf()
            fig, axes = plt.subplots(self.n_heads // n_cols, n_cols, figsize=(20, 8))
            for h in range(self.n_heads):
                if self.n_heads > n_cols:
                    ax = axes[h // n_cols, h % n_cols]
                else:
                    ax = axes[h]
                ax.imshow(yy_aws[-1, h, :, :], aspect="auto")
                ax.grid(False)
                ax.set_xlabel("Input (head%d)" % h)
                ax.set_ylabel("Output (head%d)" % h)
                ax.xaxis.set_major_locator(MaxNLocator(integer=True))
                ax.yaxis.set_major_locator(MaxNLocator(integer=True))

            fig.tight_layout()
            fig.savefig(os.path.join(save_path, 'layer%d.png' % (l)), dvi=500)
            plt.close()
