"""
Graph Attention Networks in DGL using SPMV optimization.
References
----------
Paper: https://arxiv.org/abs/1710.10903
Author's code: https://github.com/PetarV-/GAT
Pytorch implementation: https://github.com/Diego999/pyGAT
"""

import torch
import torch.nn as nn
import dgl.function as fn
from dgl.nn.pytorch.softmax import EdgeSoftmax
import torch.nn.functional as F

class GraphAttention2(nn.Module):
    def __init__(self, g, in_dim, out_dim,  num_heads, feat_drop, attn_drop, alpha, name, residual=False):
        super(GraphAttention2, self).__init__()
        self.g = g
        self.num_heads = num_heads
        self.name = name

        self.fc1 = nn.Linear(in_dim, in_dim, bias=False)
        self.fc2 = nn.Linear(in_dim, num_heads * out_dim, bias=False)
        if feat_drop:
            self.feat_drop = nn.Dropout(feat_drop)
        else:
            self.feat_drop = lambda x : x
        if attn_drop:
            self.attn_drop = nn.Dropout(attn_drop)
        else:
            self.attn_drop = lambda x : x
        self.attn_l = nn.Parameter(torch.Tensor(size=(num_heads, out_dim, 1)))
        self.attn_r = nn.Parameter(torch.Tensor(size=(num_heads, out_dim, 1)))
        nn.init.xavier_normal_(self.fc1.weight.data, gain=1.414)
        nn.init.xavier_normal_(self.fc2.weight.data, gain=1.414)
        nn.init.xavier_normal_(self.attn_l.data, gain=1.414)
        nn.init.xavier_normal_(self.attn_r.data, gain=1.414)
        self.leaky_relu1 = nn.LeakyReLU(alpha)
        self.leaky_relu2 = nn.LeakyReLU(alpha)
        self.softmax = EdgeSoftmax()
        self.residual = residual
        if residual:
            if in_dim != out_dim:
                self.res_fc = nn.Linear(in_dim, num_heads * out_dim, bias=False)
                nn.init.xavier_normal_(self.res_fc.weight.data, gain=1.414)
            else:
                self.res_fc = None

    def forward(self, inputs):
        # prepare
        h = self.feat_drop(inputs)  # NxD
        ft1 = self.fc1(h)  # NxHxD'
        h2 = self.leaky_relu1(ft1)
        ft2 = self.fc2(h2).reshape((h2.shape[0], self.num_heads, -1))  # NxHxD'
        head_ft = ft2.transpose(0, 1)  # HxNxD'
        a1 = torch.bmm(head_ft, self.attn_l).transpose(0, 1)  # NxHx1
        a2 = torch.bmm(head_ft, self.attn_r).transpose(0, 1)  # NxHx1
        self.g.ndata.update({'ft': ft2, 'a1': a1, 'a2': a2})
        # 1. compute edge attention
        self.g.apply_edges(self.edge_attention)
        # 2. compute softmax in two parts: exp(x - max(x)) and sum(exp(x - max(x)))
        self.edge_softmax()
        # 2. compute the aggregated node features scaled by the dropped,
        # unnormalized attention values.
        self.g.update_all(fn.src_mul_edge('ft', 'a_drop', 'ft'), fn.sum('ft', 'ft'))
        # 3. apply normalizer
        ret = self.g.ndata['ft'] #/ self.g.ndata['z']  # NxHxD'
        # 4. residual
        if self.residual:
            if self.res_fc is not None:
                resval = self.res_fc(h).reshape((h.shape[0], self.num_heads, -1))  # NxHxD'
            else:
                resval = torch.unsqueeze(h, 1)  # Nx1xD'
            ret = resval + ret
        return ret

    def edge_attention(self, edges):
        # an edge UDF to compute unnormalized attention values from src and dst
        a = self.leaky_relu1(edges.src['a1'] + edges.dst['a2'])
        return {'a' : a}

    def edge_softmax(self):
        scores = self.softmax.apply(self.g,self.g.edata['a'])
        # Save normalizer
        #self.g.ndata['z'] = normalizer
        # Dropout attention scores and save them
        self.g.edata['a_drop'] = self.attn_drop(scores)

class GAT2(nn.Module):
    def __init__(self, g, num_layers, in_dim,  num_hidden, num_classes, heads, activation, feat_drop, attn_drop, alpha,
                 residual):
        super(GAT2, self).__init__()
        self.g = g
        self.num_layers = num_layers
        self.layers = nn.ModuleList()
        self.activation = activation
        # input projection (no residual)
        self.layers.append(GraphAttention2(g,
                                          in_dim=in_dim,
                                          out_dim=num_hidden,
                                          num_heads=heads[0],
                                          feat_drop=feat_drop,
                                          attn_drop=attn_drop,
                                          alpha=alpha,
                                          residual=False,
                                          name='0'))
        # hidden layers
        for l in range(1, num_layers):
            # due to multi-head, the in_dim = num_hidden * num_heads
            lyr = GraphAttention2(g,
                                 in_dim=num_hidden*heads[l-1],
                                 out_dim=num_hidden,
                                 num_heads=heads[l],
                                 feat_drop=feat_drop,
                                 attn_drop=attn_drop,
                                 alpha=alpha,
                                 residual=residual,
                                 name=str(l))
            self.layers.append(lyr)
        # output projection
        self.layers.append(GraphAttention2(g,
                                          in_dim=num_hidden*heads[-2],
                                          out_dim=num_classes,
                                          num_heads=heads[-1],
                                          feat_drop=feat_drop,
                                          attn_drop=attn_drop,
                                          alpha=alpha,
                                          residual=residual,
                                          name='X'))

    def forward(self, inputs):
        h = inputs
        for l in range(self.num_layers-1):
            h = self.layers[l](h).flatten(1)
            h = self.activation(h)
        h = self.layers[self.num_layers-1](h).flatten(1)
        h = torch.tanh(h)
        # output projection
        logits = self.layers[-1](h).mean(1)
        return logits

    def set_g(self, g):
        self.g = g
        for l in range(self.num_layers):
            self.layers[l].g = g
