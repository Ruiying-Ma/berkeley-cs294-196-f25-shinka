# EVOLVE-BLOCK-START
"""
W-TinyLFU with Segmented Duel and Adaptive Aging.
- Window (1%): Filters new items.
- Main Cache (SLRU): Probation (20%) + Protected (80%).
- Frequency Sketch: Count-Min style with Doorkeeper.
- Adaptive Aging: Halves frequencies every 5x capacity.
- Segmented Duel: Bias depends on whether the victim is in Probation or Protected.
"""
from collections import OrderedDict

# Global State
algo_state = {
    'window': OrderedDict(),    # key -> size
    'probation': OrderedDict(), # key -> size
    'protected': OrderedDict(), # key -> size
    'w_size': 0,
    'prob_size': 0,
    'prot_size': 0,
    'freq': {},                 # key -> count
    'doorkeeper': set(),        # Set of keys seen approximately once
    'aging_counter': 0,         # Counter for frequency aging
    'max_time': 0               # For detecting trace reset
}

def _reset(cache_snapshot):
    current_time = cache_snapshot.access_count
    if current_time < algo_state['max_time']:
        algo_state['window'].clear()
        algo_state['probation'].clear()
        algo_state['protected'].clear()
        algo_state['w_size'] = 0
        algo_state['prob_size'] = 0
        algo_state['prot_size'] = 0
        algo_state['freq'].clear()
        algo_state['doorkeeper'].clear()
        algo_state['aging_counter'] = 0
        algo_state['max_time'] = 0
    algo_state['max_time'] = current_time

def _update_freq(key, capacity):
    state = algo_state
    
    # 1. Aging Mechanism
    state['aging_counter'] += 1
    if state['aging_counter'] >= capacity * 5:
        state['aging_counter'] = 0
        # Halve frequencies to adapt to workload changes
        rem = []
        for k, v in state['freq'].items():
            nv = v // 2
            if nv == 0: rem.append(k)
            else: state['freq'][k] = nv
        for k in rem: del state['freq'][k]
        
    # 2. Doorkeeper Management (Scan Resistance)
    # Reset Doorkeeper based on size to keep it effective as a filter
    if len(state['doorkeeper']) > capacity * 2:
        state['doorkeeper'].clear()

    # 3. Frequency Update
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
    _reset(cache_snapshot)
    state = algo_state
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.01))
    
    # Identify Candidates (LRU is first)
    cand_w = next(iter(state['window'])) if state['window'] else None
    
    cand_m = None
    victim_is_protected = False
    
    if state['probation']:
        cand_m = next(iter(state['probation']))
    elif state['protected']:
        cand_m = next(iter(state['protected']))
        victim_is_protected = True
        
    # Fail-safe
    if cand_w is None and cand_m is None: return None
    if cand_w is None: return cand_m
    if cand_m is None: return cand_w
    
    # Admission Policy
    # 1. Grow Window if space allows
    if state['w_size'] < w_cap:
        return cand_m
        
    # 2. Duel: Window vs Main
    fw = _get_freq(cand_w)
    fm = _get_freq(cand_m)
    
    if victim_is_protected:
        # Bias towards keeping Protected items
        if fw > fm + 1:
            return cand_m
        else:
            return cand_w
    else:
        # Probation: Standard TinyLFU (No bias, or slight bias for incumbent)
        # Using strict inequality fw > fm favors incumbent on ties.
        if fw > fm:
            return cand_m
        else:
            return cand_w

def update_after_hit(cache_snapshot, obj):
    _reset(cache_snapshot)
    state = algo_state
    key = obj.key
    capacity = cache_snapshot.capacity
    
    _update_freq(key, capacity)
    
    # Cache Management
    if key in state['window']:
        state['window'].move_to_end(key)
    elif key in state['protected']:
        state['protected'].move_to_end(key)
    elif key in state['probation']:
        # Promote: Probation -> Protected
        val = state['probation'].pop(key)
        state['prob_size'] -= val
        state['protected'][key] = val
        state['prot_size'] += val
        
        # Enforce Protected Limit (80% of Main)
        # Main = Capacity - Window
        main_capacity = capacity - max(1, int(capacity * 0.01))
        limit = int(main_capacity * 0.8)
        
        while state['prot_size'] > limit and state['protected']:
            k, v = state['protected'].popitem(last=False)
            state['prot_size'] -= v
            state['probation'][k] = v
            state['prob_size'] += v
            state['probation'].move_to_end(k)

def update_after_insert(cache_snapshot, obj):
    _reset(cache_snapshot)
    state = algo_state
    key = obj.key
    size = obj.size
    capacity = cache_snapshot.capacity
    
    # Update Frequency
    _update_freq(key, capacity)
    
    # Insert into Window
    state['window'][key] = size
    state['w_size'] += size
    
    # Handle Window Overflow -> Move to Probation
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