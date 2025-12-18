# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

import random

# LeCaR (Learning Cache Replacement) Global State
m_lru = {}       # key -> last_access_timestamp
m_freq = {}      # key -> access_frequency
m_h_lru = set()  # Ghost LRU keys (History of LRU victims)
m_h_lfu = set()  # Ghost LFU keys (History of LFU victims)
m_w = 0.5        # Weight for LRU policy (probability to use LRU)
m_learning_rate = 0.05
m_last_access_count = 0
m_action = None  # Store the last eviction decision ('LRU', 'LFU', 'BOTH')

def check_reset(cache_snapshot):
    global m_last_access_count, m_lru, m_freq, m_h_lru, m_h_lfu, m_w, m_action
    current_count = cache_snapshot.access_count
    if current_count < m_last_access_count:
        m_lru.clear()
        m_freq.clear()
        m_h_lru.clear()
        m_h_lfu.clear()
        m_w = 0.5
        m_action = None
    m_last_access_count = current_count

def evict(cache_snapshot, obj):
    '''
    LeCaR eviction logic.
    '''
    check_reset(cache_snapshot)
    global m_lru, m_freq, m_w, m_action

    candidates = list(cache_snapshot.cache.keys())
    if not candidates:
        return None

    # Identify victims under policies
    # LRU victim: min timestamp
    victim_lru = min(candidates, key=lambda k: m_lru.get(k, 0))
    # LFU victim: min frequency, tie-breaker LRU
    victim_lfu = min(candidates, key=lambda k: (m_freq.get(k, 1), m_lru.get(k, 0)))

    if victim_lru == victim_lfu:
        victim = victim_lru
        m_action = 'BOTH'
    else:
        if random.random() < m_w:
            victim = victim_lru
            m_action = 'LRU'
        else:
            victim = victim_lfu
            m_action = 'LFU'

    return victim

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after hit.
    '''
    check_reset(cache_snapshot)
    global m_lru, m_freq
    m_lru[obj.key] = cache_snapshot.access_count
    m_freq[obj.key] = m_freq.get(obj.key, 0) + 1

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after insert. Handle learning from ghost hits.
    '''
    check_reset(cache_snapshot)
    global m_lru, m_freq, m_w, m_h_lru, m_h_lfu, m_learning_rate

    m_lru[obj.key] = cache_snapshot.access_count
    m_freq[obj.key] = 1  # Reset frequency on new insertion

    # Learning
    if obj.key in m_h_lru:
        # Hit in LRU history -> LRU was bad -> favor LFU -> decrease w
        m_w = max(0.01, m_w - m_learning_rate)
        m_h_lru.remove(obj.key)
        if obj.key in m_h_lfu: m_h_lfu.remove(obj.key)

    elif obj.key in m_h_lfu:
        # Hit in LFU history -> LFU was bad -> favor LRU -> increase w
        m_w = min(0.99, m_w + m_learning_rate)
        m_h_lfu.remove(obj.key)
        if obj.key in m_h_lru: m_h_lru.remove(obj.key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after eviction. Add to history.
    '''
    check_reset(cache_snapshot)
    global m_action, m_h_lru, m_h_lfu, m_lru, m_freq

    key = evicted_obj.key

    if m_action == 'LRU':
        m_h_lru.add(key)
    elif m_action == 'LFU':
        m_h_lfu.add(key)
    elif m_action == 'BOTH':
        m_h_lru.add(key)
        m_h_lfu.add(key)

    # Manage Ghost Size
    cap = cache_snapshot.capacity

    while len(m_h_lru) > cap:
        victim = min(m_h_lru, key=lambda k: m_lru.get(k, 0))
        m_h_lru.remove(victim)
        if victim not in m_h_lfu and victim in m_lru:
            del m_lru[victim]
            if victim in m_freq: del m_freq[victim]

    while len(m_h_lfu) > cap:
        victim = min(m_h_lfu, key=lambda k: m_lru.get(k, 0))
        m_h_lfu.remove(victim)
        if victim not in m_h_lru and victim in m_lru:
            del m_lru[victim]
            if victim in m_freq: del m_freq[victim]

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