# EVOLVE-BLOCK-START
"""S3-LRU with Windowed Small Eviction, Random Victim, and Jitter"""
import random
import itertools

# Global State
# s3_small: FIFO queue for the small segment (probation)
# s3_main: LRU queue for the main segment (protected)
# s3_ghost: Ghost cache for tracking eviction from small
# s3_freq: Frequency counter for objects (max 3)
s3_small = {}
s3_main = {}
s3_ghost = {}
s3_freq = {}

def evict(cache_snapshot, obj):
    '''
    S3-LRU with Windowed Small Eviction, Random Victim, and Jitter.
    
    Combines best aspects of previous attempts:
    1.  **S3-LRU Structure**: Main segment uses LRU (Move-to-MRU) which performs best on 
        most traces (from Current Program).
    2.  **3x Ghost**: Extended history for rescue (from Current Program).
    3.  **Probabilistic Promotion**: 1% chance to promote scan items (from Current Program).
    4.  **Jittered Small Capacity**: Target +/- 1% to break loops (from Prior Program).
    5.  **Windowed Random Eviction in Small**: Look at K=5 items. Check all for promotion. 
        If none, pick a RANDOM victim. This randomness significantly reduces thrashing 
        on synchronized loops (from Prior Program).
    '''
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity
    
    # 1. Jittered Small Capacity
    # Target 10% +/- 1%
    noise = max(1, int(capacity * 0.01))
    s_capacity = max(1, int(capacity * 0.1) + random.randint(-noise, noise))

    # 2. Ghost Cleanup (3x Capacity)
    while len(s3_ghost) > 3 * capacity:
        s3_ghost.pop(next(iter(s3_ghost)))
        
    k_window = 5

    while True:
        # 3. Decision: Small or Main?
        if len(s3_small) >= s_capacity or not s3_main:
            # Evict from Small
            if not s3_small:
                return None
            
            # Windowed Scan: Check first K items
            candidates = list(itertools.islice(s3_small, k_window))
            
            promoted = False
            for cand in candidates:
                freq = s3_freq.get(cand, 0)
                
                # Merit Promotion (Hit)
                if freq > 0:
                    s3_small.pop(cand)
                    s3_main[cand] = None
                    s3_freq[cand] = 0
                    promoted = True
                    break
                
                # Probabilistic Promotion (1%)
                if (cache_snapshot.access_count ^ hash(cand)) % 100 == 0:
                    s3_small.pop(cand)
                    s3_main[cand] = None
                    s3_freq[cand] = 0
                    promoted = True
                    break
            
            if promoted:
                continue

            # No promotion? Pick Random Victim from Window
            # Randomness helps break loop synchronization
            return random.choice(candidates)

        else:
            # Evict from Main (LRU)
            candidate = next(iter(s3_main))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Second Chance: Reinsert at MRU (tail)
                s3_main.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = freq - 1
                continue
            
            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Increment frequency.
    - Move to MRU in Main (True LRU).
    '''
    global s3_freq, s3_main
    key = obj.key
    s3_freq[key] = min(s3_freq.get(key, 0) + 1, 3)

    if key in s3_main:
        val = s3_main.pop(key)
        s3_main[key] = val

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Check Ghost -> Main.
    - Else -> Small.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key
    s3_freq[key] = 0

    if key in s3_ghost:
        s3_main[key] = None
        s3_ghost.pop(key)
    else:
        s3_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Cleanup queues.
    - Small evictions go to Ghost.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)

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