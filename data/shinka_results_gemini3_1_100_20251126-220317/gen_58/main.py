# EVOLVE-BLOCK-START
"""
W-TinyLFU with Doorkeeper, Adaptive Aging, and Biased Eviction.
Maintains a small Window and an SLRU Main Cache.
Uses a Doorkeeper to filter one-hit wonders.
"""
from collections import deque, OrderedDict

# Global State
algo_state = {
    'window': OrderedDict(),    # key -> size (LRU is first)
    'window_size': 0,           # Current total size of window
    'probation': OrderedDict(), # key -> size (LRU is first)
    'protected': OrderedDict(), # key -> size (LRU is first)
    'protected_size': 0,
    'freq': {},                 # key -> count
    'doorkeeper': set(),        # Set of keys seen approximately once
    'aging_counter': 0,
    'max_time': 0               # For detecting trace reset
}

def _reset_state(current_time):
    if current_time < algo_state['max_time']:
        algo_state['window'].clear()
        algo_state['window_size'] = 0
        algo_state['probation'].clear()
        algo_state['protected'].clear()
        algo_state['protected_size'] = 0
        algo_state['freq'].clear()
        algo_state['doorkeeper'].clear()
        algo_state['aging_counter'] = 0
        algo_state['max_time'] = 0
    algo_state['max_time'] = current_time

def _update_freq(key, capacity):
    state = algo_state

    # Aging
    state['aging_counter'] += 1
    if state['aging_counter'] >= capacity * 5:
        state['aging_counter'] = 0
        # Halve frequencies
        rem = []
        for k, v in state['freq'].items():
            nv = v // 2
            if nv == 0: rem.append(k)
            else: state['freq'][k] = nv
        for k in rem: del state['freq'][k]

    # Doorkeeper Management
    if len(state['doorkeeper']) > capacity * 2:
        state['doorkeeper'].clear()

    # Update
    if key in state['freq']:
        state['freq'][key] = min(state['freq'][key] + 1, 15)
    elif key in state['doorkeeper']:
        state['doorkeeper'].remove(key)
        state['freq'][key] = 2
    else:
        state['doorkeeper'].add(key)

def _get_freq(key):
    if key in algo_state['freq']:
        return algo_state['freq'][key]
    if key in algo_state['doorkeeper']:
        return 1
    return 0

def evict(cache_snapshot, obj):
    _reset_state(cache_snapshot.access_count)
    state = algo_state
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.01))

    # Candidates
    cand_w_key = next(iter(state['window'])) if state['window'] else None

    cand_m_key = None
    if state['probation']:
        cand_m_key = next(iter(state['probation']))
    elif state['protected']:
        cand_m_key = next(iter(state['protected']))

    # Fail-safe
    if cand_w_key is None and cand_m_key is None: return None
    if cand_w_key is None: return cand_m_key
    if cand_m_key is None: return cand_w_key

    fw = _get_freq(cand_w_key)
    fm = _get_freq(cand_m_key)

    # 1. Window Growing Logic
    if state['window_size'] < w_cap:
        # If Main victim is valuable (fm > fw), sacrifice Window instead.
        if fm > fw:
            return cand_w_key
        return cand_m_key

    # 2. Window Full - Duel
    # Bias towards Main (Probation/Protected) to avoid churn.
    if fw > fm + 1:
        return cand_m_key
    else:
        return cand_w_key

def update_after_hit(cache_snapshot, obj):
    _reset_state(cache_snapshot.access_count)
    state = algo_state
    key = obj.key
    capacity = cache_snapshot.capacity

    _update_freq(key, capacity)

    # Position Update
    if key in state['window']:
        state['window'].move_to_end(key)
    elif key in state['protected']:
        state['protected'].move_to_end(key)
    elif key in state['probation']:
        # Promote Probation -> Protected
        val = state['probation'].pop(key)
        state['protected'][key] = val
        state['protected_size'] += val

        # Enforce Protected Limit (80% of Main)
        limit = int(capacity * 0.8)
        while state['protected_size'] > limit and state['protected']:
            k, v = state['protected'].popitem(last=False)
            state['protected_size'] -= v
            state['probation'][k] = v
            state['probation'].move_to_end(k)

def update_after_insert(cache_snapshot, obj):
    _reset_state(cache_snapshot.access_count)
    state = algo_state
    key = obj.key
    size = obj.size
    capacity = cache_snapshot.capacity

    _update_freq(key, capacity)

    # Insert to Window
    state['window'][key] = size
    state['window_size'] += size

    # Check Window Overflow -> Move to Probation
    w_cap = max(1, int(capacity * 0.01))
    while state['window_size'] > w_cap and state['window']:
        k, s = state['window'].popitem(last=False)
        state['window_size'] -= s

        state['probation'][k] = s
        state['probation'].move_to_end(k)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    state = algo_state
    key = evicted_obj.key
    size = evicted_obj.size

    if key in state['window']:
        del state['window'][key]
        state['window_size'] -= size
    elif key in state['probation']:
        del state['probation'][key]
    elif key in state['protected']:
        val = state['protected'].pop(key)
        state['protected_size'] -= val

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