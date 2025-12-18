# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# W-TinyLFU Globals
w_lru = OrderedDict()      # Window Cache
slru_prob = OrderedDict()  # Probation (Main)
slru_prot = OrderedDict()  # Protected (Main)
freq = {}                  # Frequency counters
door = set()               # Doorkeeper (first access filter)

def evict(cache_snapshot, obj):
    '''
    W-TinyLFU Eviction Decision:
    - If Window has space (or Main is empty), evict from Main to make room.
    - If Window is full, conduct a duel between Window LRU and Main LRU.
    - Winner stays (or moves to Main), Loser is evicted.
    '''
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.01))

    # Identify Candidates
    cand_w = next(iter(w_lru)) if w_lru else None

    cand_m = None
    if slru_prob:
        cand_m = next(iter(slru_prob))
    elif slru_prot:
        cand_m = next(iter(slru_prot))

    # Boundary Cases
    if not cand_m: return cand_w if cand_w else next(iter(cache_snapshot.cache))
    if not cand_w: return cand_m # Should ideally not happen if inserted correctly

    # Logic
    if len(w_lru) < w_cap:
        return cand_m

    # Duel
    # Get frequencies (Doorkeeper logic applied on read)
    fw = freq.get(cand_w, 0) + (1 if cand_w in door else 0)
    fm = freq.get(cand_m, 0) + (1 if cand_m in door else 0)

    if fw > fm:
        return cand_m # Window wins, evict Main
    else:
        return cand_w # Main wins, evict Window

def update_after_hit(cache_snapshot, obj):
    '''
    Update frequency and cache positions on hit.
    '''
    key = obj.key

    # Update Frequency
    if key in freq:
        freq[key] += 1
    elif key in door:
        freq[key] = 1 # Move from door to freq
        door.remove(key) # Keep set small
    else:
        door.add(key)

    # Update Cache Structures
    if key in w_lru:
        w_lru.move_to_end(key)
    elif key in slru_prot:
        slru_prot.move_to_end(key)
    elif key in slru_prob:
        # Promote Probation -> Protected
        del slru_prob[key]
        slru_prot[key] = None

        # Enforce Protected Capacity (80% of Main)
        c = cache_snapshot.capacity
        w_cap = max(1, int(c * 0.01))
        main_cap = c - w_cap
        prot_cap = int(main_cap * 0.8)

        while len(slru_prot) > prot_cap:
            # Demote Protected LRU -> Probation MRU
            k, _ = slru_prot.popitem(last=False)
            slru_prob[k] = None

def update_after_insert(cache_snapshot, obj):
    '''
    Handle new insertion into Window and maintain Frequency aging.
    '''
    key = obj.key

    # Reset State on new trace detection
    if cache_snapshot.access_count <= 1:
        w_lru.clear()
        slru_prob.clear()
        slru_prot.clear()
        freq.clear()
        door.clear()

    # Aging: periodic reset of frequencies
    # Interval: 10 * Capacity
    if cache_snapshot.access_count % (cache_snapshot.capacity * 10) == 0:
        # Halve frequencies
        rem = []
        for k, v in freq.items():
            freq[k] = v // 2
            if freq[k] == 0: rem.append(k)
        for k in rem: del freq[k]
        door.clear()

    # Track Frequency
    if key in freq:
        freq[key] += 1
    elif key in door:
        freq[key] = 1
        door.remove(key)
    else:
        door.add(key)

    # Always insert new items to Window
    w_lru[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Clean up after eviction and handle Window->Main migration if needed.
    '''
    key = evicted_obj.key

    # Remove from structures
    if key in w_lru:
        del w_lru[key]
    elif key in slru_prob:
        del slru_prob[key]
    elif key in slru_prot:
        del slru_prot[key]

    # Check if we need to migrate Window LRU to Probation
    # This happens if we evicted from Main (Probation/Protected)
    # but the Window was full, meaning Window LRU pushed into Main.
    w_cap = max(1, int(cache_snapshot.capacity * 0.01))

    # If Window is still full (or overfull) after eviction, it means
    # we evicted from Main. To make space for the NEW item entering Window,
    # the current Window LRU must move to Probation.
    if len(w_lru) >= w_cap:
        k, _ = w_lru.popitem(last=False)
        slru_prob[k] = None

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