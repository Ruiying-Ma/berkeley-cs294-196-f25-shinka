# EVOLVE-BLOCK-START
"""
WALRUS: Window-LFU with Aging and Ghost Frequencies
Combines a FIFO Window for scan resistance with a Frequency-based Main segment.
Uses randomized sampling for approximate LFU eviction in the Main segment.
Maintains ghost frequencies to support loop patterns and applies periodic aging.
"""
import random
from collections import deque

class WalrusState:
    def __init__(self, capacity):
        self.capacity = capacity
        self.window = deque()          # FIFO queue for new items
        self.main = set()              # Set of keys in the Main segment
        self.freq = {}                 # Global frequency counter (includes ghosts)
        self.access_count = 0          # Total access counter
        self.aging_interval = capacity # Age frequencies every 'capacity' accesses

        # Tuning parameters
        self.window_ratio = 0.1
        self.sample_size = 5
        self.max_freq_history = capacity * 5

    def get_freq(self, key):
        return self.freq.get(key, 0)

    def inc_freq(self, key):
        curr = self.freq.get(key, 0)
        if curr < 15:
            self.freq[key] = curr + 1

    def age_freqs(self):
        # Halve frequencies to bias towards recent popularity
        keys_to_remove = []
        for k, v in self.freq.items():
            new_v = v >> 1 # Integer division by 2
            if new_v == 0:
                keys_to_remove.append(k)
            else:
                self.freq[k] = new_v

        for k in keys_to_remove:
            # Don't remove if currently in cache!
            if k not in self.main and k not in self.window:
                del self.freq[k]
            else:
                self.freq[k] = 1 # Keep at least 1 if in cache

_walrus_state = {}

def get_state(cache_snapshot):
    cache_id = id(cache_snapshot.cache)
    if cache_id not in _walrus_state:
        _walrus_state[cache_id] = WalrusState(cache_snapshot.capacity)
    return _walrus_state[cache_id]

def evict(cache_snapshot, obj):
    '''
    Decide eviction victim based on Window vs Main duel.
    '''
    state = get_state(cache_snapshot)

    # Clean up state if cache was reset externally
    if not cache_snapshot.cache and (state.window or state.main):
        state.window.clear()
        state.main.clear()
        state.freq.clear()

    # Target Window Size
    target_window = max(1, int(cache_snapshot.capacity * state.window_ratio))

    victim = None

    # Scenario 1: Window is full (or over quota)
    # Check if we should evict from Window or if Window victim can displace a Main item
    if len(state.window) >= target_window:
        # Candidate from Window (FIFO Tail)
        w_candidate = state.window[0]

        # If Main is empty, we must evict from Window
        if not state.main:
            return w_candidate

        w_freq = state.get_freq(w_candidate)

        # Strict Admission: Window item must have > 1 frequency (at least one hit)
        # to challenge Main. This filters one-hit wonders (scans).
        if w_freq <= 1:
            return w_candidate

        # Candidate from Main (Approximate LFU via Sampling)
        sample_keys = random.sample(list(state.main), min(len(state.main), state.sample_size))
        m_candidate = min(sample_keys, key=lambda k: state.get_freq(k))

        # Duel: Frequency check
        if w_freq > state.get_freq(m_candidate):
            victim = m_candidate
        else:
            victim = w_candidate

    # Scenario 2: Window is under quota.
    # Ideally we make space in Main to allow Window to grow.
    else:
        if state.main:
            sample_keys = random.sample(list(state.main), min(len(state.main), state.sample_size))
            victim = min(sample_keys, key=lambda k: state.get_freq(k))
        elif state.window:
             victim = state.window[0]
        else:
             victim = next(iter(cache_snapshot.cache))

    return victim

def update_after_hit(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    state.access_count += 1
    state.inc_freq(obj.key)

    # Aging
    if state.access_count % state.aging_interval == 0:
        state.age_freqs()

def update_after_insert(cache_snapshot, obj):
    state = get_state(cache_snapshot)
    state.access_count += 1
    state.inc_freq(obj.key)

    # New items always enter Window
    state.window.append(obj.key)

    # Clean up frequency map if growing too large (Ghost cleanup)
    if len(state.freq) > state.max_freq_history:
        # Simple cleanup heuristic could go here, but relying on aging is usually sufficient
        pass

    if state.access_count % state.aging_interval == 0:
        state.age_freqs()

def update_after_evict(cache_snapshot, obj, evicted_obj):
    state = get_state(cache_snapshot)
    key = evicted_obj.key

    # Remove from local structures
    if key in state.main:
        state.main.remove(key)

    if state.window:
        if state.window[0] == key:
            state.window.popleft()
        else:
            try:
                state.window.remove(key)
            except ValueError:
                pass

    # Logic Update:
    # If we evicted a Main item to make room for a Window item (Duel won),
    # The Window item (the duel winner) must be moved to Main to free up the Window slot.
    # We detect this condition by checking if Window is over capacity.
    target_window = max(1, int(cache_snapshot.capacity * state.window_ratio))
    while len(state.window) > target_window:
        # Promote the oldest window item to Main
        winner = state.window.popleft()
        state.main.add(winner)
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