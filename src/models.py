################################################################
# Generalisation of Geometric Vector Perceptron, Jing et al.
# for explicit multi-state biomolecule representation learning.
# Original repository: https://github.com/drorlab/gvp-pytorch
################################################################

from typing import Optional
import torch
from torch import nn
import torch.nn.functional as F
from torch.distributions import Categorical
import torch_geometric

from src.layers import *
from src.sampling import choose_nts
from src.constants import LETTER_TO_NUM

class AutoregressiveMultiGNNv2(torch.nn.Module):
    '''
    Autoregressive GVP-GNN for **multiple** structure-conditioned RNA design.
    
    Takes in RNA structure graphs of type `torch_geometric.data.Data` 
    or `torch_geometric.data.Batch` and returns a categorical distribution
    over 4 bases at each position in a `torch.Tensor` of shape [n_nodes, 4].
    
    The standard forward pass requires sequence information as input
    and should be used for training or evaluating likelihood.
    For sampling or design, use `self.sample`.

    Args:
        node_in_dim (tuple): node dimensions in input graph
        node_h_dim (tuple): node dimensions to use in GVP-GNN layers
        node_in_dim (tuple): edge dimensions in input graph
        edge_h_dim (tuple): edge dimensions to embed in GVP-GNN layers
        num_layers (int): number of GVP-GNN layers in encoder/decoder
        drop_rate (float): rate to use in all dropout layers
        out_dim (int): output dimension (4 bases)
        max_moment_order (int): Maximum order of tensor moments to compute
        attention_heads (int): Number of attention heads in the hybrid GVP-attention layer
        attention_dropout (float): Dropout rate for attention weights
    '''
    def __init__(
        self,
        node_in_dim = (64, 4), 
        node_h_dim = (128, 16), 
        edge_in_dim = (32, 1), 
        edge_h_dim = (32, 1),
        num_layers = 3, 
        drop_rate = 0.1,
        out_dim = 4,
        attention_heads = 4,  
        attention_dropout = 0.1,  
        max_moment_order = 2,  
    ):
        super().__init__()
        self.node_in_dim = node_in_dim
        self.node_h_dim = node_h_dim
        self.edge_in_dim = edge_in_dim
        self.edge_h_dim = edge_h_dim
        self.num_layers = num_layers
        self.out_dim = out_dim
        self.max_moment_order = max_moment_order
        activations = (F.silu, None)
        
        # Node input embedding
        self.W_v = torch.nn.Sequential(
            LayerNorm(self.node_in_dim),
            GVP(self.node_in_dim, self.node_h_dim,
                activations=(None, None), vector_gate=True)
        )

        # Edge input embedding
        self.W_e = torch.nn.Sequential(
            LayerNorm(self.edge_in_dim),
            GVP(self.edge_in_dim, self.edge_h_dim, 
                activations=(None, None), vector_gate=True)
        )
        
        # Encoder layers (supports multiple conformations)
        self.encoder_layers = nn.ModuleList(
                MultiAttentiveGVPLayer(self.node_h_dim, self.edge_h_dim, 
                                      activations=activations, vector_gate=True,
                                      drop_rate=drop_rate, norm_first=True,
                                      n_heads=attention_heads, 
                                      attention_dropout=attention_dropout)  
            for _ in range(num_layers))

        # MLP for tensor moment pooling (psi): calculate input dimension: d + d^2 + ... + d^p (where d is node_h_dim[0])
        d = self.node_h_dim[0]
        moment_dim = sum(d**order for order in range(1, self.max_moment_order + 1))
        self.psi = nn.Sequential(
            nn.Linear(moment_dim, 2 * d),
            nn.SiLU(),
            nn.Dropout(drop_rate),
            nn.Linear(2 * d, d)
        )

        # Decoder layers
        self.W_s = nn.Embedding(self.out_dim, self.out_dim)
        self.edge_h_dim = (self.edge_h_dim[0] + self.out_dim, self.edge_h_dim[1])
        self.decoder_layers = nn.ModuleList(
                AttentiveGVPLayer(self.node_h_dim, self.edge_h_dim,
                                 activations=activations, vector_gate=True, 
                                 drop_rate=drop_rate, norm_first=True,
                                 n_heads=attention_heads,
                                 attention_dropout=attention_dropout) 
            for _ in range(num_layers))
        
        # Output
        self.W_out = GVP(self.node_h_dim, (self.out_dim, 0), activations=(None, None))
    
    def forward(self, batch):

        h_V = (batch.node_s, batch.node_v)
        h_E = (batch.edge_s, batch.edge_v)
        edge_index = batch.edge_index
        seq = batch.seq

        h_V = self.W_v(h_V)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
        h_E = self.W_e(h_E)  # (n_edges, n_conf, d_se), (n_edges, n_conf, d_ve, 3)

        for layer in self.encoder_layers:
            h_V = layer(h_V, edge_index, h_E)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)

        # Pool multi-conformation features: 
        # nodes: (n_nodes, d_s), (n_nodes, d_v, 3)
        # edges: (n_edges, d_se), (n_edges, d_ve, 3)
        h_V, h_E = self.pool_multi_conf(h_V, h_E, batch.mask_confs, edge_index)

        encoder_embeddings = h_V
        
        h_S = self.W_s(seq)
        h_S = h_S[edge_index[0]]
        h_S[edge_index[0] >= edge_index[1]] = 0
        h_E = (torch.cat([h_E[0], h_S], dim=-1), h_E[1])
        
        for layer in self.decoder_layers:
            h_V = layer(h_V, edge_index, h_E, autoregressive_x = encoder_embeddings)
        
        logits = self.W_out(h_V)
        
        return logits


    @torch.no_grad()
    def convert_sequences_to_tensors(self, sequences, device):
        '''
        Convert a list of sequences to a list of tensors
        '''
        avoid_tensors = []
        for seq_str in sequences:
            seq_tensor = torch.tensor([LETTER_TO_NUM[residue] for residue in seq_str], device=device)
            avoid_tensors.append(seq_tensor)
        return avoid_tensors


    @torch.no_grad()
    def prevent_forbidden_sequences(self, lgts, idx, seq, avoid_tensors, beam_width, n_samples, num_nodes, device):
        '''
        Prevent forbidden sequences from being generated
        '''
        for avoid_seq in avoid_tensors:
            # only if about to complete an avoid_seq pattern
            if idx >= len(avoid_seq) - 1:
                sequence_reshaped = seq.view(beam_width*n_samples, num_nodes)
                start_idx = idx - (len(avoid_seq) - 1)
                if start_idx < 0:
                    seq_window = torch.zeros((beam_width*n_samples, len(avoid_seq)-1), dtype=torch.long, device=device)
                else:
                    seq_window = sequence_reshaped[:, start_idx:idx]
                avoid_window = avoid_seq[:-1]
                            
                if len(avoid_window) == seq_window.size(1):
                    matches = torch.all(seq_window == avoid_window.unsqueeze(0), dim=1)
                    if torch.any(matches):
                        mask = matches
                        lgts[mask, avoid_seq[-1]] = float('-inf')
        return lgts


    @torch.no_grad()
    def sample(
            self, 
            batch, 
            n_samples, 
            temperature: Optional[float] = 0.1, 
            logit_bias: Optional[torch.Tensor] = None,
            return_logits: Optional[bool] = False,
            beam_width: Optional[int] = 2,
            beam_branch: Optional[int] = 2,
            sampling_strategy: Optional[str] = "categorical",
            sampling_value: Optional[float] = 0.0,
            max_temperature: Optional[float] = 0.5,
            temperature_factor: Optional[float] = 0,
            avoid_sequences: Optional[list] = None
        ):
        '''
        Samples sequences autoregressively from the distribution
        learned by the model.

        Args:
            batch (torch_geometric.data.Data): mini-batch containing one
                RNA backbone to design sequences for
            n_samples (int): number of samples
            temperature (float): temperature to use in softmax over 
                the categorical distribution
            logit_bias (torch.Tensor): bias to add to logits during sampling
                to manually fix or control nucleotides in designed sequences,
                of shape [n_nodes, 4]
            return_logits (bool): whether to return logits or 
            sampling_strategy (str): one of "categorical", "greedy", "top_k", "top_p", "min_p", etc.
            sampling_value (float): value for sampling strategy
            beam_width (int): number of beams to maintain during search
            beam_branch (int): number of samples to get from sampling strategy
            avoid_sequences (list): list of sequences to avoid
        Returns:
            seq (torch.Tensor): int tensor of shape [n_samples, n_nodes]
                                based on the residue-to-int mapping of
                                the original training data
            logits (torch.Tensor): logits of shape [n_samples, n_nodes, 4]
                                   (only if return_logits is True)
        ''' 
        h_V = (batch.node_s, batch.node_v)
        h_E = (batch.edge_s, batch.edge_v)
        edge_index = batch.edge_index
    
        device = edge_index.device
        num_nodes = h_V[0].shape[0]
        
        h_V = self.W_v(h_V)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
        h_E = self.W_e(h_E)  # (n_edges, n_conf, d_se), (n_edges, n_conf, d_ve, 3)
        
        for layer in self.encoder_layers:
            h_V = layer(h_V, edge_index, h_E)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
        
        # Pool multi-conformation features
        # nodes: (n_nodes, d_s), (n_nodes, d_v, 3)
        # edges: (n_edges, d_se), (n_edges, d_ve, 3)
        h_V, h_E = self.pool_multi_conf(h_V, h_E, batch.mask_confs, edge_index)
        
        # Repeat features for sampling n_samples times
        # might have to change this
        h_V = (h_V[0].repeat(beam_width*n_samples, 1),
            h_V[1].repeat(beam_width*n_samples, 1, 1))
        h_E = (h_E[0].repeat(beam_width*n_samples, 1),
            h_E[1].repeat(beam_width*n_samples, 1, 1))

        # Expand edge index for autoregressive decoding
        edge_index = edge_index.expand(beam_width*n_samples, -1, -1)
        offset = num_nodes * torch.arange(beam_width*n_samples, device=device).view(beam_width*n_samples, 1, 1)

        edge_index = torch.cat(tuple(edge_index + offset), dim=-1)
        # This is akin to 'batching' (in PyG style) n_samples copies of the graph
        
        scores = torch.zeros(beam_width*n_samples, dtype=torch.float, device=device)  # cumulative log-probability
        seq = torch.zeros(beam_width*num_nodes*n_samples, dtype=torch.int, device=device)  # decoded tokens (to be filled)
        h_S = torch.zeros(beam_width*num_nodes*n_samples, self.out_dim, device=device)
        # Each decoder layer keeps its own cache (here cloned from the pooled encoder features)
        h_V_cache = [(h_V[0].clone(), h_V[1].clone()) for _ in self.decoder_layers]
        # Optionally, you can store logits for later inspection
        logits = torch.zeros(beam_width*num_nodes*n_samples, self.out_dim, device=device)
        
        # Convert avoid_sequences to tensor format if provided
        if avoid_sequences is not None:
            avoid_tensors = self.convert_sequences_to_tensors(avoid_sequences, device)

        # Decode one token at a time
        for i in range(num_nodes):

            # --- Prepare messages for decoding token at position i ---
            # In the original sample(), h_S is used via indexing with edge_index.
            # Here we prepare the subset h_S_ corresponding to incoming edges for node i.
            h_S_ = h_S[edge_index[0]]
            # Zero out contributions from nodes not yet decoded:
            h_S_[edge_index[0] >= edge_index[1]] = 0

            # Concatenate h_S_ with edge features
            h_E_ = (torch.cat([h_E[0], h_S_], dim=-1), h_E[1])
            # Select only the incoming edges for node i:
            edge_mask = edge_index[1] % num_nodes == i  # True for all edges where dst is node i
            edge_index_ = edge_index[:, edge_mask]  # subset all incoming edges to node i
            h_E_ = tuple_index(h_E_, edge_mask)

            # Create a mask that is True only for the current node i (across all copies in the beam)
            node_mask = torch.zeros(beam_width*n_samples*num_nodes, device=device, dtype=torch.bool)
            node_mask[i::num_nodes] = True  # True for all nodes i and its repeats
            # not entirely sure if the thing above is correct

            # --- Pass through decoder layers ---
            # We simulate the same decoder forward pass as in sample(), updating the cache.
            for j, layer in enumerate(self.decoder_layers):
                out = layer(h_V_cache[j], edge_index_, h_E_,
                        autoregressive_x=h_V_cache[0], node_mask=node_mask)
                out = tuple_index(out, node_mask)  # subset out to only node i and its repeats
                # Update the cache for the next layer if needed
                if j < len(self.decoder_layers)-1:
                    h_V_cache[j+1][0][i::num_nodes] = out[0]
                    h_V_cache[j+1][1][i::num_nodes] = out[1]
            # Final logits for node i:
            lgts = self.W_out(out)

            # Add logit bias if provided to fix or bias positions
            if logit_bias is not None:
                lgts += logit_bias[i]

            # Add negative infinity to logits for sequences to avoid
            if avoid_sequences is not None:
                lgts = self.prevent_forbidden_sequences(lgts, i, seq, avoid_tensors, beam_width, n_samples, num_nodes, device)
            
            # Sample from logits
            # Make temperature dependent of sequence length being decoded to increase
            # stochasticity in beams as we progress through the sequence
            temperature_init = temperature
            temperature = min(temperature_init + temperature * temperature_factor * i, max_temperature)
            top_tokens, log_probs = choose_nts(lgts, strategy=sampling_strategy, beam_branch=beam_branch,
                                    temperature=temperature, sampling_value=sampling_value)

            # log probs will return probabilities for each nucleotide type
            # top tokens will return beam_branch samples of tokens for each of the sequences in n_samples
            # For each candidate token, create a new beam candidate.
            new_beam_seq = seq.clone().repeat(beam_branch, 1)
            new_beam_h_S = h_S.clone().repeat(beam_branch, 1, 1)
            new_beam_logits = logits.clone().repeat(beam_branch, 1, 1)

            top_log_probs_beam = log_probs.gather(dim=1, index=top_tokens)
            top_log_probs_beam = top_log_probs_beam.transpose(0, 1)

            new_beam_scores = scores.repeat(beam_branch, 1) + top_log_probs_beam
            new_beam_seq[:,i::num_nodes] = top_tokens.transpose(0,1)
            new_beam_logits[:,i::num_nodes] = lgts  # store the logits for analysis
            new_beam_h_S[:,i::num_nodes] = self.W_s(new_beam_seq[:,i::num_nodes]) # weird [0] indexing
            
            sorted_scores, sorted_indices = torch.sort(new_beam_scores, dim=0, descending=True)
            new_beam_seq[:,i::num_nodes] = torch.gather(new_beam_seq[:,i::num_nodes], dim=0, index=sorted_indices)

            # reorganize h_S and logits
            expanded_indices = sorted_indices.unsqueeze(-1).expand(-1, -1, new_beam_h_S[:, i::num_nodes].size(-1))
            new_beam_h_S[:,i::num_nodes] =  torch.gather(new_beam_h_S[:,i::num_nodes], dim=0, index=expanded_indices)
            new_beam_logits[:,i::num_nodes] =  torch.gather(new_beam_logits[:,i::num_nodes], dim=0, index=expanded_indices)

            # update metrics for both beams
            seq[i::num_nodes] = new_beam_seq[0,i::num_nodes]
            logits[i::num_nodes] = new_beam_logits[0,i::num_nodes]
            h_S[i::num_nodes] = new_beam_h_S[0,i::num_nodes]

            scores = sorted_scores[0]            
        
        # get ordered scores
        beamw_scores = scores.view(beam_width, -1)
        beamw_sorted_scores, beamw_sorted_indices = torch.sort(beamw_scores, dim=0, descending=True)

        # reshape tensors
        final_seq = seq.view(beam_width, n_samples, num_nodes)
        final_logits = logits.view(beam_width, n_samples, num_nodes, self.out_dim)

        # reorganize according to indices
        expanded_indices = beamw_sorted_indices.unsqueeze(-1).expand(-1, n_samples, num_nodes)
        final_seq = torch.gather(final_seq, dim=0, index=expanded_indices)
        expanded_indices = beamw_sorted_indices.unsqueeze(-1).unsqueeze(-1).expand(-1, n_samples, num_nodes, self.out_dim)
        final_logits = torch.gather(final_logits, dim=0, index=expanded_indices)

        # use sorted indices to get final tensors
        final_scores = beamw_sorted_scores[0]
        final_seq = final_seq[0]
        final_logits = final_logits[0]

        if return_logits:
            return final_seq.view(n_samples, num_nodes), final_logits.view(n_samples, num_nodes, self.out_dim)
        else:    
            return final_seq.view(n_samples, num_nodes)
        
        
    def pool_multi_conf(self, h_V, h_E, mask_confs, edge_index):
        """
        Pool multi-conformation features using tensor moment pooling for node scalar features.
        This implements a universal set aggregator as described by Maron et al.
        
        Args:
            h_V: Tuple of (scalar_features, vector_features) for nodes
            h_E: Tuple of (scalar_features, vector_features) for edges
            mask_confs: Boolean mask indicating valid conformations [n_nodes, n_conf]
            edge_index: Edge index tensor of shape [2, n_edges]
            
        Returns:
            Pooled node and edge features
        """
        if mask_confs.size(1) == 1:
            # Number of conformations is 1, no need to pool
            return (h_V[0][:, 0], h_V[1][:, 0]), (h_E[0][:, 0], h_E[1][:, 0])
        
        # True num_conf for masked pooling
        n_conf_true = mask_confs.sum(1, keepdim=True)  # (n_nodes, 1)
        
        # ==== TENSOR MOMENT POOLING FOR NODE SCALAR FEATURES ====
        # Apply mask to scalar features
        mask = mask_confs.unsqueeze(2)  # (n_nodes, n_conf, 1)
        h_V0_masked = h_V[0] * mask  # [n_nodes, n_conf, d]
        
        # Initialize list to store moments
        moments_list = []
        
        # Calculate moments up to max_moment_order
        for order in range(1, self.max_moment_order + 1):
            if order == 1:
                # First-order moment (mean)
                moment = h_V0_masked.sum(dim=1) / n_conf_true  # [n_nodes, d]
                moments_list.append(moment)
            else:
                # Higher-order moments
                moment = torch.zeros_like(h_V0_masked[:, 0])  # [n_nodes, d]
                
                for i in range(mask_confs.size(1)):
                    # Only consider valid conformations
                    conf_mask = mask_confs[:, i].bool()
                    if not conf_mask.any():
                        continue
                        
                    X_i = h_V0_masked[conf_mask, i]  # [n_valid_nodes, d]
                    
                    # For order=2, compute outer product X_i ⊗ X_i
                    if order == 2:
                        # Efficient implementation for order 2
                        tensor_i = X_i.unsqueeze(2) * X_i.unsqueeze(1)  # [n_valid_nodes, d, d]
                        tensor_i_flat = tensor_i.flatten(start_dim=1)  # [n_valid_nodes, d*d]
                    else:
                        # For higher orders, implementation would be more complex
                        # Here we approximate with element-wise powers for efficiency
                        tensor_i_flat = X_i ** order  # [n_valid_nodes, d]
                    
                    # Accumulate for this conformation
                    if order == 2:
                        moment[conf_mask] = moment[conf_mask] + tensor_i_flat
                    else:
                        moment[conf_mask] = moment[conf_mask] + tensor_i_flat
                
                # Normalize by number of valid conformations
                moment = moment / n_conf_true
                moments_list.append(moment)
        
        # Concatenate all moments
        aggregated = torch.cat(moments_list, dim=1)  # [n_nodes, (d + d^2 + ... + d^p)]
        
        # Apply MLP to transform aggregated moments
        h_V0_pooled = self.psi(aggregated)  # [n_nodes, d]
        
        # ==== REGULAR POOLING FOR NODE VECTOR FEATURES AND EDGE FEATURES ====
        # Mask vector features
        mask_vec = mask.unsqueeze(3)  # (n_nodes, n_conf, 1, 1)
        h_V1 = h_V[1] * mask_vec
        h_E0 = h_E[0] * mask[edge_index[0]]
        h_E1 = h_E[1] * mask_vec[edge_index[0]]
        
        # Average pooling for vector features and edge features
        h_V1_pooled = h_V1.sum(dim=1) / n_conf_true.unsqueeze(2)  # (n_nodes, d_v, 3)
        h_E = (h_E0.sum(dim=1) / n_conf_true[edge_index[0]],              # (n_edges, d_se)
               h_E1.sum(dim=1) / n_conf_true[edge_index[0]].unsqueeze(2)) # (n_edges, d_ve, 3)

        return (h_V0_pooled, h_V1_pooled), h_E




class AutoregressiveMultiGNNv1(torch.nn.Module):
    '''
    Autoregressive GVP-GNN for **multiple** structure-conditioned RNA design.
    
    Takes in RNA structure graphs of type `torch_geometric.data.Data` 
    or `torch_geometric.data.Batch` and returns a categorical distribution
    over 4 bases at each position in a `torch.Tensor` of shape [n_nodes, 4].
    
    The standard forward pass requires sequence information as input
    and should be used for training or evaluating likelihood.
    For sampling or design, use `self.sample`.

    Args:
        node_in_dim (tuple): node dimensions in input graph
        node_h_dim (tuple): node dimensions to use in GVP-GNN layers
        node_in_dim (tuple): edge dimensions in input graph
        edge_h_dim (tuple): edge dimensions to embed in GVP-GNN layers
        num_layers (int): number of GVP-GNN layers in encoder/decoder
        drop_rate (float): rate to use in all dropout layers
        out_dim (int): output dimension (4 bases)
    '''
    def __init__(
        self,
        node_in_dim = (64, 4), 
        node_h_dim = (128, 16), 
        edge_in_dim = (32, 1), 
        edge_h_dim = (32, 1),
        num_layers = 3, 
        drop_rate = 0.1,
        out_dim = 4
    ):
        super().__init__()
        self.node_in_dim = node_in_dim
        self.node_h_dim = node_h_dim
        self.edge_in_dim = edge_in_dim
        self.edge_h_dim = edge_h_dim
        self.num_layers = num_layers
        self.out_dim = out_dim
        activations = (F.silu, None)
        
        # Node input embedding
        self.W_v = torch.nn.Sequential(
            LayerNorm(self.node_in_dim),
            GVP(self.node_in_dim, self.node_h_dim,
                activations=(None, None), vector_gate=True)
        )

        # Edge input embedding
        self.W_e = torch.nn.Sequential(
            LayerNorm(self.edge_in_dim),
            GVP(self.edge_in_dim, self.edge_h_dim, 
                activations=(None, None), vector_gate=True)
        )
        
        # Encoder layers (supports multiple conformations)
        self.encoder_layers = nn.ModuleList(
                MultiGVPConvLayer(self.node_h_dim, self.edge_h_dim, 
                                  activations=activations, vector_gate=True,
                                  drop_rate=drop_rate, norm_first=True)
            for _ in range(num_layers))
        
        # Decoder layers
        self.W_s = nn.Embedding(self.out_dim, self.out_dim)
        self.edge_h_dim = (self.edge_h_dim[0] + self.out_dim, self.edge_h_dim[1])
        self.decoder_layers = nn.ModuleList(
                GVPConvLayer(self.node_h_dim, self.edge_h_dim,
                             activations=activations, vector_gate=True, 
                             drop_rate=drop_rate, autoregressive=True, norm_first=True) 
            for _ in range(num_layers))
        
        # Output
        self.W_out = GVP(self.node_h_dim, (self.out_dim, 0), activations=(None, None))
    
    def forward(self, batch):

        h_V = (batch.node_s, batch.node_v)
        h_E = (batch.edge_s, batch.edge_v)
        edge_index = batch.edge_index
        seq = batch.seq

        h_V = self.W_v(h_V)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
        h_E = self.W_e(h_E)  # (n_edges, n_conf, d_se), (n_edges, n_conf, d_ve, 3)

        for layer in self.encoder_layers:
            h_V = layer(h_V, edge_index, h_E)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)

        # Pool multi-conformation features: 
        # nodes: (n_nodes, d_s), (n_nodes, d_v, 3)
        # edges: (n_edges, d_se), (n_edges, d_ve, 3)
        h_V, h_E = self.pool_multi_conf(h_V, h_E, batch.mask_confs, edge_index)

        encoder_embeddings = h_V
        
        h_S = self.W_s(seq)
        h_S = h_S[edge_index[0]]
        h_S[edge_index[0] >= edge_index[1]] = 0
        h_E = (torch.cat([h_E[0], h_S], dim=-1), h_E[1])
        
        for layer in self.decoder_layers:
            h_V = layer(h_V, edge_index, h_E, autoregressive_x = encoder_embeddings)
        
        logits = self.W_out(h_V)
        
        return logits
    
    @torch.no_grad()
    def sample(self,
            batch,
            n_samples, 
            temperature: Optional[float] = 0.1, 
            logit_bias: Optional[torch.Tensor] = None,
            return_logits: Optional[bool] = False,
            beam_width: Optional[int] = 2,
            beam_branch: Optional[int] = 2,
            sampling_strategy: Optional[str] = "categorical",
            sampling_value: Optional[float] = 0.0,
            max_temperature: Optional[float] = 0.5,
            temperature_factor: Optional[float] = 0.0,
        ):
        '''
        Samples sequences autoregressively from the distribution
        learned by the model.

        Args:
            batch (torch_geometric.data.Data): mini-batch containing one
                RNA backbone to design sequences for
            n_samples (int): number of samples
            temperature (float): temperature to use in softmax over 
                the categorical distribution
            logit_bias (torch.Tensor): bias to add to logits during sampling
                to manually fix or control nucleotides in designed sequences,
                of shape [n_nodes, 4]
            return_logits (bool): whether to return logits or 
            sampling_strategy (str): one of "categorical", "greedy", "top_k", "top_p", "min_p", etc.
            top_k (int): if using top-k sampling, how many tokens to keep
            top_p (float): if using nucleus (top-p) sampling, what cumulative probability threshold
            min_p (float): if using min-p sampling, probability threshold w.r.t max prob
            beam_width (int): number of beams to maintain during search
            beam_branch (int): number of samples to get from sampling strategy
        Returns:
            seq (torch.Tensor): int tensor of shape [n_samples, n_nodes]
                                based on the residue-to-int mapping of
                                the original training data
            logits (torch.Tensor): logits of shape [n_samples, n_nodes, 4]
                                   (only if return_logits is True)
        ''' 
        h_V = (batch.node_s, batch.node_v)
        h_E = (batch.edge_s, batch.edge_v)
        edge_index = batch.edge_index
    
        device = edge_index.device
        num_nodes = h_V[0].shape[0]
        
        h_V = self.W_v(h_V)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
        h_E = self.W_e(h_E)  # (n_edges, n_conf, d_se), (n_edges, n_conf, d_ve, 3)
        
        for layer in self.encoder_layers:
            h_V = layer(h_V, edge_index, h_E)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
        
        # Pool multi-conformation features
        # nodes: (n_nodes, d_s), (n_nodes, d_v, 3)
        # edges: (n_edges, d_se), (n_edges, d_ve, 3)
        h_V, h_E = self.pool_multi_conf(h_V, h_E, batch.mask_confs, edge_index)
        
        # Repeat features for sampling n_samples times
        # might have to change this
        h_V = (h_V[0].repeat(beam_width*n_samples, 1),
            h_V[1].repeat(beam_width*n_samples, 1, 1))
        h_E = (h_E[0].repeat(beam_width*n_samples, 1),
            h_E[1].repeat(beam_width*n_samples, 1, 1))

        # Expand edge index for autoregressive decoding
        edge_index = edge_index.expand(beam_width*n_samples, -1, -1)
        offset = num_nodes * torch.arange(beam_width*n_samples, device=device).view(beam_width*n_samples, 1, 1)

        edge_index = torch.cat(tuple(edge_index + offset), dim=-1)
        # This is akin to 'batching' (in PyG style) n_samples copies of the graph
        
        scores = torch.zeros(beam_width*n_samples, dtype=torch.float, device=device)  # cumulative log-probability
        seq = torch.zeros(beam_width*num_nodes*n_samples, dtype=torch.int, device=device)  # decoded tokens (to be filled)
        h_S = torch.zeros(beam_width*num_nodes*n_samples, self.out_dim, device=device)
        # Each decoder layer keeps its own cache (here cloned from the pooled encoder features)
        h_V_cache = [(h_V[0].clone(), h_V[1].clone()) for _ in self.decoder_layers]
        # Optionally, you can store logits for later inspection
        logits = torch.zeros(beam_width*num_nodes*n_samples, self.out_dim, device=device)

        # Decode one token at a time
        for i in range(num_nodes):

            # --- Prepare messages for decoding token at position i ---
            # In the original sample(), h_S is used via indexing with edge_index.
            # Here we prepare the subset h_S_ corresponding to incoming edges for node i.
            h_S_ = h_S[edge_index[0]]
            # Zero out contributions from nodes not yet decoded:
            h_S_[edge_index[0] >= edge_index[1]] = 0

            # Concatenate h_S_ with edge features
            h_E_ = (torch.cat([h_E[0], h_S_], dim=-1), h_E[1])
            # Select only the incoming edges for node i:
            edge_mask = edge_index[1] % num_nodes == i  # True for all edges where dst is node i
            edge_index_ = edge_index[:, edge_mask]  # subset all incoming edges to node i
            h_E_ = tuple_index(h_E_, edge_mask)

            # Create a mask that is True only for the current node i (across all copies in the beam)
            node_mask = torch.zeros(beam_width*n_samples*num_nodes, device=device, dtype=torch.bool)
            node_mask[i::num_nodes] = True  # True for all nodes i and its repeats
            # not entirely sure if the thing above is correct

            # --- Pass through decoder layers ---
            # We simulate the same decoder forward pass as in sample(), updating the cache.
            for j, layer in enumerate(self.decoder_layers):
                out = layer(h_V_cache[j], edge_index_, h_E_,
                        autoregressive_x=h_V_cache[0], node_mask=node_mask)
                out = tuple_index(out, node_mask)  # subset out to only node i and its repeats
                # Update the cache for the next layer if needed
                if j < len(self.decoder_layers)-1:
                    h_V_cache[j+1][0][i::num_nodes] = out[0]
                    h_V_cache[j+1][1][i::num_nodes] = out[1]
            # Final logits for node i:
            lgts = self.W_out(out)

            # Add logit bias if provided to fix or bias positions
            if logit_bias is not None:
                lgts += logit_bias[i]

            # Sample from logits
            # Make temperature dependent of sequence length being decoded to increase
            # stochasticity in beams as we progress through the sequence
            temperature_init = temperature
            temperature = min(temperature_init + temperature * temperature_factor * i, max_temperature)
            top_tokens, log_probs = choose_nts(lgts, strategy=sampling_strategy, beam_branch=beam_branch,
                                    temperature=temperature, sampling_value=sampling_value)

            # log probs will return probabilities for each nucleotide type
            # top tokens will return beam_branch samples of tokens for each of the sequences in n_samples
            # For each candidate token, create a new beam candidate.
            new_beam_seq = seq.clone().repeat(beam_branch, 1)
            new_beam_h_S = h_S.clone().repeat(beam_branch, 1, 1)
            new_beam_logits = logits.clone().repeat(beam_branch, 1, 1)

            top_log_probs_beam = log_probs.gather(dim=1, index=top_tokens)
            top_log_probs_beam = top_log_probs_beam.transpose(0, 1)

            new_beam_scores = scores.repeat(beam_branch, 1) + top_log_probs_beam
            new_beam_seq[:,i::num_nodes] = top_tokens.transpose(0,1)
            new_beam_logits[:,i::num_nodes] = lgts  # store the logits for analysis
            new_beam_h_S[:,i::num_nodes] = self.W_s(new_beam_seq[:,i::num_nodes]) # weird [0] indexing
            
            sorted_scores, sorted_indices = torch.sort(new_beam_scores, dim=0, descending=True)

            new_beam_seq[:,i::num_nodes] = torch.gather(new_beam_seq[:,i::num_nodes], dim=0, index=sorted_indices)

            # reorganize h_S and logits
            expanded_indices = sorted_indices.unsqueeze(-1).expand(-1, -1, new_beam_h_S[:, i::num_nodes].size(-1))
            new_beam_h_S[:,i::num_nodes] =  torch.gather(new_beam_h_S[:,i::num_nodes], dim=0, index=expanded_indices)
            new_beam_logits[:,i::num_nodes] =  torch.gather(new_beam_logits[:,i::num_nodes], dim=0, index=expanded_indices)

            # update metrics for both beams
            seq[i::num_nodes] = new_beam_seq[0,i::num_nodes]
            logits[i::num_nodes] = new_beam_logits[0,i::num_nodes]
            h_S[i::num_nodes] = new_beam_h_S[0,i::num_nodes]

            scores = sorted_scores[0]            
        
        # get ordered scores
        beamw_scores = scores.view(beam_width, -1)
        beamw_sorted_scores, beamw_sorted_indices = torch.sort(beamw_scores, dim=0, descending=True)

        # reshape tensors
        final_seq = seq.view(beam_width, n_samples, num_nodes)
        final_logits = logits.view(beam_width, n_samples, num_nodes, self.out_dim)

        # reorganize according to indices
        expanded_indices = beamw_sorted_indices.unsqueeze(-1).expand(-1, n_samples, num_nodes)
        final_seq = torch.gather(final_seq, dim=0, index=expanded_indices)
        expanded_indices = beamw_sorted_indices.unsqueeze(-1).unsqueeze(-1).expand(-1, n_samples, num_nodes, self.out_dim)
        final_logits = torch.gather(final_logits, dim=0, index=expanded_indices)

        # use sorted indices to get final tensors
        final_scores = beamw_sorted_scores[0]
        final_seq = final_seq[0]
        final_logits = final_logits[0]

        if return_logits:
            return final_seq.view(n_samples, num_nodes), final_logits.view(n_samples, num_nodes, self.out_dim)
        else:    
            return final_seq.view(n_samples, num_nodes)

    def pool_multi_conf(self, h_V, h_E, mask_confs, edge_index):

        if mask_confs.size(1) == 1:
            # Number of conformations is 1, no need to pool
            return (h_V[0][:, 0], h_V[1][:, 0]), (h_E[0][:, 0], h_E[1][:, 0])
        
        # True num_conf for masked mean pooling
        n_conf_true = mask_confs.sum(1, keepdim=True)  # (n_nodes, 1)
        
        # Mask scalar features
        mask = mask_confs.unsqueeze(2)  # (n_nodes, n_conf, 1)
        h_V0 = h_V[0] * mask
        h_E0 = h_E[0] * mask[edge_index[0]]

        # Mask vector features
        mask = mask.unsqueeze(3)  # (n_nodes, n_conf, 1, 1)
        h_V1 = h_V[1] * mask
        h_E1 = h_E[1] * mask[edge_index[0]]
        
        # Average pooling multi-conformation features
        h_V = (h_V0.sum(dim=1) / n_conf_true,               # (n_nodes, d_s)
               h_V1.sum(dim=1) / n_conf_true.unsqueeze(2))  # (n_nodes, d_v, 3)
        h_E = (h_E0.sum(dim=1) / n_conf_true[edge_index[0]],               # (n_edges, d_se)
               h_E1.sum(dim=1) / n_conf_true[edge_index[0]].unsqueeze(2))  # (n_edges, d_ve, 3)

        return h_V, h_E


class NonAutoregressiveMultiGNNv1(torch.nn.Module):
    '''
    Non-Autoregressive GVP-GNN for **multiple** structure-conditioned RNA design.
    
    Takes in RNA structure graphs of type `torch_geometric.data.Data` 
    or `torch_geometric.data.Batch` and returns a categorical distribution
    over 4 bases at each position in a `torch.Tensor` of shape [n_nodes, 4].
    
    The standard forward pass requires sequence information as input
    and should be used for training or evaluating likelihood.
    For sampling or design, use `self.sample`.
    
    Args:
        node_in_dim (tuple): node dimensions in input graph
        node_h_dim (tuple): node dimensions to use in GVP-GNN layers
        node_in_dim (tuple): edge dimensions in input graph
        edge_h_dim (tuple): edge dimensions to embed in GVP-GNN layers
        num_layers (int): number of GVP-GNN layers in encoder/decoder
        drop_rate (float): rate to use in all dropout layers
        out_dim (int): output dimension (4 bases)
    '''
    def __init__(
        self,
        node_in_dim = (64, 4), 
        node_h_dim = (128, 16), 
        edge_in_dim = (32, 1), 
        edge_h_dim = (32, 1),
        num_layers = 3, 
        drop_rate = 0.1,
        out_dim = 4,
    ):
        super().__init__()
        self.node_in_dim = node_in_dim
        self.node_h_dim = node_h_dim
        self.edge_in_dim = edge_in_dim
        self.edge_h_dim = edge_h_dim
        self.num_layers = num_layers
        self.out_dim = out_dim
        activations = (F.silu, None)
        
        # Node input embedding
        self.W_v = torch.nn.Sequential(
            LayerNorm(self.node_in_dim),
            GVP(self.node_in_dim, self.node_h_dim,
                activations=(None, None), vector_gate=True)
        )

        # Edge input embedding
        self.W_e = torch.nn.Sequential(
            LayerNorm(self.edge_in_dim),
            GVP(self.edge_in_dim, self.edge_h_dim, 
                activations=(None, None), vector_gate=True)
        )
        
        # Encoder layers (supports multiple conformations)
        self.encoder_layers = nn.ModuleList(
                MultiGVPConvLayer(self.node_h_dim, self.edge_h_dim, 
                                  activations=activations, vector_gate=True,
                                  drop_rate=drop_rate, norm_first=True)
            for _ in range(num_layers))
        
        # Output
        self.W_out = torch.nn.Sequential(
            LayerNorm(self.node_h_dim),
            GVP(self.node_h_dim, self.node_h_dim,
                activations=(None, None), vector_gate=True),
            GVP(self.node_h_dim, (self.out_dim, 0), 
                activations=(None, None))   
        )
    
    def forward(self, batch):

        h_V = (batch.node_s, batch.node_v)
        h_E = (batch.edge_s, batch.edge_v)
        edge_index = batch.edge_index
        
        h_V = self.W_v(h_V)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
        h_E = self.W_e(h_E)  # (n_edges, n_conf, d_se), (n_edges, n_conf, d_ve, 3)

        for layer in self.encoder_layers:
            h_V = layer(h_V, edge_index, h_E)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)

        # Pool multi-conformation features: 
        # nodes: (n_nodes, d_s), (n_nodes, d_v, 3)
        # edges: (n_edges, d_se), (n_edges, d_ve, 3)
        # h_V, h_E = self.pool_multi_conf(h_V, h_E, batch.mask_confs, edge_index)
        h_V = (h_V[0].mean(dim=1), h_V[1].mean(dim=1))

        logits = self.W_out(h_V)  # (n_nodes, out_dim)
        
        return logits
    
    def sample(self, batch, n_samples, temperature=0.1, return_logits=False):
        
        with torch.no_grad():

            h_V = (batch.node_s, batch.node_v)
            h_E = (batch.edge_s, batch.edge_v)
            edge_index = batch.edge_index
        
            h_V = self.W_v(h_V)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
            h_E = self.W_e(h_E)  # (n_edges, n_conf, d_se), (n_edges, n_conf, d_ve, 3)
            
            for layer in self.encoder_layers:
                h_V = layer(h_V, edge_index, h_E)  # (n_nodes, n_conf, d_s), (n_nodes, n_conf, d_v, 3)
            
            # Pool multi-conformation features
            # h_V, h_E = self.pool_multi_conf(h_V, h_E, batch.mask_confs, edge_index)
            h_V = (h_V[0].mean(dim=1), h_V[1].mean(dim=1))
            
            logits = self.W_out(h_V)  # (n_nodes, out_dim)
            probs = F.softmax(logits / temperature, dim=-1)
            seq = torch.multinomial(probs, n_samples, replacement=True)  # (n_nodes, n_samples)

            if return_logits:
                return seq.permute(1, 0).contiguous(), logits.unsqueeze(0).repeat(n_samples, 1, 1)
            else:
                return seq.permute(1, 0).contiguous()        
    def pool_multi_conf(self, h_V, h_E, mask_confs, edge_index):

        if mask_confs.size(1) == 1:
            # Number of conformations is 1, no need to pool
            return (h_V[0][:, 0], h_V[1][:, 0]), (h_E[0][:, 0], h_E[1][:, 0])
        
        # True num_conf for masked mean pooling
        n_conf_true = mask_confs.sum(1, keepdim=True)  # (n_nodes, 1)
        
        # Mask scalar features
        mask = mask_confs.unsqueeze(2)  # (n_nodes, n_conf, 1)
        h_V0 = h_V[0] * mask
        h_E0 = h_E[0] * mask[edge_index[0]]

        # Mask vector features
        mask = mask.unsqueeze(3)  # (n_nodes, n_conf, 1, 1)
        h_V1 = h_V[1] * mask
        h_E1 = h_E[1] * mask[edge_index[0]]
        
        # Average pooling multi-conformation features
        h_V = (h_V0.sum(dim=1) / n_conf_true,               # (n_nodes, d_s)
               h_V1.sum(dim=1) / n_conf_true.unsqueeze(2))  # (n_nodes, d_v, 3)
        h_E = (h_E0.sum(dim=1) / n_conf_true[edge_index[0]],               # (n_edges, d_se)
               h_E1.sum(dim=1) / n_conf_true[edge_index[0]].unsqueeze(2))  # (n_edges, d_ve, 3)

        return h_V, h_E
