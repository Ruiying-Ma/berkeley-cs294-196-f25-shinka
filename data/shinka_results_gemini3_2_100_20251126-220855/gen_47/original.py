# EVOLVE-BLOCK-START
"""Cache eviction algorithm: S3-LIFO-LRU with Ghost-Frequency Restoration"""
from collections import OrderedDict

# Global State
s3_small = OrderedDict() # Small queue (Probationary, LIFO eviction)
s3_main = OrderedDict()  # Main queue (Protected, LRU eviction)
s3_ghost = OrderedDict() # Ghost registry (FIFO, stores key -> freq)
s3_freq = {}             # Frequency map
s3_config = {}           # Configuration parameters
last_op_count = -1

def _reset_state(cache_snapshot):
    global s3_small, s3_main, s3_ghost, s3_freq, s3_config, last_op_count
    if cache_snapshot.access_count < last_op_count:
        s3_small.clear()
        s3_main.clear()
        s3_ghost.clear()
        s3_freq.clear()
        s3_config.clear()
    last_op_count = cache_snapshot.access_count

    if not s3_config:
        cap = cache_snapshot.capacity
        # Small queue size target (10%)
        s3_config['small_target'] = max(1, int(cap * 0.1))
        # Aging interval (once per capacity accesses)
        s3_config['aging_interval'] = cap

def _age_freqs():
    # Halve all frequencies
    for k in list(s3_freq):
        s3_freq[k] >>= 1
        if s3_freq[k] == 0:
            del s3_freq[k]

def evict(cache_snapshot, obj):
    '''
    S3-LIFO-LRU Eviction:
    - Ages frequencies periodically.
    - Favors evicting from Small (LIFO) to protect Main and filter scan/loops.
    - Promotes from Small to Main if freq > 1.
    - Fallback to Main (LRU).
    '''
    _reset_state(cache_snapshot)

    # Aging Logic
    if cache_snapshot.access_count % s3_config['aging_interval'] == 0:
        _age_freqs()

    target_small = s3_config['small_target']

    # 1. Try evicting from Small if it's over budget or if Main is empty
    # We use a loop to handle promotions
    while len(s3_small) > target_small or (not s3_main and s3_small):
        # LIFO Eviction: Inspect tail (newest)
        victim_key, _ = s3_small.popitem(last=True)

        # Check Promotion (freq > 1)
        if s3_freq.get(victim_key, 0) > 1:
            # Promote to Main (MRU)
            s3_main[victim_key] = 1
            s3_main.move_to_end(victim_key)
            # Remove from freq map (optional, or keep for history)
            # We keep it in s3_freq for continuity
        else:
            # Victim found. Put back for update_after_evict (or just return)
            # We must return a key that is present in the cache.
            # We popped it, so we must push it back to be consistent with external view
            s3_small[victim_key] = 1
            # move_to_end(last=True) is implied by assignment for new key
            return victim_key

    # 2. If Small is safe, evict from Main (LRU)
    if s3_main:
        return next(iter(s3_main))

    # Fallback (should be covered)
    if s3_small:
        return next(iter(s3_small))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Main: Move to MRU (Strict LRU).
    - Small: Increment freq (Lazy promotion).
    '''
    _reset_state(cache_snapshot)
    key = obj.key

    if key in s3_main:
        s3_main.move_to_end(key)
    elif key in s3_small:
        s3_freq[key] = s3_freq.get(key, 0) + 1

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Ghost Hit: Restore freq, Promote to Main.
    - New: Insert to Small (Probation).
    '''
    _reset_state(cache_snapshot)
    key = obj.key

    if key in s3_ghost:
        # Restore frequency
        restored_freq = s3_ghost.pop(key)
        s3_freq[key] = restored_freq
        # Promote to Main immediately
        s3_main[key] = 1
        s3_main.move_to_end(key)
    else:
        # New Item -> Small
        s3_small[key] = 1
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Remove from queues.
    - Save to Ghost (with frequency).
    '''
    key = evicted_obj.key
    capacity = cache_snapshot.capacity

    if key in s3_small:
        del s3_small[key]
    elif key in s3_main:
        del s3_main[key]

    # Save to Ghost
    current_freq = s3_freq.get(key, 0)
    s3_ghost[key] = current_freq
    s3_ghost.move_to_end(key) # Mark as recent in Ghost

    # Clean up freq map
    if key in s3_freq:
        del s3_freq[key]

    # Maintain Ghost size
    if len(s3_ghost) > capacity:
        s3_ghost.popitem(last=False) # Remove oldest ghost
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