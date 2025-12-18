# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads
LeCaR-style hybrid of LRU and LFU with online multiplicative-weights learning.
"""

from collections import OrderedDict

# Recency and timestamps
lru_order = OrderedDict()   # resident keys in LRU order (LRU at beginning)
m_key_timestamp = dict()    # last access timestamp (for tie-breaking)

# Lightweight LFU with periodic decay
m_key_freq = dict()         # resident frequency counter
last_freq_decay_access = 0

# LeCaR expert weights (deterministic variant)
w_lru = 0.5
w_lfu = 0.5
learning_rate = 0.15  # multiplicative weights learning rate
last_weight_decay_access = 0

# Track who evicted a key last for regret update
last_evicted_by = dict()    # key -> 'LRU' or 'LFU'
last_policy_used = None     # 'LRU' or 'LFU' on last eviction

# Cache capacity snapshot (object count capacity from framework)
cache_capacity = None


def _ensure_capacity(cache_snapshot):
    global cache_capacity
    if cache_capacity is None:
        cache_capacity = max(int(cache_snapshot.capacity), 1)


def _resync(cache_snapshot):
    # Ensure our LRU order matches the actual cache content
    cache_keys = set(cache_snapshot.cache.keys())

    # Remove non-residents from metadata
    for k in list(lru_order.keys()):
        if k not in cache_keys:
            lru_order.pop(k, None)
            m_key_freq.pop(k, None)
            m_key_timestamp.pop(k, None)

    # Add any resident key we didn't track (seed at MRU)
    for k in cache_keys:
        if k not in lru_order:
            lru_order[k] = True
        # keep a default freq entry
        if k not in m_key_freq:
            m_key_freq[k] = 1

    # Keep order stable; nothing more needed


def _move_to_mru(key):
    if key in lru_order:
        lru_order.pop(key, None)
    lru_order[key] = True


def _lru_victim():
    if lru_order:
        return next(iter(lru_order))
    return None


def _lfu_victim():
    # Choose resident key with minimal frequency; tie-break by oldest timestamp then LRU
    if not lru_order:
        return None
    best_k = None
    best_tuple = None
    for k in lru_order.keys():
        f = m_key_freq.get(k, 0)
        ts = m_key_timestamp.get(k, 0)
        cand = (f, ts)  # lower freq, older timestamp is worse
        if best_tuple is None or cand < best_tuple:
            best_tuple = cand
            best_k = k
    if best_k is None:
        best_k = _lru_victim()
    return best_k


def _maybe_decay_freq(cache_snapshot):
    # Periodically decay frequencies to keep them fresh and bounded
    global last_freq_decay_access
    _ensure_capacity(cache_snapshot)
    interval = max(1000, cache_capacity)
    if cache_snapshot.access_count - last_freq_decay_access >= interval:
        for k in list(m_key_freq.keys()):
            v = m_key_freq[k]
            nv = (v + 1) // 2  # halve, keep at least 0 or drop
            if nv <= 0:
                m_key_freq.pop(k, None)
            else:
                m_key_freq[k] = nv
        last_freq_decay_access = cache_snapshot.access_count


def _maybe_decay_weights(cache_snapshot):
    # Gentle drift back toward equal weights to avoid lock-in under shifts
    global w_lru, w_lfu, last_weight_decay_access
    _ensure_capacity(cache_snapshot)
    interval = max(512, cache_capacity)
    if cache_snapshot.access_count - last_weight_decay_access >= interval:
        # Move 10% toward 0.5
        w_lru = 0.9 * w_lru + 0.1 * 0.5
        w_lfu = 1.0 - w_lru
        last_weight_decay_access = cache_snapshot.access_count


def _update_weights_on_miss(missed_key):
    # Penalize the policy that evicted this key previously
    global w_lru, w_lfu
    ev = last_evicted_by.get(missed_key)
    if ev == 'LRU':
        # Decrease trust in LRU, increase LFU accordingly
        w_lru = max(0.01, w_lru * (1.0 - learning_rate))
        # normalize
        total = w_lru + w_lfu
        w_lru /= total
        w_lfu = 1.0 - w_lru
    elif ev == 'LFU':
        w_lfu = max(0.01, w_lfu * (1.0 - learning_rate))
        total = w_lru + w_lfu
        w_lfu /= total
        w_lru = 1.0 - w_lfu
    # Clamp
    w_lru = min(max(w_lru, 0.01), 0.99)
    w_lfu = 1.0 - w_lru


def evict(cache_snapshot, obj):
    '''
    Choose eviction victim using LeCaR-style expert selection (deterministic).
    '''
    global last_policy_used
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)
    _maybe_decay_weights(cache_snapshot)

    # Pick expert deterministically by higher weight
    if w_lru >= w_lfu:
        victim = _lru_victim()
        last_policy_used = 'LRU'
    else:
        victim = _lfu_victim()
        last_policy_used = 'LFU'

    if victim is None:
        # Fallbacks
        victim = _lru_victim()
        if victim is None and cache_snapshot.cache:
            victim = next(iter(cache_snapshot.cache.keys()))
        if victim is None:
            last_policy_used = None
    return victim


def update_after_hit(cache_snapshot, obj):
    '''
    On hit: update recency, frequency, timestamp.
    '''
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)
    _maybe_decay_weights(cache_snapshot)

    k = obj.key
    _move_to_mru(k)
    m_key_freq[k] = m_key_freq.get(k, 0) + 1
    m_key_timestamp[k] = cache_snapshot.access_count


def update_after_insert(cache_snapshot, obj):
    '''
    On insert (miss): admit new key, update learning weights based on regret.
    '''
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)
    _maybe_decay_weights(cache_snapshot)

    k = obj.key
    # Regret update: this access was a miss; penalize the policy that evicted k last
    _update_weights_on_miss(k)

    # Insert at MRU and seed minimal frequency
    _move_to_mru(k)
    m_key_freq[k] = m_key_freq.get(k, 0) + 1
    m_key_timestamp[k] = cache_snapshot.access_count


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After evicting victim: record who evicted it, and clean metadata.
    '''
    k = evicted_obj.key
    # Record the evicting expert for regret on future miss
    if last_policy_used in ('LRU', 'LFU'):
        last_evicted_by[k] = last_policy_used
    else:
        # Default to LRU if uncertain
        last_evicted_by[k] = 'LRU'

    # Remove from resident structures
    lru_order.pop(k, None)
    m_key_freq.pop(k, None)
    m_key_timestamp.pop(k, None)

    # Reset last policy marker
    # (kept implicit for next eviction decision)

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