# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict

# Advanced W-TinyLFU with Doorkeeper and FIFO Window (S3-FIFO elements)
algo_state = {
    'window': OrderedDict(),    # Window Cache (FIFO)
    'window_hits': set(),       # Track hits in window
    'probation': OrderedDict(), # Main Cache - Probation (SLRU)
    'protected': OrderedDict(), # Main Cache - Protected (SLRU)
    'freq': {},                 # Frequency Counter
    'doorkeeper': set(),        # Doorkeeper
    'freq_count': 0,            # Aging counter
    'max_time': 0
}

def _check_reset(current_time):
    if current_time < algo_state['max_time']:
        algo_state['window'].clear()
        algo_state['window_hits'].clear()
        algo_state['probation'].clear()
        algo_state['protected'].clear()
        algo_state['freq'].clear()
        algo_state['doorkeeper'].clear()
        algo_state['freq_count'] = 0
        algo_state['max_time'] = 0
    algo_state['max_time'] = current_time

def evict(cache_snapshot, obj):
    '''
    Eviction Logic:
    - Window acts as a FIFO filter.
    - If Window tail was hit -> Promote to Main.
    - If Window tail was NOT hit -> Duel vs Main Victim (Probation/Protected).
    - Uses Doorkeeper to filter one-hit wonders.
    '''
    window = algo_state['window']
    window_hits = algo_state['window_hits']
    probation = algo_state['probation']
    protected = algo_state['protected']
    freq = algo_state['freq']
    doorkeeper = algo_state['doorkeeper']

    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.10)) # 10% Window

    # Lazy Promotion from Window: Process hits at tail
    while len(window) > w_cap:
        cand_key = next(iter(window))
        if cand_key in window_hits:
            window.popitem(last=False)
            window_hits.remove(cand_key)
            probation[cand_key] = None # Promote to Probation MRU
        else:
            break

    # Candidates
    cand_w = next(iter(window)) if window else None

    cand_m = None
    if probation:
        cand_m = next(iter(probation))
    elif protected:
        cand_m = next(iter(protected))

    # 1. Prefer evicting from Main if Window is small (to allow Window to grow)
    # But only if Main has items.
    if len(window) < w_cap and cand_m:
        return cand_m
    elif not cand_w:
        return cand_m
    elif not cand_m:
        return cand_w

    # 2. Duel: Window (unhit tail) vs Main
    def get_freq(k):
        if k in freq: return freq[k]
        if k in doorkeeper: return 1
        return 0

    fw = get_freq(cand_w)
    fm = get_freq(cand_m)

    # Bias towards Main (Incumbent)
    if fw > fm:
        # Window wins (High Freq). Promote to Main. Evict Main victim.
        window.popitem(last=False)
        window_hits.discard(cand_w)
        probation[cand_w] = None
        return cand_m
    else:
        # Window loses.
        return cand_w

def update_after_hit(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    state = algo_state

    # Frequency Aging (Accelerated: 1 * Capacity)
    state['freq_count'] += 1
    if state['freq_count'] >= cache_snapshot.capacity:
        state['doorkeeper'].clear()
        state['freq'] = {k: v // 2 for k, v in state['freq'].items() if v > 1}
        state['freq_count'] = 0

    # Frequency Update
    f = state['freq']
    dk = state['doorkeeper']
    if key in f:
        f[key] = min(f[key] + 1, 15)
    elif key in dk:
        f[key] = 2
        dk.remove(key)
    else:
        dk.add(key)

    # Cache Maintenance
    if key in state['window']:
        state['window_hits'].add(key)
        # FIFO Window: DO NOT move to end
    elif key in state['protected']:
        state['protected'].move_to_end(key)
    elif key in state['probation']:
        # Promote to Protected
        del state['probation'][key]
        state['protected'][key] = None

        # Enforce Protected Limit (80% of Main)
        w_cap = max(1, int(cache_snapshot.capacity * 0.10))
        m_cap = max(1, cache_snapshot.capacity - w_cap)
        p_cap = int(m_cap * 0.8)

        while len(state['protected']) > p_cap:
            k, _ = state['protected'].popitem(last=False)
            state['probation'][k] = None
            state['probation'].move_to_end(k)

def update_after_insert(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    state = algo_state

    # Frequency Aging
    state['freq_count'] += 1
    if state['freq_count'] >= cache_snapshot.capacity:
        state['doorkeeper'].clear()
        state['freq'] = {k: v // 2 for k, v in state['freq'].items() if v > 1}
        state['freq_count'] = 0

    # Frequency Update
    f = state['freq']
    dk = state['doorkeeper']
    if key in f:
        f[key] = min(f[key] + 1, 15)
    elif key in dk:
        f[key] = 2
        dk.remove(key)
    else:
        dk.add(key)

    # Insert to Window (FIFO)
    state['window'][key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    v_key = evicted_obj.key
    state = algo_state

    if v_key in state['window']:
        del state['window'][v_key]
        state['window_hits'].discard(v_key)
    elif v_key in state['probation']:
        del state['probation'][v_key]
    elif v_key in state['protected']:
        del state['protected'][v_key]

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