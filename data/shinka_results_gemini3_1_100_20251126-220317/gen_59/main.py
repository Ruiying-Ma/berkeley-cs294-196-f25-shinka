# EVOLVE-BLOCK-START
"""
Optimized W-TinyLFU with Doorkeeper and SLRU
- Window Cache (1%): LRU for new items.
- Main Cache (99%): SLRU (80% Protected, 20% Probation).
- Improvements:
  - Immediate promotion from Probation to Protected on hit.
  - Biased eviction duel favoring Main items.
  - "Spare Main" logic: prevents evicting valuable Main items to grow Window.
  - Frequency capped at 15 (4-bit counter).
  - Size-based Doorkeeper reset.
"""
import random
from collections import OrderedDict

class TinyLFUState:
    def __init__(self, capacity):
        self.capacity = capacity
        # Cache Segments
        self.window = OrderedDict()
        self.probation = OrderedDict()
        self.protected = OrderedDict()
        
        # Metadata
        self.freq = {}
        self.doorkeeper = set()
        self.access_count = 0
        
        # Configuration
        # 1% Window, 99% Main (split 80/20 Protected/Probation)
        self.window_size = max(1, int(capacity * 0.01))
        main_capacity = max(1, capacity - self.window_size)
        self.protected_size = int(main_capacity * 0.80)
        
        # Aging and Constraints
        # Age every 5x capacity
        self.aging_interval = capacity * 5
        self.max_freq = 15
        self.doorkeeper_limit = max(1000, capacity * 2)

    def get_freq(self, key):
        return self.freq.get(key, 0)

    def record_access(self, key):
        self.access_count += 1
        
        # Doorkeeper & Frequency Logic
        if key in self.freq:
            if self.freq[key] < self.max_freq:
                self.freq[key] += 1
        elif key in self.doorkeeper:
            self.doorkeeper.remove(key)
            self.freq[key] = 1 # Promoted from Doorkeeper to Frequency Map
        else:
            self.doorkeeper.add(key)
            
        if len(self.doorkeeper) > self.doorkeeper_limit:
            self.doorkeeper.clear()
            
        if self.access_count % self.aging_interval == 0:
            self.age()

    def age(self):
        # Halve frequencies
        rem = []
        for k in self.freq:
            self.freq[k] //= 2
            if self.freq[k] == 0:
                rem.append(k)
        for k in rem:
            del self.freq[k]

_states = {}

def get_state(cache_snapshot):
    cid = id(cache_snapshot.cache)
    if cid not in _states:
        _states[cid] = TinyLFUState(cache_snapshot.capacity)
    return _states[cid]

def evict(cache_snapshot, obj):
    """
    Determine eviction victim using Window-TinyLFU with SLRU.
    """
    state = get_state(cache_snapshot)
    
    # Consistency Check
    if not cache_snapshot.cache and (state.window or state.probation):
        state.window.clear()
        state.probation.clear()
        state.protected.clear()
        state.freq.clear()
        state.doorkeeper.clear()

    # Identify Candidates
    w_victim = next(iter(state.window)) if state.window else None
    
    m_victim = None
    if state.probation:
        m_victim = next(iter(state.probation))
    elif state.protected:
        m_victim = next(iter(state.protected))
        
    # Handle empty segment cases
    if not m_victim:
        return w_victim
    if not w_victim:
        return m_victim

    # Eviction Logic
    # If Window is full (>= 1%), we restrict its size by dueling.
    if len(state.window) >= state.window_size:
        freq_w = state.get_freq(w_victim)
        freq_m = state.get_freq(m_victim)
        
        # Duel: Bias towards Main (incumbent). 
        # Only evict Main if Window item is clearly better (freq > freq_m + 1).
        if freq_w > freq_m + 1:
            return m_victim
        else:
            return w_victim
    else:
        # Window is not full. Prefer to evict Main to grow Window, 
        # UNLESS Main victim is valuable (freq > 0).
        if state.get_freq(m_victim) > 0:
            # Main item has value, spare it. Evict Window item instead.
            return w_victim
        else:
            # Main item is likely garbage (scan/one-hit). Evict it to admit new item to Window.
            return m_victim

def update_after_hit(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    key = obj.key
    state.record_access(key)
    
    if key in state.window:
        state.window.move_to_end(key)
        
    elif key in state.probation:
        # Immediate promotion to Protected
        del state.probation[key]
        state.protected[key] = True
        
        # Handle Protected Overflow: Move LRU to Probation
        if len(state.protected) > state.protected_size:
            victim, _ = state.protected.popitem(last=False)
            state.probation[victim] = True
            state.probation.move_to_end(victim)
            
    elif key in state.protected:
        state.protected.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    key = obj.key
    state.record_access(key)
    
    # New items always enter Window
    state.window[key] = True
    
    # Balance: If Window grew too large (due to Main eviction), move overflow to Probation
    if len(state.window) > state.window_size:
        victim, _ = state.window.popitem(last=False)
        state.probation[victim] = True
        state.probation.move_to_end(victim)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    state = get_state(cache_snapshot)
    key = evicted_obj.key
    
    if key in state.window:
        del state.window[key]
    elif key in state.probation:
        del state.probation[key]
    elif key in state.protected:
        del state.protected[key]
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