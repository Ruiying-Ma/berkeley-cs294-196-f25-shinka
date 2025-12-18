# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import deque, OrderedDict

# W-TinyLFU style state with Doorkeeper and Refined Management
algo_state = {
    'window': OrderedDict(),    # key -> size (LRU is first)
    'probation': OrderedDict(), # key -> size (LRU is first)
    'protected': OrderedDict(), # key -> size (LRU is first)
    'w_size': 0,
    'prob_size': 0,
    'prot_size': 0,
    'freq': {},                 # key -> count
    'doorkeeper': set(),        # Set of keys seen once
    'aging_counter': 0,         # Counter for aging
    'max_access_count': 0       # For detecting trace reset
}

def _check_reset(cache_snapshot):
    current_time = cache_snapshot.access_count
    if current_time < algo_state['max_access_count']:
        algo_state['window'].clear()
        algo_state['probation'].clear()
        algo_state['protected'].clear()
        algo_state['w_size'] = 0
        algo_state['prob_size'] = 0
        algo_state['prot_size'] = 0
        algo_state['freq'].clear()
        algo_state['doorkeeper'].clear()
        algo_state['aging_counter'] = 0
        algo_state['max_access_count'] = 0
    algo_state['max_access_count'] = current_time

def _update_freq(key, capacity):
    state = algo_state
    state['aging_counter'] += 1

    # 5x Capacity Aging
    if state['aging_counter'] >= capacity * 5:
        state['aging_counter'] = 0
        state['doorkeeper'].clear()
        rem = []
        for k, v in state['freq'].items():
            nv = v // 2
            if nv == 0: rem.append(k)
            else: state['freq'][k] = nv
        for k in rem: del state['freq'][k]

    if key in state['freq']:
        state['freq'][key] = min(state['freq'][key] + 1, 60)
    elif key in state['doorkeeper']:
        state['doorkeeper'].remove(key)
        state['freq'][key] = 2
    else:
        state['doorkeeper'].add(key)

def evict(cache_snapshot, obj):
    _check_reset(cache_snapshot)
    state = algo_state
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.01))

    cand_w_key = next(iter(state['window'])) if state['window'] else None
    cand_m_key = None
    if state['probation']:
        cand_m_key = next(iter(state['probation']))
    elif state['protected']:
        cand_m_key = next(iter(state['protected']))

    if cand_w_key is None and cand_m_key is None:
        return None
    if cand_w_key is None:
        return cand_m_key
    if cand_m_key is None:
        return cand_w_key

    freq = state['freq']
    dk = state['doorkeeper']
    def get_freq(k):
        return freq.get(k, 1 if k in dk else 0)

    fw = get_freq(cand_w_key)
    fm = get_freq(cand_m_key)

    # 1. Window growing phase
    if state['w_size'] < w_cap:
        # Prevent evicting hot Main items for cold Window items
        if fm > fw:
            return cand_w_key
        return cand_m_key

    # 2. Window full phase -> Duel
    if fw > fm:
        return cand_m_key
    else:
        return cand_w_key

def update_after_hit(cache_snapshot, obj):
    _check_reset(cache_snapshot)
    state = algo_state
    key = obj.key
    capacity = cache_snapshot.capacity

    _update_freq(key, capacity)

    if key in state['window']:
        state['window'].move_to_end(key)
    elif key in state['protected']:
        state['protected'].move_to_end(key)
    elif key in state['probation']:
        # Strict Promotion: require established frequency
        if state['freq'].get(key, 0) > 2:
            val = state['probation'].pop(key)
            state['prob_size'] -= val
            state['protected'][key] = val
            state['prot_size'] += val

            limit = int(capacity * 0.8)
            while state['prot_size'] > limit and state['protected']:
                k, v = state['protected'].popitem(last=False)
                state['prot_size'] -= v
                state['probation'][k] = v
                state['prob_size'] += v
                state['probation'].move_to_end(k)
        else:
            state['probation'].move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    _check_reset(cache_snapshot)
    state = algo_state
    key = obj.key
    size = obj.size
    capacity = cache_snapshot.capacity

    _update_freq(key, capacity)

    state['window'][key] = size
    state['w_size'] += size

    w_cap = max(1, int(capacity * 0.01))
    while state['w_size'] > w_cap and state['window']:
        k, v = state['window'].popitem(last=False)
        state['w_size'] -= v
        state['probation'][k] = v
        state['prob_size'] += v
        state['probation'].move_to_end(k)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    state = algo_state
    key = evicted_obj.key
    size = evicted_obj.size

    if key in state['window']:
        del state['window'][key]
        state['w_size'] -= size
    elif key in state['probation']:
        del state['probation'][key]
        state['prob_size'] -= size
    elif key in state['protected']:
        del state['protected'][key]
        state['prot_size'] -= size

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