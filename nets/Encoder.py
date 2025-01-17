import torch.nn as nn
import torch.nn.functional as F
# from nets import GraphMultiHeadAttention
from nets import GraphMultiHeadAttentionV2


class GraphEncoderlayer(nn.Module):
    def __init__(self, num_head, model_size, ff_size, edge_dim_size):
        super(GraphEncoderlayer, self).__init__()

        self.attention = GraphMultiHeadAttentionV2(num_head, query_size=model_size, edge_size=edge_dim_size)
        self.BN1 = nn.BatchNorm1d(model_size)

        self.FFN_layer1 = nn.Linear(model_size, ff_size)
        self.FFN_layer2 = nn.Linear(ff_size, model_size)
        self.BN2 = nn.BatchNorm1d(model_size)

    def forward(self, h, e=None, mask=None):
        h_attn = self.attention(h, edges=e, mask=mask)
        h_attn = self.BN1((h + h_attn).permute(0, 2, 1)).permute(0, 2, 1)

        h_out = F.relu(self.FFN_layer1(h_attn))
        h_out = self.FFN_layer2(h_out)
        h_out = self.BN2((h_attn + h_out).permute(0, 2, 1)).permute(0, 2, 1)

        if mask is not None:
            h_out[mask] = 0
        return h_out


class GraphEncoder(nn.Module):
    def __init__(self, encoder_layer, num_head, model_size, ff_size, edge_dim_size):
        super(GraphEncoder, self).__init__()
        for l in range(encoder_layer):
            self.add_module(str(l), GraphEncoderlayer(num_head, model_size, ff_size, edge_dim_size))

    def forward(self, h, e=None, mask=None):
        h_out = h
        for child in self.children():
            h_out = child(h_out, e, mask=mask)
            #h_out = h_out + h_in
        return h_out
