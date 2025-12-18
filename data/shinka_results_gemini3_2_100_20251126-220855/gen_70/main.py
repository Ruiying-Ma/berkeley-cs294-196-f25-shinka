# EVOLVE-BLOCK-START
"""Adaptive S3-FIFO with Jitter, Ghost Lists, and Windowed Eviction"""
import random
import itertools

# Global State
# s3_small: FIFO queue for the small segment (probation)
# s3_main: LRU queue for the main segment (protected)
# s3_ghost_small: Ghost cache for tracking eviction from small
# s3_ghost_main: Ghost cache for tracking eviction from main
# s3_freq: Frequency counter for objects (max 3)
# s3_ratio: Adaptive ratio for Small queue size (default 0.1)
s3_small = {}
s3_main = {}
s3_ghost_small = {}
s3_ghost_main = {}
s3_freq = {}
s3_ratio = 0.1
last_access_count = 0

def check_reset(cache_snapshot):
    """Reset globals if access_count decreases (indicating a new trace)."""
    global s3_small, s3_main, s3_ghost_small, s3_ghost_main, s3_freq, s3_ratio, last_access_count
    if cache_snapshot.access_count < last_access_count:
        s3_small.clear()
        s3_main.clear()
        s3_ghost_small.clear()
        s3_ghost_main.clear()
        s3_freq.clear()
        s3_ratio = 0.1
    last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Adaptive S3-FIFO with Jitter and Windowed Eviction.
    
    Combined Features:
    - S3-FIFO Structure: Small (Probation) & Main (Protected).
    - Adaptive Sizing: Adjusts Small/Main ratio based on ghost hits.
    - Ghost Lists: Tracks evictions from both Small and Main to guide adaptation.
    - Jitter: Randomized S capacity to break loops.
    - Windowed Scan: Looks ahead K items to find promotions without Head-of-Line blocking.
    - Random Victim in S: Randomly selects victim in Small window to reduce thrashing.
    '''
    check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost_small, s3_ghost_main, s3_freq, s3_ratio

    capacity = cache_snapshot.capacity
    
    # 1. Adaptive S Capacity with Jitter
    # Base target from adaptive ratio
    target_s = int(capacity * s3_ratio)
    
    # Add Jitter (Noise) to break synchronization
    noise_range = max(1, int(capacity * 0.01))
    s_capacity = max(1, target_s + random.randint(-noise_range, noise_range))

    # 2. Ghost Cleanup
    while len(s3_ghost_small) > capacity:
        s3_ghost_small.pop(next(iter(s3_ghost_small)))
    while len(s3_ghost_main) > capacity:
        s3_ghost_main.pop(next(iter(s3_ghost_main)))

    k_window = 5

    while True:
        # 3. Queue Selection
        if len(s3_small) >= s_capacity or not s3_main:
            queue = s3_small
            is_small = True
        else:
            queue = s3_main
            is_small = False
            
        if not queue:
            return None

        # 4. Window Scan
        candidates = list(itertools.islice(queue, k_window))
        
        # 5. Check Promotions / Maintenance
        promoted = False
        for key in candidates:
            freq = s3_freq.get(key, 0)
            if freq > 0:
                if is_small:
                    # Promote S -> M
                    s3_small.pop(key)
                    s3_main[key] = None
                    s3_freq[key] = 0 # Reset freq
                else:
                    # Reinsert M -> M (Second Chance)
                    s3_main.pop(key)
                    s3_main[key] = None # Move to MRU
                    s3_freq[key] = freq - 1
                
                promoted = True
                break
        
        if promoted:
            continue
            
        # 6. Victim Selection
        if is_small:
            # Small Queue: Random victim in window (Break loops)
            return random.choice(candidates)
        else:
            # Main Queue: LRU victim (Head)
            return candidates[0]

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global s3_freq, s3_main
    key = obj.key
    s3_freq[key] = min(s3_freq.get(key, 0) + 1, 3)

    if key in s3_main:
        # Move to MRU
        val = s3_main.pop(key)
        s3_main[key] = val

def update_after_insert(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost_small, s3_ghost_main, s3_freq, s3_ratio
    key = obj.key
    s3_freq[key] = 0

    # Adaptation Delta
    delta = max(0.01, 1.0 / cache_snapshot.capacity) if cache_snapshot.capacity > 0 else 0.01

    if key in s3_ghost_small:
        # Ghost S Hit: Small was too small
        s3_ratio = min(0.9, s3_ratio + delta)
        s3_main[key] = None # Rescue to Main
        s3_ghost_small.pop(key)
    elif key in s3_ghost_main:
        # Ghost M Hit: Main was too small
        s3_ratio = max(0.01, s3_ratio - delta)
        s3_main[key] = None # Rescue to Main
        s3_ghost_main.pop(key)
    else:
        # New Insert -> Small
        s3_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost_small, s3_ghost_main, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost_small[key] = None
    elif key in s3_main:
        s3_main.pop(key)
        s3_ghost_main[key] = None

    if key in s3_freq:
        s3_freq.pop(key)
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