# EVOLVE-BLOCK-START
"""
W-TinyLFU with Segmented Bias, Adaptive Window, and Accelerated Promotion.
"""

from collections import OrderedDict

# Global state
algo_state = {
    'window': OrderedDict(),  # key -> size (FIFO/LRU)
    'window_size': 0,
    'probation': OrderedDict(), # key -> size (Main-Probation)
    'protected': OrderedDict(), # key -> size (Main-Protected)
    'protected_size': 0,
    'freq': {},               # key -> count
    'doorkeeper': set(),      # key
    'access_count': 0,        # Internal counter for aging
    'last_trace_access': -1   # To detect trace reset
}

def _check_trace_reset(snapshot_access_count):
    if snapshot_access_count < algo_state['last_trace_access']:
        algo_state['window'].clear()
        algo_state['window_size'] = 0
        algo_state['probation'].clear()
        algo_state['protected'].clear()
        algo_state['protected_size'] = 0
        algo_state['freq'].clear()
        algo_state['doorkeeper'].clear()
        algo_state['access_count'] = 0
    algo_state['last_trace_access'] = snapshot_access_count

def _update_freq(key, capacity):
    freq = algo_state['freq']
    dk = algo_state['doorkeeper']

    # Frequency Update
    if key in freq:
        freq[key] = min(freq[key] + 1, 15) # Cap at 15
    elif key in dk:
        dk.remove(key)
        freq[key] = 2 # Promotion from DK
    else:
        dk.add(key)

    # Aging (every 5*capacity accesses)
    if algo_state['access_count'] >= capacity * 5:
        # Halve frequencies
        to_rem = []
        for k in freq:
            freq[k] //= 2
            if freq[k] == 0: to_rem.append(k)
        for k in to_rem: del freq[k]
        algo_state['access_count'] = 0
    
    # Doorkeeper Reset (Size based: > 2*capacity)
    if len(dk) > capacity * 2:
        dk.clear()

def _get_freq(key):
    if key in algo_state['freq']: return algo_state['freq'][key]
    if key in algo_state['doorkeeper']: return 1
    return 0

def evict(cache_snapshot, obj):
    _check_trace_reset(cache_snapshot.access_count)
    
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.01))

    # Candidates
    cand_w_key = next(iter(window)) if window else None
    cand_m_key = None
    is_protected = False

    if probation:
        cand_m_key = next(iter(probation))
    elif protected:
        cand_m_key = next(iter(protected))
        is_protected = True

    # 1. Window Growth Phase (Adaptive)
    # If Window is not full, we normally evict Main.
    # But if Main victim is valuable (freq > 0), we spare it and evict Window.
    # This keeps Window small if Main is hot.
    if algo_state['window_size'] < w_cap:
        if cand_m_key:
            fm = _get_freq(cand_m_key)
            if fm > 0:
                if cand_w_key: return cand_w_key
                return cand_m_key # Should not happen if W empty but size>0?
            else:
                return cand_m_key
        else:
            if cand_w_key: return cand_w_key
            return None

    # 2. Steady State (Window Full) -> Duel
    if not cand_w_key: return cand_m_key
    if not cand_m_key: return cand_w_key

    fw = _get_freq(cand_w_key)
    fm = _get_freq(cand_m_key)

    # Segmented Bias
    # Protected items get extra incumbency protection
    bias = 3 if is_protected else 1

    if fw > fm + bias:
        return cand_m_key
    else:
        return cand_w_key

def update_after_hit(cache_snapshot, obj):
    _check_trace_reset(cache_snapshot.access_count)
    key = obj.key
    algo_state['access_count'] += 1

    _update_freq(key, cache_snapshot.capacity)
    
    # SLRU Management
    if key in algo_state['protected']:
        algo_state['protected'].move_to_end(key)
    elif key in algo_state['probation']:
        # Promote to Protected
        val = algo_state['probation'].pop(key)
        algo_state['protected'][key] = val
        algo_state['protected_size'] += val
        
        # Enforce Protected Limit (80% of Capacity)
        limit = int(cache_snapshot.capacity * 0.8)
        while algo_state['protected_size'] > limit and algo_state['protected']:
            k, v = algo_state['protected'].popitem(last=False)
            algo_state['protected_size'] -= v
            algo_state['probation'][k] = v
            algo_state['probation'].move_to_end(k)
    elif key in algo_state['window']:
        # Window Hit: Move to MRU of Window (standard LRU within Window)
        algo_state['window'].move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    _check_trace_reset(cache_snapshot.access_count)
    key = obj.key
    size = obj.size
    algo_state['access_count'] += 1

    _update_freq(key, cache_snapshot.capacity)

    # Insert into Window
    algo_state['window'][key] = size
    algo_state['window_size'] += size

    # Handle Overflow: Window -> Main
    w_cap = max(1, int(cache_snapshot.capacity * 0.01))
    
    while algo_state['window_size'] > w_cap and algo_state['window']:
        k, s = algo_state['window'].popitem(last=False)
        algo_state['window_size'] -= s
        
        # Sketch-Accelerated Promotion
        # If frequency is high, jump straight to Protected
        freq = _get_freq(k)
        if freq >= 5:
            algo_state['protected'][k] = s
            algo_state['protected_size'] += s
            
            # Enforce Protected Limit
            limit = int(cache_snapshot.capacity * 0.8)
            while algo_state['protected_size'] > limit and algo_state['protected']:
                pk, pv = algo_state['protected'].popitem(last=False)
                algo_state['protected_size'] -= pv
                algo_state['probation'][pk] = pv
                algo_state['probation'].move_to_end(pk)
        else:
            algo_state['probation'][k] = s
            algo_state['probation'].move_to_end(k)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    key = evicted_obj.key
    size = evicted_obj.size

    # Remove from appropriate structure
    if key in algo_state['window']:
        del algo_state['window'][key]
        algo_state['window_size'] -= size
    elif key in algo_state['probation']:
        del algo_state['probation'][key]
    elif key in algo_state['protected']:
        v = algo_state['protected'].pop(key)
        algo_state['protected_size'] -= v
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