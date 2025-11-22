# EVOLVE-BLOCK-START
"""
New EPLB implementation: proportional replication + LPT placement.

- Returns the same outputs as the original API.
- Optimized for a balance between runtime and balancedness.
"""

import math
import heapq
from typing import Tuple
import torch
import numpy as np


def _allocate_replicas_proportional(weights_np: np.ndarray, num_replicas: int) -> np.ndarray:
    """
    Fast heap-based allocation of extra replicas.

    Strategy:
      - Start with 1 replica per logical expert.
      - Maintain a max-heap keyed by current per-replica load (weight / count).
      - For each extra replica, pop the expert with largest current per-replica load,
        increment its count, recompute its load, and push back.
    This is O(extra_slots * log(num_log)) and deterministic. Handles zero-total case
    by even distribution as before.
    """
    num_log = weights_np.size
    assert num_replicas >= num_log, "num_replicas must be >= num_logical_experts"

    extra_slots = num_replicas - num_log
    if extra_slots == 0:
        return np.ones(num_log, dtype=np.int64)

    total = float(weights_np.sum())
    # If total is zero (no requests), distribute extras evenly
    if total <= 0.0:
        base_extra = np.full(num_log, extra_slots // num_log, dtype=np.int64)
        rem = int(extra_slots - base_extra.sum())
        if rem > 0:
            # deterministic assignment of remainders
            base_extra[:rem] += 1
        return (1 + base_extra).astype(np.int64)

    # Initialize counts and heap of (-load_per_replica, idx)
    counts = np.ones(num_log, dtype=np.int64)
    # Use python heapq; negative because heapq is a min-heap
    heap = []
    for j in range(num_log):
        load = float(weights_np[j]) / float(counts[j]) if counts[j] > 0 else 0.0
        heap.append((-load, j))
    heapq.heapify(heap)

    # Allocate extra slots greedily to current max per-replica load
    for _ in range(int(extra_slots)):
        neg_load, j = heapq.heappop(heap)
        counts[j] += 1
        new_load = float(weights_np[j]) / float(counts[j]) if counts[j] > 0 else 0.0
        heapq.heappush(heap, (-new_load, j))

    return counts.astype(np.int64)


def _place_replicas_lpt(weights_np: np.ndarray,
                        logcnt_np: np.ndarray,
                        num_replicas: int,
                        num_gpus: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    Place replicas (given by logcnt) onto num_replicas physical slots,
    grouped by num_gpus with equal capacity per GPU.

    Strategy:
      - First, for each logical (in descending per-replica weight), attempt to spread its
        replicas across distinct GPUs (up to min(count, num_gpus)), picking the currently
        least-loaded GPUs with available slots. This reduces colocations of the same logical
        on one GPU and improves balancedness.
      - Any remaining replicas are placed using the usual LPT heap strategy.
      - After placement, perform a single cheap swap attempt between heaviest and lightest GPU
        if it strictly reduces the global max (bounded refinement).
    """
    num_log = weights_np.size
    phy_per_gpu = num_replicas // num_gpus
    assert phy_per_gpu * num_gpus == num_replicas, "num_replicas must be divisible by num_gpus"

    # per-replica weight estimate
    per_rep_weight = np.zeros(num_log, dtype=float)
    nz = weights_np > 0
    per_rep_weight[nz] = weights_np[nz] / logcnt_np[nz].astype(float)

    phy2log = np.empty(num_replicas, dtype=np.int64)
    phyrank = np.empty(num_replicas, dtype=np.int64)

    # GPU state
    loads = np.zeros(num_gpus, dtype=float)
    slots_left = np.full(num_gpus, phy_per_gpu, dtype=int)
    next_slot_index = np.zeros(num_gpus, dtype=int)

    # Helper to assign one replica to a specific GPU
    def _assign_to_gpu(g: int, logical: int, rank: int, w: float):
        slot_in_gpu = int(next_slot_index[g])
        phys_idx = int(g * phy_per_gpu + slot_in_gpu)
        phy2log[phys_idx] = int(logical)
        phyrank[phys_idx] = int(rank)
        next_slot_index[g] += 1
        slots_left[g] -= 1
        loads[g] += float(w)

    # Prepare list of logicals sorted by descending per-replica weight
    ordered_logs = np.argsort(-per_rep_weight)

    remaining_replicas = []  # tuples (w, logical, rank) to place by heap

    # First pass: try to spread replicas of each logical across distinct GPUs
    for l in ordered_logs:
        cnt = int(logcnt_np[l])
        if cnt == 0:
            continue
        # number to attempt to spread without repeating GPUs
        k = min(cnt, num_gpus)
        assigned = 0
        if k > 0:
            # choose GPUs sorted by current load that have available slots
            # argsort gives indices from smallest load to largest
            cand_gpus = np.argsort(loads)
            for g in cand_gpus:
                if slots_left[g] <= 0:
                    continue
                # assign one replica here
                _assign_to_gpu(g, l, assigned, per_rep_weight[l])
                assigned += 1
                if assigned >= k:
                    break
        # rest replicas go to remaining list for LPT placement
        for r in range(assigned, cnt):
            remaining_replicas.append((per_rep_weight[l], int(l), int(r)))

    # Sort remaining replicas by descending weight (LPT)
    if remaining_replicas:
        remaining_replicas.sort(key=lambda x: -x[0])

        # Build heap from GPUs that still have slots
        heap = [(float(loads[g]), int(g)) for g in range(num_gpus) if slots_left[g] > 0]
        heapq.heapify(heap)
        # Place remaining replicas using LPT heap
        for w, l, r in remaining_replicas:
            # pop until GPU with slot
            while True:
                if not heap:
                    raise RuntimeError("Ran out of GPU slots while placing replicas")
                load, g = heapq.heappop(heap)
                if slots_left[g] > 0:
                    break
            _assign_to_gpu(g, l, r, w)
            # push back if still capacity
            if slots_left[g] > 0:
                heapq.heappush(heap, (float(loads[g]), int(g)))

    # Cheap bounded refinement: try one swap between heaviest and lightest GPU to reduce global max
    if num_gpus > 1:
        max_g = int(np.argmax(loads))
        min_g = int(np.argmin(loads))
        if loads[max_g] - loads[min_g] > 1e-12:
            # find heaviest slot in max_g and lightest in min_g
            base_max = max_g * phy_per_gpu
            base_min = min_g * phy_per_gpu
            heaviest_slot = None
            heaviest_w = -1.0
            lightest_slot = None
            lightest_w = float('inf')
            for s in range(phy_per_gpu):
                idx_max = base_max + s
                l_max = int(phy2log[idx_max])
                wmax = per_rep_weight[l_max]
                if wmax > heaviest_w:
                    heaviest_w = wmax
                    heaviest_slot = idx_max
                idx_min = base_min + s
                l_min = int(phy2log[idx_min])
                wmin = per_rep_weight[l_min]
                if wmin < lightest_w:
                    lightest_w = wmin
                    lightest_slot = idx_min
            if heaviest_slot is not None and lightest_slot is not None:
                old_max = float(np.max(loads))
                new_loads = loads.copy()
                new_loads[max_g] = new_loads[max_g] - heaviest_w + lightest_w
                new_loads[min_g] = new_loads[min_g] - lightest_w + heaviest_w
                if float(np.max(new_loads)) + 1e-12 < old_max:
                    # perform swap
                    a_log = int(phy2log[heaviest_slot])
                    b_log = int(phy2log[lightest_slot])
                    a_rank = int(phyrank[heaviest_slot])
                    b_rank = int(phyrank[lightest_slot])
                    phy2log[heaviest_slot] = b_log
                    phy2log[lightest_slot] = a_log
                    phyrank[heaviest_slot] = b_rank
                    phyrank[lightest_slot] = a_rank
                    # update loads to reflect swap (not strictly necessary after one swap, but keep consistent)
                    loads = new_loads

    return phy2log, phyrank


def rebalance_experts(
    weight: torch.Tensor,
    num_replicas: int,
    num_groups: int,
    num_nodes: int,
    num_gpus: int,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Reimplemented EPLB using proportional replication + LPT placement.

    Inputs:
        weight: [num_layers, num_logical_experts], CPU or GPU tensor
        num_replicas: total number of physical experts (must be divisible by num_gpus)
        num_groups, num_nodes: accepted but not used by this simplified global algorithm
        num_gpus: number of GPUs

    Returns:
        phy2log: [num_layers, num_replicas] (int64)
        log2phy: [num_layers, num_logical_experts, maxlogcnt] (int64) with -1 filler
        logcnt: [num_layers, num_logical_experts] (int64)
    """
    # Move to CPU numpy for fast small-scale computations and to avoid GPU sync
    weight_cpu = weight.float().cpu()
    num_layers, num_logical = weight_cpu.shape
    device_out = torch.device("cpu")

    # Basic checks
    assert num_replicas >= num_logical, "num_replicas must be >= num_logical_experts"
    assert num_replicas % num_gpus == 0, "num_replicas must be divisible by num_gpus"

    phy2log_all = np.empty((num_layers, num_replicas), dtype=np.int64)
    phyrank_all = np.empty((num_layers, num_replicas), dtype=np.int64)
    logcnt_all = np.empty((num_layers, num_logical), dtype=np.int64)

    weight_np_all = weight_cpu.numpy()

    for layer in range(num_layers):
        weights_np = weight_np_all[layer].astype(float)
        # Allocate replica counts proportional to weights
        logcnt_np = _allocate_replicas_proportional(weights_np, num_replicas)
        # Place replicas onto GPUs using LPT
        phy2log_np, phyrank_np = _place_replicas_lpt(weights_np, logcnt_np, num_replicas, num_gpus)

        phy2log_all[layer] = phy2log_np
        phyrank_all[layer] = phyrank_np
        logcnt_all[layer] = logcnt_np

    # Convert to torch tensors
    phy2log = torch.from_numpy(phy2log_all).to(dtype=torch.int64, device=device_out)
    phyrank = torch.from_numpy(phyrank_all).to(dtype=torch.int64, device=device_out)
    logcnt = torch.from_numpy(logcnt_all).to(dtype=torch.int64, device=device_out)

    # Build log2phy: [layers, num_logical, maxlogcnt]
    maxlogcnt = int(logcnt.max())
    log2phy = torch.full((num_layers, num_logical, maxlogcnt),
                         -1,
                         dtype=torch.int64,
                         device=device_out)

    # Vectorized fill of log2phy using scatter to avoid Python loops.
    # Compose indices: linear position = phy2log * maxlogcnt + phyrank, shape [layers, num_replicas]
    # Then scatter physical indices across the flattened last dimension.
    if maxlogcnt > 0:
        composite_idx = (phy2log * maxlogcnt + phyrank).view(num_layers, -1)
        # positions to write: 0..num_replicas-1 repeated per layer
        phys_positions = torch.arange(num_replicas, dtype=torch.int64, device=device_out).expand(num_layers, -1)
        # scatter into view(num_layers, -1)
        log2phy.view(num_layers, -1).scatter_(-1, composite_idx, phys_positions)

    return phy2log, log2phy, logcnt


# EVOLVE-BLOCK-END


# This part remains fixed (not evolved)
def run_eplb(weight: torch.Tensor, num_replicas: int, num_groups: int,
             num_nodes: int, num_gpus: int):
    """Run the expert parallelism load balancer"""
    phy2log, log2phy, logcnt = rebalance_experts(
        weight, num_replicas, num_groups, num_nodes, num_gpus
    )
    return phy2log, log2phy, logcnt


__all__ = ["rebalance_experts", "run_eplb"]
# ============================================================================
# EVOLUTION METADATA
# ============================================================================
# Generation: 36
# Score: 0.678392 (best out of 100 generations)
# Improvement: +3.49% over baseline (0.655539 → 0.678392)
# Balancedness: 0.3568 (vs 0.3111 baseline = +14.7%)
# Speed Score: 1.0000 (maintained)
# Patch Type: diff
# Date: November 21, 2025
# 
# Key Innovation: Fast heap-based allocation of extra replicas
# - Max-heap keyed by per-replica load (weight/count)
# - O(extra_slots × log(num_log)) complexity
# - Deterministic and efficient
# ============================================================================
