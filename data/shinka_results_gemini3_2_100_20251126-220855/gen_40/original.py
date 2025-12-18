# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict
import random

# S3-Jitter-Ghost Global State
s3_small = OrderedDict() # FIFO queue for new/probationary items
s3_main = OrderedDict()  # LRU queue for promoted/protected items
s3_ghost = OrderedDict() # FIFO queue for tracking history of evicted items
s3_freq = {}             # Frequency map for items in Small
s3_params = {}           # Configuration parameters
last_access_count = -1

def _reset_state(cache_snapshot):
    """Resets global state if a new trace is detected."""
    global s3_small, s3_main, s3_ghost, s3_freq, s3_params, last_access_count
    if cache_snapshot.access_count < last_access_count:
        s3_small.clear()
        s3_main.clear()
        s3_ghost.clear()
        s3_freq.clear()
        s3_params.clear()
    last_access_count = cache_snapshot.access_count
    
    # Initialize parameters once per trace
    if not s3_params:
        cap = cache_snapshot.capacity
        s3_params['small_ratio'] = 0.1
        # Jitter range: +/- 5% of capacity
        s3_params['jitter_range'] = max(1, int(cap * 0.05))
        # Probability to randomly save a victim (Anti-Thrashing)
        s3_params['lucky_save_prob'] = 0.01

def evict(cache_snapshot, obj):
    '''
    Eviction Policy:
    - Calculates a jittered target size for the Small queue.
    - Tries to evict from Small first.
    - Promotes items from Small to Main if they have >0 hits.
    - Uses "Lucky Save" (epsilon-greedy) to keep random items, breaking loops.
    - If Small is safe, evicts from Main (LRU).
    '''
    _reset_state(cache_snapshot)
    capacity = cache_snapshot.capacity
    
    # Dynamic target size for Small queue to avoid resonance
    jitter = random.randint(-s3_params['jitter_range'], s3_params['jitter_range'])
    target_small = max(1, int(capacity * s3_params['small_ratio']) + jitter)
    
    # 1. Process Small Queue
    # We loop here because we might promote items instead of evicting
    while len(s3_small) > target_small or (not s3_main and s3_small):
        # Peek at FIFO head (oldest)
        victim_key = next(iter(s3_small))
        
        # Check Promotion Criteria (at least 1 hit)
        if s3_freq.get(victim_key, 0) > 0:
            # Promote to Main
            s3_small.move_to_end(victim_key, last=False) # Helper to pop head
            s3_small.popitem(last=False)
            
            s3_main[victim_key] = 1
            s3_main.move_to_end(victim_key) # Insert at MRU
            
            # Remove from freq map (only tracks Small)
            if victim_key in s3_freq:
                del s3_freq[victim_key]
            # Loop continues to find next victim
        else:
            # Valid victim found (0 hits)
            # Lucky Save: Random chance to spare this item
            if random.random() < s3_params['lucky_save_prob']:
                # Move to end (newest) - give it another cycle
                s3_small.move_to_end(victim_key)
            else:
                return victim_key

    # 2. Process Main Queue (LRU)
    if s3_main:
        # Lucky Save for Main (Anti-Loop for Main segment)
        # Try up to 3 times to find a victim that isn't "lucky"
        for _ in range(3):
            victim_key = next(iter(s3_main)) # LRU head
            if random.random() < s3_params['lucky_save_prob']:
                s3_main.move_to_end(victim_key) # Save to MRU
            else:
                return victim_key
        # Fallback if we saved 3 times
        return next(iter(s3_main))

    # Fallback (should be covered by while loop)
    if s3_small:
        return next(iter(s3_small))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Main: Move to MRU.
    - Small: Increment freq (lazy promotion).
    '''
    _reset_state(cache_snapshot)
    key = obj.key
    
    if key in s3_main:
        s3_main.move_to_end(key)
    elif key in s3_small:
        # Increment freq, cap at 3 to avoid infinite growth
        s3_freq[key] = min(s3_freq.get(key, 0) + 1, 3)
    else:
        # Recovery for inconsistency
        s3_main[key] = 1
        s3_main.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert (Miss):
    - Ghost: If present, promote directly to Main (High Value).
    - Else: Insert into Small (Probation).
    '''
    _reset_state(cache_snapshot)
    key = obj.key
    
    if key in s3_ghost:
        # Resurrect from Ghost directly to Main
        s3_main[key] = 1
        s3_main.move_to_end(key)
        del s3_ghost[key]
    else:
        # New item to Small
        s3_small[key] = 1
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Eviction:
    - Remove from queues.
    - Add to Ghost list for history tracking.
    '''
    key = evicted_obj.key
    
    if key in s3_small:
        del s3_small[key]
        if key in s3_freq:
            del s3_freq[key]
    elif key in s3_main:
        del s3_main[key]
        
    # Add to Ghost
    s3_ghost[key] = 1
    # Limit Ghost size to capacity
    if len(s3_ghost) > cache_snapshot.capacity:
        s3_ghost.popitem(last=False)
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