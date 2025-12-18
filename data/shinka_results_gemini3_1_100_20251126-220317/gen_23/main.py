# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# W-TinyLFU Globals
t_window = dict()       # FIFO Window (Nursery)
t_probation = dict()    # SLRU Probation (Main L1)
t_protected = dict()    # SLRU Protected (Main L2)
t_freq = dict()         # Frequency Counts (Approx)
t_doorkeeper = set()    # Bloom Filter / Set for 1st hit
t_accesses = 0          # Counter for aging

def estimate_freq(key):
    '''Helper to get frequency estimate'''
    global t_freq, t_doorkeeper
    count = 0
    if key in t_freq:
        count = t_freq[key]
    if key in t_doorkeeper:
        count += 1
    return count

def evict(cache_snapshot, obj):
    '''
    W-TinyLFU Eviction Logic:
    - Candidates: Head of Window (FIFO) vs LRU of Main (Probation).
    - If Window > 5% Cap: Duel candidates based on frequency.
    - Winner stays (Window moves to Main), Loser is evicted.
    '''
    global t_window, t_probation, t_protected, t_freq, t_doorkeeper

    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.05)) # 5% Window

    cand_w = None
    if t_window:
        cand_w = next(iter(t_window))

    cand_m = None
    # Main candidate: LRU of probation, else LRU of protected
    if t_probation:
        cand_m = next(iter(t_probation))
    elif t_protected:
        cand_m = next(iter(t_protected))

    victim = None

    # Logic: If Window full or Main empty, consider Window eviction
    if len(t_window) > w_cap and cand_w and cand_m:
        # Duel
        score_w = estimate_freq(cand_w)
        score_m = estimate_freq(cand_m)

        if score_w > score_m:
            # Window wins, Main victim dies.
            # Move Window candidate to Probation (Promote)
            del t_window[cand_w]
            t_probation[cand_w] = None
            victim = cand_m
        else:
            # Window loses
            victim = cand_w
    elif len(t_window) > w_cap and cand_w:
         # Main empty
         victim = cand_w
    else:
        # Window under budget, evict from Main
        if cand_m:
            victim = cand_m
        else:
            # Main empty? Fallback
            victim = cand_w if cand_w else next(iter(cache_snapshot.cache))

    # Cleanup internal lists for the victim immediately to keep sync
    if victim in t_window:
        del t_window[victim]
    elif victim in t_probation:
        del t_probation[victim]
    elif victim in t_protected:
        del t_protected[victim]

    return victim

def update_after_hit(cache_snapshot, obj):
    '''
    On Cache Hit:
    - Update frequency.
    - Manage SLRU promotions/demotions.
    '''
    global t_window, t_probation, t_protected, t_freq, t_doorkeeper

    key = obj.key

    # Update Frequency
    if key in t_doorkeeper:
        t_freq[key] = t_freq.get(key, 0) + 1
    else:
        t_doorkeeper.add(key)

    # Cache Policy
    if key in t_window:
        # Window is FIFO, do not move on hit
        pass
    elif key in t_probation:
        # Promote to Protected
        del t_probation[key]
        t_protected[key] = None
    elif key in t_protected:
        # Move to MRU
        del t_protected[key]
        t_protected[key] = None

    # Check Protected Capacity (80% of Main)
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.05))
    main_cap = max(1, capacity - w_cap)
    protected_cap = int(main_cap * 0.8)

    if len(t_protected) > protected_cap:
        # Demote LRU of protected to probation
        demote_key = next(iter(t_protected))
        del t_protected[demote_key]
        t_probation[demote_key] = None

def update_after_insert(cache_snapshot, obj):
    '''
    On Cache Insert (Miss):
    - Reset if new trace.
    - Insert to Window.
    - Update frequency and handle aging.
    '''
    global t_window, t_probation, t_protected, t_freq, t_doorkeeper, t_accesses

    # Reset
    if cache_snapshot.access_count <= 1:
        t_window.clear()
        t_probation.clear()
        t_protected.clear()
        t_freq.clear()
        t_doorkeeper.clear()
        t_accesses = 0

    key = obj.key
    # Insert to Window (FIFO tail)
    t_window[key] = None

    # Freq
    if key in t_doorkeeper:
        t_freq[key] = t_freq.get(key, 0) + 1
    else:
        t_doorkeeper.add(key)

    # Aging
    t_accesses += 1
    capacity = cache_snapshot.capacity
    if t_accesses >= 10 * capacity:
        t_accesses = 0
        t_doorkeeper.clear()
        # Halve frequencies
        for k in list(t_freq.keys()):
            t_freq[k] //= 2
            if t_freq[k] == 0:
                del t_freq[k]

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After Eviction:
    - Internal structures already updated in evict().
    '''
    pass

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