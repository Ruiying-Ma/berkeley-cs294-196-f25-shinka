# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# W-TinyLFU Globals
wtlfu_window = dict()      # Admission window (LRU)
wtlfu_probation = dict()   # Main cache probation (LRU)
wtlfu_protected = dict()   # Main cache protected (LRU)
wtlfu_freq = dict()        # Frequency counters
wtlfu_count = 0            # Total access counter for reset

def get_config(capacity):
    # Dynamic configuration: Window 1%, Main 99% (80% Protected, 20% Probation)
    w_cap = max(1, int(capacity * 0.01))
    main_cap = capacity - w_cap
    protected_cap = int(main_cap * 0.8)
    return w_cap, protected_cap

def init_globals(cache_snapshot):
    global wtlfu_window, wtlfu_probation, wtlfu_protected, wtlfu_freq
    global wtlfu_count

    # Reset for new trace
    if cache_snapshot.access_count <= 1:
        wtlfu_window.clear()
        wtlfu_probation.clear()
        wtlfu_protected.clear()
        wtlfu_freq.clear()
        wtlfu_count = 0

    # Sync if state is empty but cache is not (mid-trace recovery)
    if not wtlfu_window and not wtlfu_probation and not wtlfu_protected and cache_snapshot.cache:
        for k in cache_snapshot.cache:
            wtlfu_probation[k] = None

def evict(cache_snapshot, obj):
    '''
    W-TinyLFU Eviction:
    - If Window <= Capacity, evict from Main (Probation) to admit to Window.
    - If Window > Capacity, Duel:
      Compare Window LRU freq vs Probation LRU freq.
      Loser is evicted.
    '''
    global wtlfu_window, wtlfu_probation, wtlfu_protected, wtlfu_freq

    init_globals(cache_snapshot)
    w_cap, _ = get_config(cache_snapshot.capacity)

    victim_window = next(iter(wtlfu_window)) if wtlfu_window else None
    victim_probation = next(iter(wtlfu_probation)) if wtlfu_probation else None

    # If Window is not full, we prefer to evict from Main to allow Window growth
    # (Unless Main is empty, then we must evict from Window)
    if len(wtlfu_window) <= w_cap:
        if victim_probation:
            return victim_probation
        elif victim_window:
            return victim_window
        elif wtlfu_protected:
            return next(iter(wtlfu_protected))

    # Window is full. Duel.
    if not victim_probation:
        # If Probation is empty, check Protected (demote/evict) or Window
        if wtlfu_protected:
            return next(iter(wtlfu_protected))
        return victim_window

    # Frequency Duel
    freq_w = wtlfu_freq.get(victim_window, 0)
    freq_p = wtlfu_freq.get(victim_probation, 0)

    if freq_w > freq_p:
        # Window item wins, evict Probation item
        return victim_probation
    else:
        # Probation item wins (or tie), evict Window item
        return victim_window

    return next(iter(cache_snapshot.cache))

def update_after_hit(cache_snapshot, obj):
    '''
    Hit:
    - Update Freq.
    - Manage SLRU promotions (Probation -> Protected).
    '''
    global wtlfu_window, wtlfu_probation, wtlfu_protected, wtlfu_freq, wtlfu_count

    key = obj.key
    wtlfu_freq[key] = wtlfu_freq.get(key, 0) + 1
    wtlfu_count += 1

    # Periodic Reset
    if wtlfu_count > cache_snapshot.capacity * 10:
        for k in wtlfu_freq:
            wtlfu_freq[k] //= 2
        wtlfu_count = 0

    if key in wtlfu_probation:
        del wtlfu_probation[key]
        wtlfu_protected[key] = None
    elif key in wtlfu_protected:
        del wtlfu_protected[key]
        wtlfu_protected[key] = None
    elif key in wtlfu_window:
        del wtlfu_window[key]
        wtlfu_window[key] = None

    # Enforce Protected Capacity
    _, protected_cap = get_config(cache_snapshot.capacity)
    while len(wtlfu_protected) > protected_cap:
        demoted = next(iter(wtlfu_protected))
        del wtlfu_protected[demoted]
        wtlfu_probation[demoted] = None

def update_after_insert(cache_snapshot, obj):
    '''
    Insert:
    - Update Freq.
    - Add to Window.
    '''
    global wtlfu_window, wtlfu_freq, wtlfu_count

    init_globals(cache_snapshot)
    key = obj.key
    wtlfu_freq[key] = wtlfu_freq.get(key, 0) + 1
    wtlfu_count += 1

    if wtlfu_count > cache_snapshot.capacity * 10:
        for k in wtlfu_freq:
            wtlfu_freq[k] //= 2
        wtlfu_count = 0

    wtlfu_window[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Evict:
    - Clean up lists.
    - Handle Window -> Probation flow if Window Candidate won the duel.
    '''
    global wtlfu_window, wtlfu_probation, wtlfu_protected

    key = evicted_obj.key

    if key in wtlfu_window:
        del wtlfu_window[key]
    elif key in wtlfu_probation:
        del wtlfu_probation[key]
    elif key in wtlfu_protected:
        del wtlfu_protected[key]

    # If Window is still overflowing (meaning we evicted from Main),
    # move the Window LRU to Probation.
    w_cap, _ = get_config(cache_snapshot.capacity)
    while len(wtlfu_window) > w_cap:
        candidate = next(iter(wtlfu_window))
        del wtlfu_window[candidate]
        wtlfu_probation[candidate] = None
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