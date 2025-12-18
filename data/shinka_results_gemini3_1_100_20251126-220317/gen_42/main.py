# EVOLVE-BLOCK-START
"""
Adaptive W-TinyLFU with Ghost Lists
- Dynamically adjusts Window vs Main sizes based on ghost hits.
- Uses W-TinyLFU admission policy (Frequency Duel).
- SLRU for Main Cache (Probation + Protected).
- Doorkeeper for scan resistance.
"""
from collections import OrderedDict

algo_state = {
    'window': OrderedDict(),    # key -> size
    'probation': OrderedDict(), # key -> size
    'protected': OrderedDict(), # key -> size
    'window_size': 0,
    'protected_size': 0,
    'freq': {},                 # key -> count
    'doorkeeper': set(),        # key
    'access_count': 0,
    'w_ratio': 0.01,            # 1% initial window
    'ghost_w': OrderedDict(),   # keys evicted from Window
    'ghost_m': OrderedDict(),   # keys evicted from Main
}

def evict(cache_snapshot, obj):
    '''
    Adaptive W-TinyLFU Eviction with Doorkeeper
    '''
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    freq = algo_state['freq']
    dk = algo_state['doorkeeper']
    
    capacity = cache_snapshot.capacity
    w_cap = int(capacity * algo_state['w_ratio'])
    if w_cap < 1: w_cap = 1

    def get_freq(k):
        if k in freq: return freq[k]
        if k in dk: return 1
        return 0

    candidate_w = next(iter(window)) if window else None
    
    candidate_m = None
    if probation:
        candidate_m = next(iter(probation))
    elif protected:
        candidate_m = next(iter(protected))

    victim = None

    # 1. Grow Window if needed (and Main has victim)
    if algo_state['window_size'] < w_cap and candidate_m:
        victim = candidate_m
    
    # 2. Window Full -> Duel
    elif candidate_w:
        if not candidate_m:
            victim = candidate_w
        else:
            fw = get_freq(candidate_w)
            fm = get_freq(candidate_m)
            # Tie-breaker: Evict Window (reject new)
            if fw > fm:
                victim = candidate_m
            else:
                victim = candidate_w
    else:
        victim = candidate_m

    return victim

def update_after_hit(cache_snapshot, obj):
    key = obj.key
    algo_state['access_count'] += 1

    f = algo_state['freq']
    dk = algo_state['doorkeeper']
    
    # Frequency & Aging
    if key in f:
        f[key] = min(f[key] + 1, 63)
    elif key in dk:
        dk.remove(key)
        f[key] = 2
    else:
        dk.add(key)

    if algo_state['access_count'] >= cache_snapshot.capacity * 10:
        algo_state['doorkeeper'].clear()
        to_rem = []
        for k, v in f.items():
            f[k] = v // 2
            if f[k] == 0: to_rem.append(k)
        for k in to_rem: del f[k]
        algo_state['access_count'] = 0

    # Cache Update
    if key in algo_state['window']:
        algo_state['window'].move_to_end(key)
    elif key in algo_state['protected']:
        algo_state['protected'].move_to_end(key)
    elif key in algo_state['probation']:
        # Promote
        s = algo_state['probation'].pop(key)
        algo_state['protected'][key] = s
        algo_state['protected_size'] += s
        
        # SLRU Limit (80% of Main)
        m_cap = max(1, cache_snapshot.capacity - algo_state['window_size'])
        p_limit = int(m_cap * 0.8)
        
        while algo_state['protected_size'] > p_limit and algo_state['protected']:
            k, v = algo_state['protected'].popitem(last=False)
            algo_state['protected_size'] -= v
            algo_state['probation'][k] = v
            algo_state['probation'].move_to_end(k)

def update_after_insert(cache_snapshot, obj):
    key = obj.key
    size = obj.size
    algo_state['access_count'] += 1

    # Adaptive Window - Ghost Hits
    gw = algo_state['ghost_w']
    gm = algo_state['ghost_m']
    
    if key in gw:
        algo_state['w_ratio'] = min(0.8, algo_state['w_ratio'] + 0.01)
        del gw[key]
    elif key in gm:
        algo_state['w_ratio'] = max(0.01, algo_state['w_ratio'] - 0.01)
        del gm[key]

    # Frequency
    f = algo_state['freq']
    dk = algo_state['doorkeeper']
    if key in f:
        f[key] = min(f[key] + 1, 63)
    elif key in dk:
        dk.remove(key)
        f[key] = 2
    else:
        dk.add(key)

    if algo_state['access_count'] >= cache_snapshot.capacity * 10:
        algo_state['doorkeeper'].clear()
        to_rem = []
        for k, v in f.items():
            f[k] = v // 2
            if f[k] == 0: to_rem.append(k)
        for k in to_rem: del f[k]
        algo_state['access_count'] = 0

    # Insert to Window
    algo_state['window'][key] = size
    algo_state['window_size'] += size

def update_after_evict(cache_snapshot, obj, evicted_obj):
    v_key = evicted_obj.key
    v_size = evicted_obj.size
    
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    gw = algo_state['ghost_w']
    gm = algo_state['ghost_m']

    # Identify and Remove
    if v_key in window:
        del window[v_key]
        algo_state['window_size'] -= v_size
        gw[v_key] = None
    elif v_key in probation:
        del probation[v_key]
        gm[v_key] = None
    elif v_key in protected:
        val = protected.pop(v_key)
        algo_state['protected_size'] -= val
        gm[v_key] = None
        
    # Trim Ghosts
    g_cap = cache_snapshot.capacity
    if len(gw) > g_cap: gw.popitem(last=False)
    if len(gm) > g_cap: gm.popitem(last=False)
    
    # Manage Window -> Probation migration
    w_cap = int(cache_snapshot.capacity * algo_state['w_ratio'])
    if w_cap < 1: w_cap = 1
    
    if algo_state['window_size'] >= w_cap and window:
        k, s = window.popitem(last=False)
        algo_state['window_size'] -= s
        probation[k] = s
        probation.move_to_end(k) # MRU
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