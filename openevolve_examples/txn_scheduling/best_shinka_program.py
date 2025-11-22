# EVOLVE-BLOCK-START
"""Transaction scheduling algorithm for optimizing makespan across multiple workloads
Rewritten to use an adaptive beam-search + local improvement hybrid approach.
"""

import time
import random
import sys
import os
from functools import lru_cache
from collections import defaultdict, OrderedDict
import math
import threading
import concurrent.futures

# Find the repository root by looking for openevolve_examples directory
def find_repo_root(start_path):
    """Find the repository root by looking for openevolve_examples directory."""
    current = os.path.abspath(start_path)
    while current != os.path.dirname(current):  # Stop at filesystem root
        if os.path.exists(os.path.join(current, 'openevolve_examples', 'txn_scheduling')):
            return current
        current = os.path.dirname(current)
    raise RuntimeError("Could not find openevolve_examples directory")

repo_root = find_repo_root(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(repo_root, 'openevolve_examples', 'txn_scheduling'))

from txn_simulator import Workload
from workloads import WORKLOAD_1, WORKLOAD_2, WORKLOAD_3

def _estimate_workload_complexity(workload):
    # Use average txn length as a proxy for complexity (higher -> more complex)
    try:
        lengths = [w[0][3] for w in workload.txns]
    except Exception:
        # fallback: uniform
        lengths = [1] * workload.num_txns
    avg_len = sum(lengths) / max(1, len(lengths))
    max_len = max(lengths) if lengths else 1
    return avg_len, max_len, lengths

def _select_seed_starts(lengths, num_seeds):
    # Stratified starts: longest, median, shortest, and some random
    n = len(lengths)
    idx_sorted = sorted(range(n), key=lambda i: (-lengths[i], i))
    seeds = []
    # top longest
    for i in range(min(num_seeds, max(1, n//10 + 1))):
        seeds.append(idx_sorted[i])
    # a median sample
    if n > 2 and len(seeds) < num_seeds:
        mid = n // 2
        seeds.append(idx_sorted[mid])
    # some short ones
    if n > 3 and len(seeds) < num_seeds:
        seeds.append(idx_sorted[-1])
    # random fill
    while len(seeds) < num_seeds:
        r = random.randrange(0, n)
        if r not in seeds:
            seeds.append(r)
    return seeds[:num_seeds]

def get_best_schedule(workload, num_seqs):
    """
    Hybrid beam search + adaptive sampling + local improvement scheduler.
    Params:
        workload: Workload object
        num_seqs: baseline beam width / sampling intensity
    Returns:
        (best_cost, best_sequence)
    """

    avg_len, max_len, lengths = _estimate_workload_complexity(workload)
    n = workload.num_txns

    # Adaptive parameterization
    # base beam width scales with provided num_seqs, but grows for complex workloads
    beam_width = int(max(2, min(60, num_seqs * (1 + (avg_len / max(1.0, 10.0))))))
    # candidate pool per beam node
    candidate_k = int(max(3, min(40, beam_width * 2)))
    # exploration vs exploitation mixing ratio
    if avg_len > 20:
        sample_rate = 0.95
    elif avg_len > 8:
        sample_rate = 0.8
    else:
        sample_rate = 0.55

    # limit the number of cost evaluations per extension step to keep runtime bounded
    max_cost_evals_per_step = int(max(200, beam_width * candidate_k * 2))
    # local improvement parameters
    local_search_iters = 200 + int(avg_len * 2)
    sa_temp_init = 1.0
    sa_temp_decay = 0.995

    # Precompute simple heuristic ranking (by length descending)
    txn_indices = list(range(n))
    txn_by_length = sorted(txn_indices, key=lambda i: (-lengths[i], i))

    # Memoization for cost evaluations
    prefix_cost_cache = {}
    cache_lock = threading.Lock()
    thread_local = threading.local()

    def _thread_cache():
        # per-thread small OrderedDict LRU cache
        if not hasattr(thread_local, 'hot'):
            thread_local.hot = OrderedDict()
        return thread_local.hot

    def cached_cost(seq):
        # seq is a list
        key = tuple(seq)
        tc = _thread_cache()
        # fast check in thread-local hot cache (no global lock)
        if key in tc:
            val = tc.pop(key)
            tc[key] = val  # move to end (most-recent)
            return val
        # check global cache under lock
        with cache_lock:
            if key in prefix_cost_cache:
                val = prefix_cost_cache[key]
                # populate thread-local hot cache
                tc[key] = val
                if len(tc) > 256:
                    tc.popitem(last=False)
                return val
        # compute outside lock
        val = workload.get_opt_seq_cost(list(seq))
        with cache_lock:
            prefix_cost_cache[key] = val
        # insert to thread-local cache
        tc[key] = val
        if len(tc) > 256:
            tc.popitem(last=False)
        return val

    # weighted sampling without replacement helper (probabilities from weights)
    def weighted_sample_without_replacement(pop, weights, k):
        if k <= 0 or not pop:
            return []
        if len(pop) <= k:
            return list(pop)
        # normalized probabilities
        s = float(sum(weights))
        if s == 0:
            # fallback uniform
            return random.sample(pop, k)
        probs = [w / s for w in weights]
        selected = []
        pool = list(pop)
        pool_probs = list(probs)
        for _ in range(min(k, len(pool))):
            r = random.random()
            cum = 0.0
            idx = 0
            for i, p in enumerate(pool_probs):
                cum += p
                if r <= cum:
                    idx = i
                    break
            selected.append(pool.pop(idx))
            pool_probs.pop(idx)
            # renormalize
            s2 = sum(pool_probs)
            if s2 > 0:
                pool_probs = [p / s2 for p in pool_probs]
            else:
                # remaining uniform
                pool_probs = [1.0 / max(1, len(pool))] * len(pool)
        return selected

    # Seed starting points (stratified)
    num_seed_starts = min(beam_width, max(3, num_seqs))
    starts = _select_seed_starts(lengths, num_seed_starts)

    # Deterministic cost-aware greedy full-sequence seed (bounded per-step candidates)
    best_known_full_cost = float('inf')
    best_known_full_seq = None
    try:
        greedy_start = starts[0] if starts else txn_by_length[0]
        greedy_seq = [greedy_start]
        rem = set(txn_indices) - {greedy_start}
        while rem:
            candidates = list(rem)
            # cap candidate set for speed when many remain
            if len(candidates) > 60:
                candidates.sort(key=lambda x: -lengths[x])
                candidates = candidates[:60]
            best_t = None
            best_c = float('inf')
            for t in candidates:
                c = cached_cost(greedy_seq + [t])
                if c < best_c:
                    best_c = c
                    best_t = t
            if best_t is None:
                best_t = candidates[0]
            greedy_seq.append(best_t)
            rem.remove(best_t)
        best_known_full_seq = greedy_seq
        best_known_full_cost = cached_cost(greedy_seq)
    except Exception:
        best_known_full_cost = float('inf')
        best_known_full_seq = None

    # Beam search: each entry is (seq_list, cost)
    beam = []
    # initialize beam with seeds
    for s in starts:
        seq = [s]
        cost = cached_cost(seq)
        beam.append((seq, cost))

    # also inject the single-prefix from greedy seed to bias beam starts (if not duplicate)
    if best_known_full_seq:
        gs0 = best_known_full_seq[0]
        if gs0 not in [b[0][0] for b in beam]:
            beam.append(([gs0], cached_cost([gs0])))

    # ensure we have at least one candidate
    if not beam:
        beam = [([0], cached_cost([0]))]

    # Beam search expansion
    # We'll expand step by step until full sequences are reached
    step = 1
    # Limit total cost evaluations done
    total_cost_evals = 0

    # Use a persistent executor to reduce repeated overhead
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 1)))
    try:
        while step < n:
            eval_futures = []
            # build a frequency map of txn appearances in current beam (used for novelty sampling)
            txn_freq = defaultdict(int)
            for seq, _ in beam:
                for t in seq:
                    txn_freq[t] += 1

            # adapt sample_rate a bit based on length variance (more variance -> more exploration)
            mean_len = sum(lengths) / max(1, len(lengths))
            var_len = sum((x - mean_len) ** 2 for x in lengths) / max(1, len(lengths))
            var_norm = var_len / max(1.0, (max_len ** 2))
            sample_rate_local = min(0.99, sample_rate + 0.5 * var_norm)

            # For each partial sequence in beam, form candidate extensions
            for seq, seq_cost in beam:
                remaining = [t for t in txn_indices if t not in seq]
                if not remaining:
                    continue

                # Heuristic candidates: take top K by length among remaining
                rem_by_length = sorted(remaining, key=lambda i: (-lengths[i], i))
                k1 = min(max(1, candidate_k // 2), len(rem_by_length))
                cand_set = set(rem_by_length[:k1])

                # Add some novelty-weighted candidates for exploration:
                # prefer transactions that are less frequent in the beam (novelty)
                rand_needed = min(candidate_k - len(cand_set), max(0, len(remaining) - len(cand_set)))
                if rand_needed > 0:
                    pool = [t for t in remaining if t not in cand_set]
                    if pool:
                        weights = []
                        for t in pool:
                            inv_freq = 1.0 / (1 + txn_freq.get(t, 0))
                            len_bias = (lengths[t] / max(1.0, max_len))
                            weights.append(inv_freq * 0.6 + len_bias * 0.4)
                        picks = weighted_sample_without_replacement(pool, weights, rand_needed)
                        for p in picks:
                            if p not in cand_set:
                                cand_set.add(p)
                    # if still short, fill deterministically
                    if len(cand_set) < candidate_k:
                        for r in remaining:
                            if r not in cand_set:
                                cand_set.add(r)
                                if len(cand_set) >= candidate_k:
                                    break

                # If remaining is very small, include all
                if len(remaining) <= candidate_k:
                    cand_set = set(remaining)

                # Cap number of candidates further to avoid explosion
                cand_list = list(cand_set)[:candidate_k]

                # Evaluate cost of seq + [t] for each candidate t
                for t in cand_list:
                    new_seq = seq + [t]
                    future = executor.submit(cached_cost, new_seq)
                    eval_futures.append((future, new_seq))
                    total_cost_evals += 1
                    if total_cost_evals > 500000:
                        break
                if total_cost_evals > 500000:
                    break
            # collect completed futures but honor a global cap per step
            collected = []
            for fut, new_seq in eval_futures:
                if len(collected) >= max_cost_evals_per_step:
                    break
                try:
                    new_cost = fut.result()
                    collected.append((new_seq, new_cost))
                except Exception:
                    pass

            if not collected:
                break

            # Deduplicate by sequence tuple keeping lowest cost
            seq_cost_map = {}
            for seq, c in collected:
                key = tuple(seq)
                if key not in seq_cost_map or c < seq_cost_map[key]:
                    seq_cost_map[key] = c

            # Convert to list of (tuple_seq, cost)
            candidate_list = [(k, v) for k, v in seq_cost_map.items()]

            # Diversity-promoting selection: iteratively pick entries with cost + penalty,
            # where penalty increases with overlap (set-based) to encourage varied beams.
            selected = []
            selected_keys = []
            # lambda scales with avg length to make diversity more important for longer txns
            diversity_lambda = max(0.01, 0.1 * (mean_len / max(1.0, mean_len)))
            # Pre-sort candidates by raw cost as basis
            candidate_list.sort(key=lambda kv: kv[1])
            while len(selected) < beam_width and candidate_list:
                best_idx = None
                best_score = None
                # evaluate adjusted score for first M candidates to keep cost low
                for idx in range(min(len(candidate_list), max(50, beam_width * 3))):
                    key, cost_cand = candidate_list[idx]
                    # compute average overlap with already selected (Jaccard-like on sets)
                    set_c = set(key)
                    overlap_pen = 0.0
                    for sk in selected_keys:
                        set_s = set(sk)
                        inter = len(set_c & set_s)
                        uni = len(set_c | set_s)
                        if uni > 0:
                            overlap_pen += (inter / uni)
                    if selected_keys:
                        overlap_pen = overlap_pen / len(selected_keys)
                    adjusted = cost_cand + diversity_lambda * overlap_pen * mean_len
                    if best_score is None or adjusted < best_score:
                        best_score = adjusted
                        best_idx = idx
                if best_idx is None:
                    break
                chosen_key, chosen_cost = candidate_list.pop(best_idx)
                selected.append((list(chosen_key), chosen_cost))
                selected_keys.append(chosen_key)

            # fall back: fill with best by raw cost if not enough selected
            if len(selected) < beam_width:
                remaining_best = sorted(candidate_list, key=lambda kv: kv[1])
                for k, v in remaining_best:
                    if len(selected) >= beam_width:
                        break
                    selected.append((list(k), v))

            beam = selected
            step += 1
    finally:
        executor.shutdown(wait=True)

    # After building full sequences in beam, pick best and apply local improvement
    full_sequences = []
    for seq, c in beam:
        if len(seq) == n:
            full_sequences.append((seq, c))
        else:
            # If some beam entries are still partial due to caps, create complete sequence by greedy fill
            remaining = [t for t in txn_indices if t not in seq]
            greedy_seq = seq.copy()
            # append remaining by descending length as fast heuristic
            greedy_seq.extend(sorted(remaining, key=lambda i: (-lengths[i], i)))
            c_full = cached_cost(greedy_seq)
            full_sequences.append((greedy_seq, c_full))

    # include deterministic greedy seed full sequence (if computed) as candidate
    if 'best_known_full_seq' in locals() and best_known_full_seq:
        full_sequences.append((best_known_full_seq, best_known_full_cost))

    # Take the best
    best_seq, best_cost = min(full_sequences, key=lambda kv: kv[1])

    # Local improvement: adjacent swaps + random two-opt with simulated annealing acceptance
    seq = best_seq.copy()
    cost = best_cost
    temp = sa_temp_init
    for it in range(local_search_iters):
        improved = False
        # try adjacent swaps first (cheap)
        for i in range(0, n - 1):
            new_seq = seq.copy()
            new_seq[i], new_seq[i+1] = new_seq[i+1], new_seq[i]
            new_cost = cached_cost(new_seq)
            if new_cost < cost:
                seq = new_seq
                cost = new_cost
                improved = True
                break
        if improved:
            temp *= sa_temp_decay
            continue

        # Random two-opt swap attempt
        i = random.randrange(0, n)
        j = random.randrange(0, n)
        if i == j:
            continue
        if i > j:
            i, j = j, i
        new_seq = seq[:i] + list(reversed(seq[i:j+1])) + seq[j+1:]
        new_cost = cached_cost(new_seq)
        delta = new_cost - cost
        # accept if better or with small probability (simulated annealing)
        if delta < 0 or random.random() < math.exp(-max(0.0, delta) / max(1e-6, temp)):
            seq = new_seq
            cost = new_cost
        temp *= sa_temp_decay

    # Final validity check
    assert len(seq) == n and len(set(seq)) == n

    return cost, seq

def get_random_costs():
    """
    Evaluate scheduling algorithm on three different workloads.
    Returns:
        Tuple of (total_makespan, list_of_schedules, execution_time)
    """
    start_time = time.time()
    # Workload 1: Complex mixed read/write transactions
    workload = Workload(WORKLOAD_1)
    makespan1, schedule1 = get_best_schedule(workload, 10)
    cost1 = workload.get_opt_seq_cost(schedule1)

    # Workload 2: Simple read-then-write pattern
    workload2 = Workload(WORKLOAD_2)
    makespan2, schedule2 = get_best_schedule(workload2, 10)
    cost2 = workload2.get_opt_seq_cost(schedule2)

    # Workload 3: Minimal read/write operations
    workload3 = Workload(WORKLOAD_3)
    makespan3, schedule3 = get_best_schedule(workload3, 10)
    cost3 = workload3.get_opt_seq_cost(schedule3)

    total_makespan = cost1 + cost2 + cost3
    schedules = [schedule1, schedule2, schedule3]
    execution_time = time.time() - start_time

    return total_makespan, schedules, execution_time

# EVOLVE-BLOCK-END


# This part remains fixed (not evolved)
def run_scheduling():
    """Run the transaction scheduling algorithm for all workloads"""
    total_makespan, schedules, execution_time = get_random_costs()
    return total_makespan, schedules, execution_time


if __name__ == "__main__":
    total_makespan, schedules, execution_time = run_scheduling()
    print(f"Total makespan: {total_makespan}, Execution time: {execution_time:.4f}s")
    print(f"Individual workload costs: {[workload.get_opt_seq_cost(schedule) for workload, schedule in zip([Workload(WORKLOAD_1), Workload(WORKLOAD_2), Workload(WORKLOAD_3)], schedules)]}")

