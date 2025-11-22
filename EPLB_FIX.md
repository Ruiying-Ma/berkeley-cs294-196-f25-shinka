# EPLB Debug Fix

## Problem Identified ✓

All programs showed the **exact same score (0.750)** with no variation, resulting in zero improvement across all generations.

### Root Cause

The evaluation was using **dummy/placeholder scores** because the required workload file was missing:

**Missing File**: `openevolve_examples/eplb/expert-load.json` (223MB real workload data)

When this file doesn't exist, the evaluator returns hardcoded dummy values (see `examples/eplb/evaluate.py` lines 211-224):

```python
else:
    # Dummy metrics if workload file not available
    avg_balancedness = 0.5      # Always 0.5
    speed_score = 1.0            # Always 1.0
    combined_score = 0.75        # Always 0.75 (average of above)
```

This meant:
- ✗ Every program got the same score (0.75)
- ✗ No differentiation between good/bad algorithms
- ✗ Evolution couldn't optimize anything
- ✗ LLM had no signal to learn from

### Secondary Issue

The `evaluator.py` file was also using a relative path (`expert-load.json`) that depended on the current working directory, causing import failures when run from different locations.

## Solutions Applied ✓

### 1. Downloaded Workload Data

```bash
cd /Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve/openevolve_examples/eplb
curl -L -o expert-load.json "https://huggingface.co/datasets/abmfy/eplb-openevolve/resolve/main/expert-load.json"
```

**File size**: 223MB of real vLLM expert workload statistics
**Source**: HuggingFace dataset (abmfy/eplb-openevolve)

### 2. Fixed Path Resolution

Updated `openevolve_examples/eplb/evaluator.py` line 11:

**Before:**
```python
WORKLOAD_PATH = "expert-load.json"  # Relative to CWD
```

**After:**
```python
WORKLOAD_PATH = os.path.join(os.path.dirname(__file__), "expert-load.json")  # Relative to file
```

This ensures the workload file is found regardless of where the evaluation is run from.

## Verification ✓

**Before Fix:**
- All programs: score = 0.750 (dummy value)
- Balancedness: 0.5 (dummy value)
- Speed: 1.0 (dummy value)
- No variation possible

**After Fix:**
- Initial program: score = **0.6555**
- Balancedness: **0.3111** (real performance on workloads)
- Speed: 1.0 (measured execution time ~0.25s)
- **Scores now vary based on actual algorithm quality!**

### Sample Evaluation Output

```
Run 1/5 completed in 0.25 seconds
Run 2/5 completed in 0.25 seconds
Run 3/5 completed in 0.25 seconds
Run 4/5 completed in 0.25 seconds
Run 5/5 completed in 0.25 seconds

balancedness: 0.211 (workload 1)
balancedness: 0.340 (workload 2)
balancedness: 0.367 (workload 3)
balancedness: 0.353 (workload 4)
balancedness: 0.285 (workload 5)

combined_score: 0.6555385907025855
private: {
  'avg_balancedness_score': 0.3110771814051708,
  'speed_score': 1.0
}
```

## Next Steps

### Start Fresh Evolution Run

With real workloads now available, start a new evolution run:

```bash
cd /Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve
python -m shinka.launch_hydra variant=eplb_example
```

Now the evolution will:
- ✓ Evaluate programs on real vLLM workload data
- ✓ Get meaningful differentiation between algorithms
- ✓ Optimize for actual load balancing performance
- ✓ See improvements as programs evolve

### What to Expect

With real evaluation:
- **Balancedness scores** will vary (0.0 to 1.0, higher = better)
- **Speed scores** will reflect execution time (faster = better)
- **Combined scores** will show real optimization progress
- Evolution should find programs that **beat the 0.656 baseline**

### Optimization Targets

The EPLB algorithm has two objectives:
1. **Maximize balancedness** (distribute load evenly across GPUs)
   - Current baseline: 0.311
   - Maximum possible: 1.0
   - **Lots of room for improvement!**

2. **Minimize execution time** (algorithm speed)
   - Current baseline: ~0.25s
   - Can be optimized with better heuristics

## Why This Happened

The README mentioned downloading the workload file but:
1. It wasn't downloaded automatically during setup
2. The system fell back to dummy data silently
3. The warning message was easy to miss in logs
4. Evolution ran successfully (no errors) but with meaningless scores

## Related Files Changed

1. ✓ `/openevolve_examples/eplb/expert-load.json` - Downloaded (223MB)
2. ✓ `/openevolve_examples/eplb/evaluator.py` - Fixed path resolution (line 11)

## Summary

- **Issue**: Missing workload data → all programs got dummy score 0.75
- **Impact**: Zero improvement possible, evolution was meaningless
- **Fix**: Downloaded real workload data (223MB) + fixed path resolution
- **Result**: Programs now get real scores (baseline: 0.656) with variation
- **Status**: Ready for meaningful evolution! ✓
- **Action**: Start fresh evolution run with real workload evaluation

