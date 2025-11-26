# PRISM Use Case - Quick Start Guide

## What is PRISM?

GPU Model Placement optimization: Place N ML models on M GPUs to minimize maximum KV cache pressure while respecting memory constraints.

## Installation

Already integrated! No additional dependencies needed.

## Usage

### 1️⃣ Test the Baseline (5 seconds)
```bash
cd examples/prism
python initial.py
```
**Expected**: `Max KVPR: 20.892`

### 2️⃣ Run Evaluation (10 seconds)
```bash
python evaluate.py --program_path initial.py --results_dir results
```
**Expected**: 
- ✅ Combined Score: ~21.89
- ✅ Success Rate: 1.000 (100%)

### 3️⃣ Run Evolution (hours)
```bash
python run_evo.py
```
**What happens**:
- 300 generations
- 2 islands with migration
- Multiple LLMs (GPT-5, Claude, Gemini)
- Results saved to `results_prism/`

## The Problem

**Given**:
- `gpu_num`: Number of GPUs (each with 80GB)
- `models`: List of models with size, req_rate, slo

**Goal**: 
Minimize max(KVPR) where:
```
KVPR = sum(req_rate/slo) / (80GB - sum(model_size))
```

**Constraints**:
- All models must fit on GPUs
- Can't exceed 80GB per GPU

## The Algorithm

**Baseline** (can be evolved):
1. Sort models by req_rate/slo (descending)
2. Place each model on GPU with min KVPR
3. Update GPU state

**Evolution Directions**:
- Better sorting strategies
- Look-ahead placement
- Load balancing
- Search-based methods
- Mathematical optimization

## Files Overview

```
examples/prism/
├── initial.py              # ← Evolution targets this
├── evaluate.py             # ← Scores solutions
├── run_evo.py              # ← Run this for evolution
├── README.md               # ← Full documentation
├── INTEGRATION_SUMMARY.md  # ← Integration details
└── test_integration.sh     # ← Verify setup

openevolve_examples/prism/
├── evaluator.py            # Original evaluation logic
├── initial_program.py      # Original baseline
└── config.yaml             # Original config
```

## Key Metrics

| Metric | Baseline | Target |
|--------|----------|--------|
| Combined Score | 1.0-1.5 | 1.5-2.0 |
| Success Rate | 100% | 100% |
| Execution Time | <1ms | <5s |

## Evolution Configuration

- **Generations**: 300
- **Islands**: 2 (10% migration)
- **Archive**: 40 best programs
- **LLMs**: GPT-5, Claude, Gemini (dynamic selection)
- **Patches**: 60% diff, 30% full, 10% cross

## Verification

Run the test suite:
```bash
bash test_integration.sh
```

Expected: **All tests passed! ✅**

## Documentation

- 📖 **Full Guide**: `README.md` (comprehensive)
- 🔧 **Integration**: `INTEGRATION_SUMMARY.md` (technical)
- 🚀 **This Guide**: `QUICKSTART.md` (you are here)

## Help

**Program doesn't run?**
- Check you're in `examples/prism/` directory
- Verify Python environment has Shinka installed

**Evaluation fails?**
- Check `openevolve_examples/prism/evaluator.py` exists
- Run test suite: `bash test_integration.sh`

**Evolution issues?**
- Check API keys for LLMs (GPT-5, Claude, Gemini)
- Try with fewer generations first
- Check `results_prism/` for logs

## Example Output

### Initial Program
```
$ python initial.py
Max KVPR: 20.892
```

### Evaluation
```
$ python evaluate.py --program_path initial.py --results_dir results
✓ Evaluation completed successfully

Metrics:
  combined_score: 21.892
  max_kvpr: 20.892
  success_rate: 1.000
  execution_time: 0.000s
```

### Evolution (after some generations)
```
Generation 10: Best score 22.5 (+0.6)
Generation 20: Best score 23.1 (+0.6)
...
```

## What Gets Evolved?

The code between these markers:
```python
# EVOLVE-BLOCK-START
def compute_model_placement(gpu_num, models):
    # This function gets evolved by LLMs
    ...
# EVOLVE-BLOCK-END
```

Everything else stays fixed!

## Quick Commands

```bash
# Test
python initial.py

# Evaluate
python evaluate.py --program_path initial.py

# Evolve
python run_evo.py

# Verify
bash test_integration.sh

# Check results
ls -lh results_prism/
```

## Related Examples

- `examples/txn_scheduling/` - Similar discrete optimization
- `examples/telemetry_repair/` - Similar evaluation pattern

## Status

✅ **Ready to use!**

Last verified: November 25, 2025

