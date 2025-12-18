# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict

# LIRS State
algo_state = {
    'S': OrderedDict(),       # Stack S: Key -> Type ('LIR' or 'HIR')
    'Q': OrderedDict(),       # Queue Q: Key -> None (Resident HIR)
    'non_resident': set(),    # Set of keys in S but not in cache (Non-resident HIR)
    'LIR_count': 0,           # Number of LIR entries
    'max_time': 0
}

def _check_reset(current_time):
    if current_time < algo_state['max_time']:
        algo_state['S'].clear()
        algo_state['Q'].clear()
        algo_state['non_resident'].clear()
        algo_state['LIR_count'] = 0
        algo_state['max_time'] = 0
    algo_state['max_time'] = current_time

def _prune_stack():
    S = algo_state['S']
    nr = algo_state['non_resident']
    # Remove HIRs from bottom of S
    while S:
        k, status = next(iter(S.items()))
        if status == 'LIR':
            break
        # It's HIR.
        if k in nr:
            nr.remove(k)
        S.popitem(last=False) # Remove from bottom

def evict(cache_snapshot, obj):
    '''
    LIRS Eviction:
    Evict from Q (Resident HIR).
    If Q empty, evict from LIR (demote first).
    '''
    Q = algo_state['Q']
    S = algo_state['S']

    victim = None
    if Q:
        victim = next(iter(Q))
    else:
        # Q is empty, evict bottom LIR
        if S:
            victim, _ = next(iter(S.items()))

    return victim

def update_after_hit(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    S = algo_state['S']
    Q = algo_state['Q']

    # Check if in Q (Resident HIR)
    if key in Q:
        # Resident HIR
        if key in S:
            # Hot HIR -> Promote to LIR
            del Q[key]
            S.move_to_end(key)
            S[key] = 'LIR'
            algo_state['LIR_count'] += 1

            # Enforce LIR capacity
            target_lir = max(1, int(cache_snapshot.capacity * 0.99))
            if algo_state['LIR_count'] > target_lir:
                 _prune_stack()
                 if S:
                     k, status = next(iter(S.items()))
                     if status == 'LIR':
                         S[k] = 'HIR'
                         Q[k] = None
                         algo_state['LIR_count'] -= 1
                         _prune_stack()
        else:
            # Cold HIR -> Update recency, stays HIR
            S[key] = 'HIR'
            Q.move_to_end(key)
    else:
        # LIR hit
        if key in S:
            S.move_to_end(key)
            _prune_stack()
        else:
            # Should be in S if LIR. Edge case recovery.
            S[key] = 'LIR'
            S.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    S = algo_state['S']
    Q = algo_state['Q']
    nr = algo_state['non_resident']

    if key in S:
        # Was Non-Resident HIR -> Promote to LIR
        if key in nr: nr.remove(key)

        S.move_to_end(key)
        S[key] = 'LIR'
        algo_state['LIR_count'] += 1

        target_lir = max(1, int(cache_snapshot.capacity * 0.99))
        if algo_state['LIR_count'] > target_lir:
             _prune_stack()
             if S:
                 k, status = next(iter(S.items()))
                 if status == 'LIR':
                     S[k] = 'HIR'
                     Q[k] = None
                     algo_state['LIR_count'] -= 1
                     _prune_stack()
    else:
        # New item -> Resident HIR
        S[key] = 'HIR'
        Q[key] = None # Insert at end (MRU) of Q

def update_after_evict(cache_snapshot, obj, evicted_obj):
    v_key = evicted_obj.key
    S = algo_state['S']
    Q = algo_state['Q']
    nr = algo_state['non_resident']

    if v_key in Q:
        del Q[v_key]

    if v_key in S:
        if S[v_key] == 'LIR':
             algo_state['LIR_count'] -= 1
        S[v_key] = 'HIR'
        nr.add(v_key)
        _prune_stack()

# EVOLVE-BLOCK-END

# This part remains fixed (not evolved)
def run_caching(trace_path: str, copy_code_dst: str):
    """Run the caching algorithm on a trace"""
    import os
    with open(os.path.abspath(__file__), 'r', encoding="utf-8") as f:
        code_str = f.read()
    with open(os.path.join(copy_code_dst), 'w') as f:
        f.write(code_str)
    from cache_utils import Cache, CacheConfig, CacheObj, Trace
    trace = Trace(trace_path=trace_path)
    cache_capacity = max(int(trace.get_ndv() * 0.1), 1)
    cache = Cache(CacheConfig(cache_capacity))
    for entry in trace.entries:
        obj = CacheObj(key=str(entry.key))
        cache.get(obj)
    with open(copy_code_dst, 'w') as f:
        f.write("")
    hit_rate = round(cache.hit_count / cache.access_count, 6)
    return hit_rate