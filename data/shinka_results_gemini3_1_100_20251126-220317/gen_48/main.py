# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# W-TinyLFU Lite Globals
from collections import OrderedDict

q_window = OrderedDict()
q_probation = OrderedDict()
q_protected = OrderedDict()
freq = dict()
door = set()

# Config
c_window_pct = 0.01
c_protected_pct = 0.80

def evict(cache_snapshot, obj):
    '''
    W-TinyLFU Lite Eviction Logic:
    - Window (1%) vs Main (99%) structure.
    - If Window full, duel Window LRU vs Main Probation LRU using frequency.
    - Winner stays (promoted to/kept in Main), Loser is evicted.
    '''
    global q_window, q_probation, q_protected, freq, door

    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * c_window_pct))

    # Candidates
    cand_w = next(iter(q_window)) if q_window else None
    cand_p = next(iter(q_probation)) if q_probation else None

    evict_window = True

    # If Window is overflowing, we check if we can displace someone in Main
    if q_window and len(q_window) > w_cap:
        if cand_p:
            # Duel: Compare Frequencies
            fw = freq.get(cand_w, 0)
            fp = freq.get(cand_p, 0)

            if fw > fp:
                # Window wins -> Promote to Probation, Evict Probation victim
                evict_window = False

                # Internal Move: Window -> Probation
                q_window.move_to_end(cand_w) # Ensure right key is picked
                del q_window[cand_w]
                q_probation[cand_w] = None # Insert at tail
            else:
                # Window loses -> Evict Window victim
                evict_window = True
        else:
            # No probation victim -> Check protected?
            # Usually SLRU keeps protected safe. We evict Window.
            evict_window = True
    else:
        # Window not full. Evict from Main to maintain separation if needed.
        # If Main has items, evict from Main.
        if cand_p or q_protected:
            evict_window = False
        else:
            evict_window = True

    # Return the victim key
    if evict_window:
        return cand_w if cand_w else (cand_p if cand_p else next(iter(cache_snapshot.cache)))
    else:
        if cand_p: return cand_p
        if q_protected: return next(iter(q_protected))
        return cand_w

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Update Doorkeeper/Frequency.
    - SLRU Promotion (Probation -> Protected).
    '''
    global freq, door, q_window, q_probation, q_protected
    key = obj.key

    # Doorkeeper
    if key not in door:
        door.add(key)
    else:
        freq[key] = min(freq.get(key, 0) + 1, 60)

    # Cache segments
    if key in q_window:
        q_window.move_to_end(key)
    elif key in q_probation:
        # Promote
        del q_probation[key]
        q_protected[key] = None

        # Enforce Protected Limit
        w_cap = max(1, int(cache_snapshot.capacity * c_window_pct))
        main_cap = max(1, cache_snapshot.capacity - w_cap)
        prot_cap = int(main_cap * c_protected_pct)

        while len(q_protected) > prot_cap:
            demoted, _ = q_protected.popitem(last=False)
            q_probation[demoted] = None # Demote to Probation Tail
    elif key in q_protected:
        q_protected.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Init/Reset/Aging.
    - Insert into Window.
    '''
    global freq, door, q_window, q_probation, q_protected

    # Init
    if cache_snapshot.access_count <= 1:
        q_window.clear()
        q_probation.clear()
        q_protected.clear()
        freq.clear()
        door.clear()

    # Aging: Every 5*Capacity accesses
    if cache_snapshot.access_count > 0 and cache_snapshot.access_count % (5 * cache_snapshot.capacity) == 0:
        for k in list(freq.keys()):
            freq[k] //= 2
            if freq[k] == 0:
                del freq[k]
        door.clear()

    key = obj.key
    q_window[key] = None

    if key not in door:
        door.add(key)
    else:
        freq[key] = min(freq.get(key, 0) + 1, 60)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After Evict:
    - Cleanup from queues.
    '''
    global q_window, q_probation, q_protected
    key = evicted_obj.key

    if key in q_window: del q_window[key]
    if key in q_probation: del q_probation[key]
    if key in q_protected: del q_protected[key]

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