import torch
from torch import nn
import torch_geometric as pyg

from neural_lam import utils
from neural_lam.interaction_net import InteractionNet
from pnia.models.ar_model import ARModel,HyperParam, load_graph # Import ayant changé 

class BaseGraphModel(ARModel):
    """
    Base (abstract) class for graph-based models building on
    the encode-process-decode idea.
    """
    def __init__(self, hp:HyperParam):
        super().__init__(hp)

        # Load graph with static features
        # NOTE: (IMPORTANT!) mesh nodes MUST have the first N_mesh indices,
        self.hierarchical, graph_ldict = load_graph(hp)
        for name, attr_value in graph_ldict.items():
            # Make BufferLists module members and register tensors as buffers
            if isinstance(attr_value, torch.Tensor):
                self.register_buffer(name, attr_value, persistent=False)
            else:
                setattr(self, name, attr_value)

        # Specify dimensions of data
        self.N_grid, grid_static_dim = self.grid_static_features.shape # 63784 = 268x238        

        self.N_mesh, N_mesh_ignore = self.get_num_mesh()
        print(f"Loaded graph with {self.N_grid + self.N_mesh} nodes "+
                f"({self.N_grid} grid, {self.N_mesh} mesh)")

        # grid_dim from data + static + batch_static
        grid_dim = 2*self.grid_state_dim + grid_static_dim + self.grid_forcing_dim +\
            self.batch_static_feature_dim

        print(f"{grid_dim} comes from {2*self.grid_state_dim} + {grid_static_dim} + {self.grid_forcing_dim} + {self.batch_static_feature_dim}")
        self.g2m_edges, g2m_dim = self.g2m_features.shape
        self.m2g_edges, m2g_dim = self.m2g_features.shape

        # Define sub-models
        # Feature embedders for grid
        self.mlp_blueprint_end = [hp.graph.hidden_dim]*(hp.graph.hidden_layers + 1)
        self.grid_embedder = utils.make_mlp([grid_dim] +
                self.mlp_blueprint_end)
        self.g2m_embedder = utils.make_mlp([g2m_dim] +
                self.mlp_blueprint_end)
        self.m2g_embedder = utils.make_mlp([m2g_dim] +
                self.mlp_blueprint_end)

        # GNNs
        # encoder
        self.g2m_gnn = InteractionNet(self.g2m_edge_index,
                hp.graph.hidden_dim, hidden_layers=hp.graph.hidden_layers, update_edges=False)
        self.encoding_grid_mlp = utils.make_mlp([hp.graph.hidden_dim]
                + self.mlp_blueprint_end)

        # decoder
        self.m2g_gnn = InteractionNet(self.m2g_edge_index,
                hp.graph.hidden_dim, hidden_layers=hp.graph.hidden_layers, update_edges=False)

        # Output mapping (hidden_dim -> output_dim)
        self.output_map = utils.make_mlp([hp.graph.hidden_dim]*(hp.graph.hidden_layers + 1) +\
                [self.grid_state_dim], layer_norm=False) # No layer norm on this one

    def get_num_mesh(self):
        """
        Compute number of mesh nodes from loaded features,
        and number of mesh nodes that should be ignored in encoding/decoding
        """
        raise NotImplementedError("get_num_mesh not implemented")

    def embedd_mesh_nodes(self):
        """
        Embedd static mesh features
        Returns tensor of shape (N_mesh, d_h)
        """
        raise NotImplementedError("embedd_mesh_nodes not implemented")

    def process_step(self, mesh_rep):
        """
        Process step of embedd-process-decode framework
        Processes the representation on the mesh, possible in multiple steps

        mesh_rep: has shape (B, N_mesh, d_h)
        Returns mesh_rep: (B, N_mesh, d_h)
        """
        raise NotImplementedError("process_step not implemented")

    def predict_step(self, prev_state, prev_prev_state, batch_static_features, forcing):
        """
        Step state one step ahead using prediction model, X_{t-1}, X_t -> X_t+1
        prev_state: (B, N_grid, feature_dim), X_t
        prev_prev_state: (B, N_grid, feature_dim), X_{t-1}
        batch_static_features: (B, N_grid, batch_static_feature_dim)
        forcing: (B, N_grid, forcing_dim)
        """
        batch_size = prev_state.shape[0]

     
        #print(prev_state.dtype, self.grid_static_features.dtype,batch_static_features.dtype )
        # Create full grid node features of shape (B, N_grid, grid_dim)
        grid_features = torch.cat((prev_state, prev_prev_state, batch_static_features,
            forcing, self.expand_to_batch(self.grid_static_features, batch_size)),
            dim=-1)

        #print("Features",grid_features.dtype, grid_features.shape)
        # Embedd all features
        grid_emb = self.grid_embedder(grid_features) # (B, N_grid, d_h)
        g2m_emb = self.g2m_embedder(self.g2m_features) # (M_g2m, d_h)
        m2g_emb = self.m2g_embedder(self.m2g_features) # (M_m2g, d_h)
        mesh_emb = self.embedd_mesh_nodes()

        # Map from grid to mesh
        mesh_emb_expanded = self.expand_to_batch(mesh_emb, batch_size) # (B, N_mesh, d_h)
        g2m_emb_expanded = self.expand_to_batch(g2m_emb, batch_size)


        # This also splits representation into grid and mesh
        mesh_rep = self.g2m_gnn(grid_emb, mesh_emb_expanded,
                g2m_emb_expanded) # (B, N_mesh, d_h)
        # Also MLP with residual for grid representation
        grid_rep = grid_emb + self.encoding_grid_mlp(grid_emb) # (B, N_grid, d_h)

        # Run processor step
        mesh_rep = self.process_step(mesh_rep)

        # Map back from mesh to grid
        m2g_emb_expanded = self.expand_to_batch(m2g_emb, batch_size)
        grid_rep = self.m2g_gnn(mesh_rep, grid_rep, m2g_emb_expanded) # (B, N_grid, d_h)

        # Map to output dimension, only for grid
        net_output = self.output_map(grid_rep) # (B, N_grid, d_f)

        # Rescale with one-step difference statistics
        rescaled_net_output = net_output*self.step_diff_std + self.step_diff_mean

        # Residual connection for full state
        return prev_state + rescaled_net_output



class BaseHiGraphModel(BaseGraphModel):
    """
    Base class for hierarchical graph models.
    """
    def __init__(self, hp):
        super().__init__(hp)

        # Track number of nodes, edges on each level
        # Flatten lists for efficient embedding
        self.N_levels = len(self.mesh_static_features)

        # Number of mesh nodes at each level
        self.N_mesh_levels = [mesh_feat.shape[0] for mesh_feat in
                self.mesh_static_features] # Needs as python list for later
        N_mesh_levels_torch = torch.tensor(self.N_mesh_levels)

        # Print some useful info
        print("Loaded hierachical graph with structure:")
        for l, N_level in enumerate(self.N_mesh_levels):
            same_level_edges = self.m2m_features[l].shape[0]
            print(f"level {l} - {N_level} nodes, {same_level_edges} same-level edges")

            if l < (self.N_levels-1):
                up_edges = self.mesh_up_features[l].shape[0]
                down_edges = self.mesh_down_features[l].shape[0]
                print(f"  {l}<->{l+1} - {up_edges} up edges, {down_edges} down edges")

        # Embedders
        # Assume all levels have same static feature dimensionality
        mesh_dim = self.mesh_static_features[0].shape[1]
        mesh_same_dim = self.m2m_features[0].shape[1]
        mesh_up_dim = self.mesh_up_features[0].shape[1]
        mesh_down_dim = self.mesh_down_features[0].shape[1]

        # Separate mesh node embedders for each level
        self.mesh_embedders = nn.ModuleList([utils.make_mlp([mesh_dim]  +
                self.mlp_blueprint_end) for _ in range(self.N_levels)])
        self.mesh_same_embedders = nn.ModuleList([utils.make_mlp([mesh_same_dim] +
                self.mlp_blueprint_end) for _ in range(self.N_levels)])
        self.mesh_up_embedders = nn.ModuleList([utils.make_mlp([mesh_up_dim] +
                self.mlp_blueprint_end) for _ in range(self.N_levels-1)])
        self.mesh_down_embedders = nn.ModuleList([utils.make_mlp([mesh_down_dim] +
                self.mlp_blueprint_end) for _ in range(self.N_levels-1)])

        # Instantiate GNNs
        # Init GNNs
        self.mesh_init_gnns = nn.ModuleList([InteractionNet(
                edge_index, hp.graph.hidden_dim, hidden_layers=hp.graph.hidden_layers)
            for edge_index in self.mesh_up_edge_index])

        # Read out GNNs
        self.mesh_read_gnns = nn.ModuleList([InteractionNet(
                edge_index, hp.graph.hidden_dim, hidden_layers=hp.graph.hidden_layers,
                update_edges=False)
            for edge_index in self.mesh_down_edge_index])

    def get_num_mesh(self):
        """
        Compute number of mesh nodes from loaded features,
        and number of mesh nodes that should be ignored in encoding/decoding
        """
        N_mesh = sum(node_feat.shape[0] for node_feat in self.mesh_static_features)
        N_mesh_ignore = N_mesh - self.mesh_static_features[0].shape[0]
        return N_mesh, N_mesh_ignore

    def embedd_mesh_nodes(self):
        """
        Embedd static mesh features
        This embedds only bottom level, rest is done at beginning of processing step
        Returns tensor of shape (N_mesh[0], d_h)
        """
        return self.mesh_embedders[0](self.mesh_static_features[0])

    def process_step(self, mesh_rep):
        """
        Process step of embedd-process-decode framework
        Processes the representation on the mesh, possible in multiple steps

        mesh_rep: has shape (B, N_mesh, d_h)
        Returns mesh_rep: (B, N_mesh, d_h)
        """
        batch_size = mesh_rep.shape[0]

        # EMBEDD REMAINING MESH NODES (levels >= 1) -
        # Create list of mesh node representations for each level,
        # each of size (B, N_mesh[l], d_h)
        mesh_rep_levels = [mesh_rep] + [self.expand_to_batch(
            emb(node_static_features), batch_size) for
                emb, node_static_features in
                zip(list(self.mesh_embedders)[1:], list(self.mesh_static_features)[1:])]

        # - EMBEDD EDGES -
        # Embedd edges, expand with batch dimension
        mesh_same_rep = [self.expand_to_batch(emb(edge_feat), batch_size) for
                emb, edge_feat in zip(self.mesh_same_embedders, self.m2m_features)]
        mesh_up_rep = [self.expand_to_batch(emb(edge_feat), batch_size) for
                emb, edge_feat in zip(self.mesh_up_embedders, self.mesh_up_features)]
        mesh_down_rep = [self.expand_to_batch(emb(edge_feat), batch_size) for
                emb, edge_feat in zip(self.mesh_down_embedders, self.mesh_down_features)]

        # - MESH INIT. -
        # Let level_l go from 1 to L
        for level_l, gnn in enumerate(self.mesh_init_gnns, start=1):
            # Extract representations
            send_node_rep = mesh_rep_levels[level_l-1] # (B, N_mesh[l-1], d_h)
            rec_node_rep = mesh_rep_levels[level_l] # (B, N_mesh[l], d_h)
            edge_rep = mesh_up_rep[level_l-1]

            # Apply GNN
            new_node_rep, new_edge_rep = gnn(send_node_rep, rec_node_rep, edge_rep)

            # Update node and edge vectors in lists
            mesh_rep_levels[level_l] = new_node_rep # (B, N_mesh[l], d_h)
            mesh_up_rep[level_l-1] = new_edge_rep # (B, M_up[l-1], d_h)

        # - PROCESSOR -
        mesh_rep_levels, _, _, mesh_down_rep = self.hi_processor_step(mesh_rep_levels,
                mesh_same_rep, mesh_up_rep, mesh_down_rep)

        # - MESH READ OUT. -
        # Let level_l go from L-1 to 0
        for level_l, gnn in zip(
                range(self.N_levels-2, -1, -1),
                reversed(self.mesh_read_gnns)):
            # Extract representations
            send_node_rep = mesh_rep_levels[level_l+1] # (B, N_mesh[l+1], d_h)
            rec_node_rep = mesh_rep_levels[level_l] # (B, N_mesh[l], d_h)
            edge_rep = mesh_down_rep[level_l]

            # Apply GNN
            new_node_rep = gnn(send_node_rep, rec_node_rep, edge_rep)

            # Update node and edge vectors in lists
            mesh_rep_levels[level_l] = new_node_rep # (B, N_mesh[l], d_h)

        # Return only bottom level representation
        return mesh_rep_levels[0] # (B, N_mesh[0], d_h)

    def hi_processor_step(self, mesh_rep_levels, mesh_same_rep, mesh_up_rep,
            mesh_down_rep):
        """
        Internal processor step of hierarchical graph models.
        Between mesh init and read out.

        Each input is list with representations, each with shape

        mesh_rep_levels: (B, N_mesh[l], d_h)
        mesh_same_rep: (B, M_same[l], d_h)
        mesh_up_rep: (B, M_up[l -> l+1], d_h)
        mesh_down_rep: (B, M_down[l <- l+1], d_h)

        Returns same lists
        """
        raise NotImplementedError("hi_process_step not implemented")


class GraphLAM(BaseGraphModel):
    """
    Full graph-based LAM model that can be used with different (non-hierarchical )graphs.
    Mainly based on GraphCast, but the model from Keisler (2022) almost identical.
    Used for GC-LAM and L1-LAM in Oskarsson et al. (2023).
    """
    def __init__(self, hp):
        super().__init__(hp)

        assert not self.hierarchical, "GraphLAM does not use a hierarchical mesh graph"
        print("In graphLam : hierarchical is set to ",self.hierachical)

        # grid_dim from data + static + batch_static
        mesh_dim = self.mesh_static_features.shape[1]
        m2m_edges, m2m_dim = self.m2m_features.shape
        print(f"Edges in subgraphs: m2m={m2m_edges}, g2m={self.g2m_edges}, "
                f"m2g={self.m2g_edges}")

        # Define sub-models
        # Feature embedders for mesh
        self.mesh_embedder = utils.make_mlp([mesh_dim] +
                self.mlp_blueprint_end)
        self.m2m_embedder = utils.make_mlp([m2m_dim] +
                self.mlp_blueprint_end)

        # GNNs
        # processor
        processor_nets = [InteractionNet(self.m2m_edge_index,
                hp.graph.hidden_dim, hidden_layers=hp.graph.hidden_layers, aggr=hp.graph.mesh_aggr)
            for _ in range(hp.graph.processor_layers)]
        self.processor = pyg.nn.Sequential("mesh_rep, edge_rep", [
                (net, "mesh_rep, mesh_rep, edge_rep -> mesh_rep, edge_rep")
            for net in processor_nets])

    def get_num_mesh(self):
        """
        Compute number of mesh nodes from loaded features,
        and number of mesh nodes that should be ignored in encoding/decoding
        """
        return self.mesh_static_features.shape[0], 0

    def embedd_mesh_nodes(self):
        """
        Embedd static mesh features
        Returns tensor of shape (N_mesh, d_h)
        """
        return self.mesh_embedder(self.mesh_static_features) # (N_mesh, d_h)

    def process_step(self, mesh_rep):
        """
        Process step of embedd-process-decode framework
        Processes the representation on the mesh, possible in multiple steps

        mesh_rep: has shape (B, N_mesh, d_h)
        Returns mesh_rep: (B, N_mesh, d_h)
        """
        # Embedd m2m here first
        batch_size = mesh_rep.shape[0]
        m2m_emb = self.m2m_embedder(self.m2m_features) # (M_mesh, d_h)
        m2m_emb_expanded = self.expand_to_batch(m2m_emb, batch_size) # (B, M_mesh, d_h)

        mesh_rep, _ = self.processor(mesh_rep, m2m_emb_expanded) # (B, N_mesh, d_h)
        return mesh_rep


class HiLAMParallel(BaseHiGraphModel):
    """
    Version of HiLAM where all message passing in the hierarchical mesh (up, down,
    inter-level) is ran in paralell.

    This is a somewhat simpler alternative to the sequential message passing of Hi-LAM.
    """
    def __init__(self, hp):
        super().__init__(hp)

        # Processor GNNs
        # Create the complete total edge_index combining all edges for processing
        total_edge_index_list = list(self.m2m_edge_index) +\
                list(self.mesh_up_edge_index) + list(self.mesh_down_edge_index)
        total_edge_index = torch.cat(total_edge_index_list, dim=1)
        self.edge_split_sections = [ei.shape[1] for ei in total_edge_index_list]

        if hp.graph.processor_layers == 0:
            self.processor = (lambda x, edge_attr: (x, edge_attr))
        else:
            processor_nets = [InteractionNet(total_edge_index, hp.graph.hidden_dim,
                hidden_layers=hp.graph.hidden_layers,
                edge_chunk_sizes=self.edge_split_sections,
                aggr_chunk_sizes=self.N_mesh_levels)
                for _ in range(hp.graph.processor_layers)]
            self.processor = pyg.nn.Sequential("mesh_rep, edge_rep", [
                (net, "mesh_rep, mesh_rep, edge_rep -> mesh_rep, edge_rep")
                for net in processor_nets])

    def hi_processor_step(self, mesh_rep_levels, mesh_same_rep, mesh_up_rep,
            mesh_down_rep):
        """
        Internal processor step of hierarchical graph models.
        Between mesh init and read out.

        Each input is list with representations, each with shape

        mesh_rep_levels: (B, N_mesh[l], d_h)
        mesh_same_rep: (B, M_same[l], d_h)
        mesh_up_rep: (B, M_up[l -> l+1], d_h)
        mesh_down_rep: (B, M_down[l <- l+1], d_h)

        Returns same lists
        """

        # First join all node and edge representations to single tensors
        mesh_rep = torch.cat(mesh_rep_levels, dim=1) # (B, N_mesh, d_h)
        mesh_edge_rep = torch.cat(mesh_same_rep + mesh_up_rep + mesh_down_rep,
                axis=1) # (B, M_mesh, d_h)

        # Here, update mesh_*_rep and mesh_rep
        mesh_rep, mesh_edge_rep = self.processor(mesh_rep, mesh_edge_rep)

        # Split up again for read-out step
        mesh_rep_levels = list(torch.split(mesh_rep, self.N_mesh_levels, dim=1))
        mesh_edge_rep_sections = torch.split(mesh_edge_rep, self.edge_split_sections,
                dim=1)

        mesh_same_rep = mesh_edge_rep_sections[:self.N_levels]
        mesh_up_rep = mesh_edge_rep_sections[
                self.N_levels:self.N_levels+(self.N_levels-1)]
        mesh_down_rep = mesh_edge_rep_sections[
                self.N_levels+(self.N_levels-1):] # Last are down edges

        # Note: We return all, even though only down edges really are used later
        return mesh_rep_levels, mesh_same_rep, mesh_up_rep, mesh_down_rep



class HiLAM(BaseHiGraphModel):
    """
    Hierarchical graph model with message passing that goes sequentially down and up
    the hierarchy during processing.
    The Hi-LAM model from Oskarsson et al. (2023)
    """
    def __init__(self, hp):
        super().__init__(hp)

        # Make down GNNs, both for down edges and same level
        self.mesh_down_gnns = nn.ModuleList([self.make_down_gnns(hp) for _ in
            range(hp.graph.processor_layers)]) # Nested lists (proc_steps, N_levels-1)
        self.mesh_down_same_gnns = nn.ModuleList([self.make_same_gnns(hp) for _ in
            range(hp.graph.processor_layers)]) # Nested lists (proc_steps, N_levels)

        # Make up GNNs, both for up edges and same level
        self.mesh_up_gnns = nn.ModuleList([self.make_up_gnns(hp) for _ in
            range(hp.graph.processor_layers)]) # Nested lists (proc_steps, N_levels-1)
        self.mesh_up_same_gnns = nn.ModuleList([self.make_same_gnns(hp) for _ in
            range(hp.graph.processor_layers)]) # Nested lists (proc_steps, N_levels)

    def make_same_gnns(self, hp):
        """
        Make intra-level GNNs.
        """
        return nn.ModuleList([InteractionNet(
                edge_index, hp.graph.hidden_dim, hidden_layers=hp.graph.hidden_layers)
            for edge_index in self.m2m_edge_index])

    def make_up_gnns(self, hp):
        """
        Make GNNs for processing steps up through the hierarchy.
        """
        return nn.ModuleList([InteractionNet(
                edge_index, hp.graph.hidden_dim, hidden_layers=hp.graph.hidden_layers)
            for edge_index in self.mesh_up_edge_index])

    def make_down_gnns(self, hp):
        """
        Make GNNs for processing steps down through the hierarchy.
        """
        return nn.ModuleList([InteractionNet(
                edge_index, hp.graph.hidden_dim, hidden_layers=hp.graph.hidden_layers)
            for edge_index in self.mesh_down_edge_index])

    def mesh_down_step(self, mesh_rep_levels, mesh_same_rep, mesh_down_rep, down_gnns,
            same_gnns):
        """
        Run down-part of vertical processing, sequentially alternating between processing
        using down edges and same-level edges.
        """
        # Run same level processing on level L
        mesh_rep_levels[-1], mesh_same_rep[-1] = same_gnns[-1](mesh_rep_levels[-1],
                mesh_rep_levels[-1], mesh_same_rep[-1])

        # Let level_l go from L-1 to 0
        for level_l, down_gnn, same_gnn in zip(
                range(self.N_levels-2, -1, -1),
                reversed(down_gnns), reversed(same_gnns[:-1])):
            # Extract representations
            send_node_rep = mesh_rep_levels[level_l+1] # (B, N_mesh[l+1], d_h)
            rec_node_rep = mesh_rep_levels[level_l] # (B, N_mesh[l], d_h)
            down_edge_rep = mesh_down_rep[level_l]
            same_edge_rep = mesh_same_rep[level_l]

            # Apply down GNN
            new_node_rep, mesh_down_rep[level_l] = down_gnn(send_node_rep, rec_node_rep,
                    down_edge_rep)

            # Run same level processing on level l
            mesh_rep_levels[level_l], mesh_same_rep[level_l] = same_gnn(new_node_rep,
                    new_node_rep, same_edge_rep)
            # (B, N_mesh[l], d_h) and (B, M_same[l], d_h)

        return mesh_rep_levels, mesh_same_rep, mesh_down_rep

    def mesh_up_step(self, mesh_rep_levels, mesh_same_rep, mesh_up_rep, up_gnns,
            same_gnns):
        """
        Run up-part of vertical processing, sequentially alternating between processing
        using up edges and same-level edges.
        """

        # Run same level processing on level 0
        mesh_rep_levels[0], mesh_same_rep[0] = same_gnns[0](mesh_rep_levels[0],
                mesh_rep_levels[0], mesh_same_rep[0])

        # Let level_l go from 1 to L
        for level_l, (up_gnn, same_gnn) in enumerate(zip(up_gnns, same_gnns[1:]),
                start=1):
            # Extract representations
            send_node_rep = mesh_rep_levels[level_l-1] # (B, N_mesh[l-1], d_h)
            rec_node_rep = mesh_rep_levels[level_l] # (B, N_mesh[l], d_h)
            up_edge_rep = mesh_up_rep[level_l-1]
            same_edge_rep = mesh_same_rep[level_l]

            # Apply up GNN
            new_node_rep, mesh_up_rep[level_l-1] = up_gnn(send_node_rep, rec_node_rep,
                    up_edge_rep)
            # (B, N_mesh[l], d_h) and (B, M_up[l-1], d_h)

            # Run same level processing on level l
            mesh_rep_levels[level_l], mesh_same_rep[level_l] = same_gnn(new_node_rep,
                    new_node_rep, same_edge_rep)
            # (B, N_mesh[l], d_h) and (B, M_same[l], d_h)

        return mesh_rep_levels, mesh_same_rep, mesh_up_rep

    def hi_processor_step(self, mesh_rep_levels, mesh_same_rep, mesh_up_rep,
            mesh_down_rep):
        """
        Internal processor step of hierarchical graph models.
        Between mesh init and read out.

        Each input is list with representations, each with shape

        mesh_rep_levels: (B, N_mesh[l], d_h)
        mesh_same_rep: (B, M_same[l], d_h)
        mesh_up_rep: (B, M_up[l -> l+1], d_h)
        mesh_down_rep: (B, M_down[l <- l+1], d_h)

        Returns same lists
        """
        for down_gnns, down_same_gnns, up_gnns, up_same_gnns in zip(self.mesh_down_gnns,
                self.mesh_down_same_gnns, self.mesh_up_gnns, self.mesh_up_same_gnns):
            # Down
            mesh_rep_levels, mesh_same_rep, mesh_down_rep = self.mesh_down_step(
                    mesh_rep_levels, mesh_same_rep, mesh_down_rep, down_gnns,
                    down_same_gnns)

            # Up
            mesh_rep_levels, mesh_same_rep, mesh_up_rep = self.mesh_up_step(
                    mesh_rep_levels, mesh_same_rep, mesh_up_rep, up_gnns,
                    up_same_gnns)

        # Note: We return all, even though only down edges really are used later
        return mesh_rep_levels, mesh_same_rep, mesh_up_rep, mesh_down_rep