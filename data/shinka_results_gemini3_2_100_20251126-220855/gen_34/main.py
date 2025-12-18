# EVOLVE-BLOCK-START
"""Robust S3-LRU with Jitter and Random Eviction for Loop Breaking"""
from collections import OrderedDict
import random

# Global State
s3_small = OrderedDict() # FIFO for new/probationary items
s3_main = OrderedDict()  # LRU for promoted/protected items
s3_freq = {}             # Frequency counter for items in Small
s3_config = {}           # Configuration parameters
last_access_count = -1

def _reset_state(cache_snapshot):
    """Resets global state if a new trace is detected."""
    global s3_small, s3_main, s3_freq, s3_config, last_access_count
    if cache_snapshot.access_count < last_access_count:
        s3_small.clear()
        s3_main.clear()
        s3_freq.clear()
        s3_config.clear()
    last_access_count = cache_snapshot.access_count
    
    # Initialize config
    if not s3_config:
        cap = cache_snapshot.capacity
        # Small queue target: ~10% of capacity
        s3_config['small_ratio'] = 0.1
        # Epsilon for randomized eviction to break loops
        s3_config['epsilon'] = 0.05
        # Jitter magnitude: +/- 5% of capacity
        s3_config['jitter_mag'] = max(1, int(cap * 0.05))

def evict(cache_snapshot, obj):
    '''
    Robust S3-LRU:
    1. Small (FIFO) and Main (LRU) queues.
    2. Promotion to Main if accessed while in Small (Frequency >= 1).
    3. Dynamic sizing of Small (Jitter) to prevent thrashing.
    4. Epsilon-greedy eviction from Small to break synchronization loops.
    '''
    _reset_state(cache_snapshot)
    capacity = cache_snapshot.capacity
    
    # Dynamic target for Small queue size (Jitter)
    # Jitter helps prevent partition boundaries from aligning with access loops
    jitter = random.randint(-s3_config['jitter_mag'], s3_config['jitter_mag'])
    target_small = int(capacity * s3_config['small_ratio']) + jitter
    target_small = max(1, target_small)

    while True:
        # Determine eviction source
        # Prefer evicting from Small if it exceeds target size, or if Main is empty
        evict_from_small = (len(s3_small) > target_small) or (not s3_main)
            
        if evict_from_small and s3_small:
            # Peek at the oldest item in Small (Head)
            candidate = next(iter(s3_small))
            
            # Promotion Check: Has it been accessed since insertion?
            # Standard S3-FIFO promotes if frequency > 0 (accessed at least once in cache)
            if s3_freq.get(candidate, 0) > 0:
                # Promote to Main
                del s3_small[candidate]
                if candidate in s3_freq:
                    del s3_freq[candidate]
                
                # Insert into Main (MRU position)
                s3_main[candidate] = 1
                s3_main.move_to_end(candidate)
                # We promoted, so we still need to evict. Continue loop.
                continue
            else:
                # Candidate has 0 hits in cache. It is a victim.
                # Anti-Thrashing: Epsilon-Greedy Eviction
                # With small probability, evict a random item from the head region of Small
                # This breaks strict FIFO loop patterns (e.g., Trace 14)
                if random.random() < s3_config['epsilon'] and len(s3_small) > 5:
                    # Pick from first few items to maintain rough FIFO but add noise
                    keys = []
                    it = iter(s3_small)
                    try:
                        for _ in range(5):
                            keys.append(next(it))
                    except StopIteration:
                        pass
                    return random.choice(keys)
                else:
                    return candidate
        else:
            # Evict from Main (LRU policy)
            # Main is OrderedDict, iter gives keys in insertion order (oldest/LRU first)
            if s3_main:
                return next(iter(s3_main))
            # Fallback (should not be reached if logic is correct and cache is full)
            if s3_small:
                return next(iter(s3_small))
            return None

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Main: Move to MRU (True LRU).
    - Small: Increment frequency (Lazy Promotion).
    '''
    _reset_state(cache_snapshot)
    key = obj.key
    
    if key in s3_main:
        s3_main.move_to_end(key)
    elif key in s3_small:
        s3_freq[key] = s3_freq.get(key, 0) + 1
    else:
        # Recovery for state inconsistency (e.g. initial loading)
        s3_main[key] = 1
        s3_main.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Always insert into Small (FIFO).
    '''
    _reset_state(cache_snapshot)
    key = obj.key
    s3_small[key] = 1
    s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Clean up internal maps.
    '''
    key = evicted_obj.key
    if key in s3_small:
        del s3_small[key]
        if key in s3_freq:
            del s3_freq[key]
    elif key in s3_main:
        del s3_main[key]
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