# EVOLVE-BLOCK-START
"""
W-TinyLFU with Segment-Aware Eviction and Adaptive Aging
- Window (1%) + SLRU (Probation + Protected)
- Segment-Aware Duel: 
  - Probation items are evicted if Window candidate is equal or better (fw >= fm).
  - Protected items are evicted only if Window candidate is significantly better (fw > fm + 1).
- Adaptive Aging: Halves frequencies every 5x capacity accesses.
- Doorkeeper: Resets when size exceeds 2x capacity.
"""
from collections import OrderedDict

# Global State
algo_state = {
    'window': OrderedDict(),    # key -> size (LRU first)
    'probation': OrderedDict(), # key -> size (LRU first)
    'protected': OrderedDict(), # key -> size (LRU first)
    'w_size': 0,
    'prob_size': 0,
    'prot_size': 0,
    'freq': {},                 # key -> count
    'doorkeeper': set(),        # key
    'access_count': 0,
    'last_trace_access': -1,
    'params': {
        'max_freq': 15,
        'window_ratio': 0.01,
        'protected_ratio': 0.8,
        'aging_interval_mult': 5,
        'doorkeeper_limit_mult': 2
    }
}

def _check_trace_reset(snapshot_access_count):
    if snapshot_access_count < algo_state['last_trace_access']:
        algo_state['window'].clear()
        algo_state['probation'].clear()
        algo_state['protected'].clear()
        algo_state['w_size'] = 0
        algo_state['prob_size'] = 0
        algo_state['prot_size'] = 0
        algo_state['freq'].clear()
        algo_state['doorkeeper'].clear()
        algo_state['access_count'] = 0
    algo_state['last_trace_access'] = snapshot_access_count

def _update_frequency(key, capacity):
    state = algo_state
    state['access_count'] += 1
    
    # Aging
    if state['access_count'] >= capacity * state['params']['aging_interval_mult']:
        state['access_count'] = 0
        rem = []
        for k, v in state['freq'].items():
            nv = v // 2
            if nv == 0: rem.append(k)
            else: state['freq'][k] = nv
        for k in rem: del state['freq'][k]
        
    # Doorkeeper Reset
    if len(state['doorkeeper']) > capacity * state['params']['doorkeeper_limit_mult']:
        state['doorkeeper'].clear()
        
    # Update Count
    if key in state['freq']:
        state['freq'][key] = min(state['freq'][key] + 1, state['params']['max_freq'])
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
    _check_trace_reset(cache_snapshot.access_count)
    state = algo_state
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * state['params']['window_ratio']))
    
    # Candidates
    cand_w = next(iter(state['window'])) if state['window'] else None
    
    cand_m = None
    cand_m_segment = None
    if state['probation']:
        cand_m = next(iter(state['probation']))
        cand_m_segment = 'probation'
    elif state['protected']:
        cand_m = next(iter(state['protected']))
        cand_m_segment = 'protected'
        
    # Fail-safe
    if cand_w is None and cand_m is None: return None
    if cand_w is None: return cand_m
    if cand_m is None: return cand_w
    
    fw = _get_freq(cand_w)
    fm = _get_freq(cand_m)
    
    # 1. Window Growth Phase
    # Ideally evict Main to let Window grow, unless Main item is valuable.
    if state['w_size'] < w_cap:
        if fm > fw:
            return cand_w
        return cand_m
        
    # 2. Steady State Duel
    # Logic depends on whether the Main candidate is from Probation or Protected
    if cand_m_segment == 'probation':
        # Probation: Vulnerable.
        # If Window candidate is equal or better, evict Probation item.
        # This increases turnover in Probation, allowing new items a chance.
        if fw >= fm:
            return cand_m
        else:
            return cand_w
    else:
        # Protected: Secure.
        # Only evict Protected item if Window candidate is significantly better.
        # Bias factor of 1 prevents replacing established items with marginally better new ones.
        if fw > fm + 1:
            return cand_m
        else:
            return cand_w

def update_after_hit(cache_snapshot, obj):
    _check_trace_reset(cache_snapshot.access_count)
    state = algo_state
    key = obj.key
    capacity = cache_snapshot.capacity
    
    _update_frequency(key, capacity)
    
    # Cache Position Updates
    if key in state['window']:
        state['window'].move_to_end(key)
    elif key in state['protected']:
        state['protected'].move_to_end(key)
    elif key in state['probation']:
        # Promote to Protected
        val = state['probation'].pop(key)
        state['prob_size'] -= val
        state['protected'][key] = val
        state['prot_size'] += val
        
        # Enforce Protected Limit
        limit = int(capacity * state['params']['protected_ratio'])
        while state['prot_size'] > limit and state['protected']:
            # Demote Protected LRU -> Probation MRU
            k, v = state['protected'].popitem(last=False)
            state['prot_size'] -= v
            state['probation'][k] = v
            state['prob_size'] += v
            state['probation'].move_to_end(k)

def update_after_insert(cache_snapshot, obj):
    _check_trace_reset(cache_snapshot.access_count)
    state = algo_state
    key = obj.key
    size = obj.size
    capacity = cache_snapshot.capacity
    
    _update_frequency(key, capacity)
    
    # Insert to Window
    state['window'][key] = size
    state['w_size'] += size
    
    # Handle Window Overflow -> Move to Probation MRU
    w_cap = max(1, int(capacity * state['params']['window_ratio']))
    while state['w_size'] > w_cap and state['window']:
        k, v = state['window'].popitem(last=False) # Pop LRU
        state['w_size'] -= v
        state['probation'][k] = v
        state['prob_size'] += v
        state['probation'].move_to_end(k) # MRU

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