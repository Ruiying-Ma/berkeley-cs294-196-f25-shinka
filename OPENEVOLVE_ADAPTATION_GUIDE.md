# OpenEvolve to Shinka Adaptation Guide

This guide documents how OpenEvolve examples are adapted to work with the ShinkaEvolve framework, including all critical bug fixes and best practices learned during the integration process.

## Table of Contents

1. [Overview](#overview)
2. [Directory Structure](#directory-structure)
3. [Core Adaptation Pattern](#core-adaptation-pattern)
4. [Configuration Files](#configuration-files)
5. [Critical Bug Fixes](#critical-bug-fixes)
6. [Example-by-Example Breakdown](#example-by-example-breakdown)
7. [Best Practices](#best-practices)
8. [Common Pitfalls to Avoid](#common-pitfalls-to-avoid)

---

## Overview

**OpenEvolve** examples are adapted to work with **ShinkaEvolve** by creating a parallel structure that maintains the original examples while adding Shinka-specific integration files. The adaptation involves:

- **Preserving Original Code**: OpenEvolve examples remain in `openevolve_examples/` directory unchanged (as reference material)
- **Creating Shinka Versions**: New Shinka-compatible versions in `examples/` directory
- **Configuration Integration**: Hydra config files in `configs/task/` and `configs/variant/`
- **Bug Fixes**: Critical path resolution and data file fixes applied during integration

### Key Design Principle

The adaptation maintains **separation of concerns**:
- Original OpenEvolve examples stay in `openevolve_examples/` for reference and shared utilities
- Shinka-compatible versions live in `examples/` with proper integration points
- Configuration-driven execution via Hydra for flexibility

---

## Directory Structure

### High-Level Organization

```
ShinkaEvolve/
├── openevolve_examples/          # Original OpenEvolve examples (reference)
│   ├── eplb/
│   │   ├── initial_program.py    # Original implementation
│   │   ├── evaluator.py          # Original evaluator
│   │   ├── config.yaml           # OpenEvolve config
│   │   ├── expert-load.json      # Data file (223MB)
│   │   └── README.md
│   ├── txn_scheduling/
│   │   ├── initial_program.py
│   │   ├── evaluator.py
│   │   ├── txn_simulator.py      # Shared utility
│   │   ├── workloads.py          # Shared utility
│   │   └── config_openai.yaml
│   ├── llm_sql/
│   │   └── best_shinka_program.py  # Result storage
│   └── circle_packing/
│       ├── initial_program.py
│       └── evaluator.py
│
├── examples/                      # Shinka-adapted versions
│   ├── eplb/
│   │   ├── initial.py            # Adapted with EVOLVE-BLOCK
│   │   ├── evaluate.py           # Shinka evaluation wrapper
│   │   ├── run_evo.py            # Local evolution runner
│   │   └── README.md             # Shinka-specific docs
│   ├── txn_scheduling/
│   │   ├── initial.py            # Adapted with path fixes
│   │   ├── evaluate.py
│   │   ├── run_evo.py
│   │   └── README.md
│   ├── llm_sql/
│   │   ├── initial.py
│   │   ├── evaluate.py
│   │   ├── run_evo.py
│   │   ├── README.md
│   │   ├── solver.py             # Base classes (not in openevolve_examples)
│   │   ├── utils.py              # Utilities (not in openevolve_examples)
│   │   └── datasets/             # CSV data files
│   └── circle_packing/
│       ├── initial.py
│       ├── evaluate.py
│       └── run_evo.py
│
└── configs/                       # Hydra configuration
    ├── task/                      # Task-specific configs
    │   ├── eplb.yaml
    │   ├── txn_scheduling.yaml
    │   ├── llm_sql.yaml
    │   └── circle_packing.yaml
    └── variant/                   # Variant combinations
        ├── eplb_example.yaml
        ├── txn_scheduling_example.yaml
        ├── llm_sql_example.yaml
        └── circle_packing_example.yaml
```

### File Type Mapping

| OpenEvolve File | Shinka File | Purpose |
|----------------|-------------|---------|
| `initial_program.py` | `initial.py` | Starting algorithm with EVOLVE-BLOCK markers |
| `evaluator.py` | `evaluate.py` | Evaluation using `shinka.core.run_shinka_eval` |
| `config.yaml` | `configs/task/*.yaml` | Task configuration for Hydra |
| N/A | `run_evo.py` | Local evolution runner (optional) |
| N/A | `configs/variant/*.yaml` | Variant configuration combining database/evolution/task/cluster |

---

## Core Adaptation Pattern

### Step-by-Step Adaptation Process

#### 1. Create Shinka-Compatible `initial.py`

Take the OpenEvolve `initial_program.py` and adapt it:

**Key Changes:**
- Add `EVOLVE-BLOCK-START` and `EVOLVE-BLOCK-END` markers around the code to evolve
- Implement robust path resolution for imports (see [Bug Fixes](#critical-bug-fixes))
- Define an entry point function that the evaluator can call
- Preserve input immutability (use `.copy()` when modifying DataFrames/tensors)

**Example Structure:**

```python
# EVOLVE-BLOCK-START
"""
Module docstring explaining the algorithm
"""

import required_libraries

# Robust path finding function (if needed for external dependencies)
def find_dependencies_dir(start_path):
    current = os.path.abspath(start_path)
    while current != os.path.dirname(current):
        if os.path.exists(os.path.join(current, 'openevolve_examples', 'task_name')):
            return os.path.join(current, 'openevolve_examples', 'task_name')
        current = os.path.dirname(current)
    raise RuntimeError("Could not find dependencies")

# If external dependencies needed, add to path
if 'external_module' in sys.modules:
    pass  # Already imported
else:
    deps_dir = find_dependencies_dir(os.path.dirname(__file__))
    sys.path.insert(0, deps_dir)

# Algorithm implementation
def algorithm_function():
    """Main algorithm logic"""
    pass

# Entry point for evaluation
def run_algorithm():
    """Entry point that evaluator will call"""
    return algorithm_function()

# EVOLVE-BLOCK-END

if __name__ == "__main__":
    # Test code
    result = run_algorithm()
    print(f"Result: {result}")
```

#### 2. Create Shinka-Compatible `evaluate.py`

Wrap the evaluation logic using Shinka's evaluation framework:

```python
import os
import sys
from shinka.core import run_shinka_eval

def evaluate_program(program_path: str):
    """
    Evaluate a program and return metrics.
    
    Args:
        program_path: Path to the program file to evaluate
        
    Returns:
        dict: Evaluation results with 'combined_score' and optional 'private' metrics
    """
    # Import the program
    spec = importlib.util.spec_from_file_location("evolved_program", program_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    # Get the algorithm entry point
    run_fn = getattr(module, 'run_algorithm')  # Or appropriate function name
    
    # Run evaluation on test cases
    scores = []
    for test_case in get_test_cases():
        result = run_fn(test_case)
        score = compute_score(result, test_case)
        scores.append(score)
    
    # Aggregate metrics
    combined_score = aggregate_scores(scores)
    
    return {
        'combined_score': combined_score,
        'private': {  # Optional: additional metrics not visible to LLM
            'individual_scores': scores,
            'metadata': 'any extra info'
        }
    }

def main(program_path: str, results_dir: str):
    """Main evaluation entry point called by Shinka."""
    return run_shinka_eval(
        evaluate_fn=evaluate_program,
        program_path=program_path,
        results_dir=results_dir
    )

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--program_path", required=True)
    parser.add_argument("--results_dir", required=True)
    args = parser.parse_args()
    main(args.program_path, args.results_dir)
```

#### 3. Create Task Configuration (`configs/task/<task_name>.yaml`)

Define task-specific settings:

```yaml
evaluate_function:
  _target_: examples.<task_name>.evaluate.main
  program_path: ???  # Will be filled by Shinka
  results_dir: ???   # Will be filled by Shinka

distributed_job_config:
  _target_: shinka.launch.SlurmCondaJobConfig
  modules:
  - "cuda/12.4"  # If GPU needed
  eval_program_path: "shinka/eval_hydra.py"
  conda_env: "shinka"
  time: "00:20:00"
  cpus: 4
  gpus: 0  # Or 1 if GPU needed
  mem: "32G"

evo_config:
  task_sys_msg: |
    Detailed prompt for the LLM explaining:
    - The optimization goal
    - Scoring formula
    - Key directions to explore
    - Important constraints
    - Algorithm details
  
  language: "python"
  init_program_path: "examples/<task_name>/initial.py"
  job_type: "slurm_conda"  # or "local"
  
  llm_models:
    - "gpt-5"
    - "gpt-5-mini"
    - "claude-sonnet-4"
  
  llm_dynamic_selection: ucb
  llm_kwargs:
    temperatures: [0.0, 0.5, 1.0]
    max_tokens: 16384
  
  meta_rec_interval: 10
  meta_llm_models: ["gpt-5-nano"]
  embedding_model: "text-embedding-3-small"

exp_name: "shinka_<task_name>"
```

#### 4. Create Variant Configuration (`configs/variant/<task_name>_example.yaml`)

Combine configuration components:

```yaml
defaults:
  - override /database@_global_: island_large
  - override /evolution@_global_: large_budget
  - override /task@_global_: <task_name>
  - override /cluster@_global_: local
  - _self_

variant_suffix: "_example"
```

#### 5. Create `run_evo.py` (Optional)

Local evolution runner for testing:

```python
import sys
from pathlib import Path

# Add shinka to path
repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(repo_root))

from shinka.launch_local import launch_evo

if __name__ == "__main__":
    config = {
        "database": "island_large",
        "evolution": "large_budget",
        "task": "<task_name>",
        "cluster": "local",
    }
    
    launch_evo(config)
```

#### 6. Create README.md

Document the adapted example:
- Algorithm overview
- Setup instructions (dependencies, data files)
- Usage examples (evaluation, evolution)
- Performance metrics
- Known issues and fixes

---

## Critical Bug Fixes

These bug fixes are **ESSENTIAL** for successful adaptation. All three issues were discovered during integration and must be avoided in new adaptations.

### Fix #1: Robust Path Resolution for Imports ⚠️ CRITICAL

**Problem:** Evolved programs are copied to arbitrary result directories (e.g., `results/shinka_task/gen_X/main.py`), causing relative import paths to break.

**Example of Broken Code:**

```python
# ❌ WRONG: Breaks when file is moved
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'openevolve_examples', 'task'))
```

**Error:**
```
ModuleNotFoundError: No module named 'dependency_module'
```

**Solution:** Implement upward-searching path resolution:

```python
# ✅ CORRECT: Works from any location
def find_repo_root(start_path):
    """Find the repository root by searching upward for openevolve_examples."""
    current = os.path.abspath(start_path)
    while current != os.path.dirname(current):  # Stop at filesystem root
        if os.path.exists(os.path.join(current, 'openevolve_examples', 'task_name')):
            return current
        current = os.path.dirname(current)
    raise RuntimeError("Could not find openevolve_examples/task_name directory")

repo_root = find_repo_root(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(repo_root, 'openevolve_examples', 'task_name'))
```

**Impact:** Without this fix, **100% of evolved programs will fail** (as seen in txn_scheduling: 0/104 programs worked).

**Examples Fixed:**
- ✅ `txn_scheduling` - Fixed in `initial.py`
- ✅ `llm_sql` - Fixed in `initial.py` with `find_llm_sql_dir()`

---

### Fix #2: Absolute Paths for Data Files ⚠️ CRITICAL

**Problem:** Relative paths to data files fail when the evaluation is run from different directories.

**Example of Broken Code:**

```python
# ❌ WRONG: Depends on current working directory
WORKLOAD_PATH = "expert-load.json"
DATASET_DIR = "datasets"
```

**Error:**
```
FileNotFoundError: [Errno 2] No such file or directory: 'expert-load.json'
```

**Solution:** Use paths relative to the module file location:

```python
# ✅ CORRECT: Works from any execution directory
WORKLOAD_PATH = os.path.join(os.path.dirname(__file__), "expert-load.json")
DATASET_DIR = os.path.join(os.path.dirname(__file__), "datasets")

# For dependencies in openevolve_examples:
task_dir = find_task_dir(os.path.dirname(__file__))
DATA_PATH = os.path.join(task_dir, "data", "file.json")
```

**Impact:** Without this fix, evaluations **silently fall back to dummy data** or crash with file not found errors.

**Examples Fixed:**
- ✅ `eplb` - Fixed in `openevolve_examples/eplb/evaluator.py`
- ✅ `llm_sql` - Fixed in `examples/llm_sql/evaluate.py`

---

### Fix #3: Missing Large Data Files ⚠️ IMPORTANT

**Problem:** Large data files (>100MB) are not committed to git and must be downloaded separately.

**Example:** EPLB workload data was missing, causing all programs to receive identical dummy scores (0.750), making evolution meaningless.

**Symptoms:**
- All programs get the same score
- No variation across generations
- Evolution shows zero improvement
- Silent fallback to placeholder values

**Solution:**

1. **Document data download in README:**

```markdown
## Setup

### Download Required Data Files

```bash
cd openevolve_examples/eplb
curl -L -o expert-load.json "https://huggingface.co/datasets/abmfy/eplb-openevolve/resolve/main/expert-load.json"
```
```

2. **Validate data file existence in evaluator:**

```python
if not os.path.exists(WORKLOAD_PATH):
    raise FileNotFoundError(
        f"Required data file not found: {WORKLOAD_PATH}\n"
        f"Please download it from: <URL>\n"
        f"See README.md for setup instructions."
    )
```

3. **Fail loudly, not silently:**

```python
# ❌ WRONG: Silent fallback to dummy data
if os.path.exists(workload_file):
    data = load_data(workload_file)
else:
    data = dummy_data()  # LLM has no way to know this is fake!

# ✅ CORRECT: Raise error if data missing
if not os.path.exists(workload_file):
    raise FileNotFoundError(f"Required file {workload_file} not found. See README for setup.")
data = load_data(workload_file)
```

**Examples Fixed:**
- ✅ `eplb` - Downloaded 223MB `expert-load.json` file
- ✅ `llm_sql` - Verified all 5 CSV datasets present (total ~66MB)

**Impact:** Without real data, evolution is meaningless—all scores are identical.

---

### Fix #4: Input Mutation Prevention

**Problem:** Modifying input DataFrames/tensors can cause inconsistent evaluation results.

**Example of Broken Code:**

```python
# ❌ WRONG: Modifies the input DataFrame
def process_data(df):
    df['new_column'] = df['old_column'] * 2  # Mutates input!
    return df
```

**Solution:** Always copy inputs before modification:

```python
# ✅ CORRECT: Preserves input immutability
def process_data(df):
    df = df.copy()  # Create a copy
    df['new_column'] = df['old_column'] * 2
    return df
```

**Examples Fixed:**
- ✅ `llm_sql` - Added `df = df.copy()` in initial algorithm

---

### Fix #5: Unnecessary Dependencies

**Problem:** Importing unused heavy dependencies slows down evaluation and may cause import errors.

**Solution:** Remove unused imports and dependencies.

**Examples Fixed:**
- ✅ `llm_sql` - Removed unused `pyspark` import from `utils.py`

---

## Configuration Files

### Task Configuration Pattern

Each task needs a `configs/task/<task_name>.yaml` file with three main sections:

#### 1. Evaluate Function

```yaml
evaluate_function:
  _target_: examples.<task_name>.evaluate.main
  program_path: ???  # Filled by Shinka
  results_dir: ???   # Filled by Shinka
```

#### 2. Distributed Job Config (for SLURM)

```yaml
distributed_job_config:
  _target_: shinka.launch.SlurmCondaJobConfig
  modules:
  - "cuda/12.4"  # Only if GPU needed
  eval_program_path: "shinka/eval_hydra.py"
  conda_env: "shinka"
  time: "00:20:00"  # Adjust based on evaluation time
  cpus: 4
  gpus: 0  # Set to 1 if GPU needed
  mem: "32G"  # Adjust based on requirements
```

#### 3. Evolution Config

```yaml
evo_config:
  task_sys_msg: |
    Detailed system message for the LLM explaining:
    - What the algorithm does
    - Optimization objectives (be specific!)
    - Scoring formula (with weights)
    - Constraints and requirements
    - Directions to explore
    - Common pitfalls to avoid
  
  language: "python"
  init_program_path: "examples/<task_name>/initial.py"
  job_type: "slurm_conda"  # or "local"
  
  # LLM Configuration
  llm_models:
    - "gpt-5"
    - "gpt-5-mini"
    - "claude-sonnet-4"
  llm_dynamic_selection: ucb  # UCB1 for dynamic model selection
  llm_kwargs:
    temperatures: [0.0, 0.5, 1.0]  # Explore creativity levels
    max_tokens: 16384
  
  # Meta-recommendations
  meta_rec_interval: 10  # Every 10 generations
  meta_llm_models: ["gpt-5-nano"]
  meta_llm_kwargs:
    temperatures: [0.0]
  
  # Embeddings for similarity detection
  embedding_model: "text-embedding-3-small"

exp_name: "shinka_<task_name>"
```

### Variant Configuration Pattern

Variants combine database, evolution, task, and cluster configs:

```yaml
defaults:
  - override /database@_global_: island_large  # or island_small, single, etc.
  - override /evolution@_global_: large_budget  # or medium_budget, small_budget
  - override /task@_global_: <task_name>
  - override /cluster@_global_: local  # or slurm
  - _self_

variant_suffix: "_example"  # Appended to exp_name
```

**Common Combinations:**

| Variant Type | Database | Evolution | Use Case |
|--------------|----------|-----------|----------|
| Quick Test | `single` | `small_budget` | Fast iteration, debugging |
| Medium Run | `island_small` | `medium_budget` | Initial exploration |
| Full Evolution | `island_large` | `large_budget` | Production runs |

---

## Example-by-Example Breakdown

### 1. EPLB (Expert Parallelism Load Balancer)

**Status:** ✅ Fully Adapted and Fixed

**Description:** Optimizes expert assignment in MoE models to balance GPU workloads.

**Files Created:**
- `examples/eplb/initial.py` - Algorithm with EVOLVE-BLOCK (248 lines)
- `examples/eplb/evaluate.py` - Evaluation wrapper (292 lines)
- `examples/eplb/run_evo.py` - Local evolution runner (144 lines)
- `examples/eplb/README.md` - Documentation (146 lines)
- `configs/task/eplb.yaml` - Task configuration
- `configs/variant/eplb_example.yaml` - Variant configuration

**Critical Fixes Applied:**

1. **Data File Fix** (Fix #3)
   - Downloaded 223MB `expert-load.json` from HuggingFace
   - Fixed path in `openevolve_examples/eplb/evaluator.py`:
     ```python
     # Before: WORKLOAD_PATH = "expert-load.json"
     # After: WORKLOAD_PATH = os.path.join(os.path.dirname(__file__), "expert-load.json")
     ```
   - **Impact:** Before fix, all programs scored 0.750 (dummy data). After fix, scores range from ~0.31 to 1.0 with real variation.

**Dependencies:**
- PyTorch (for tensor operations)

**Data Requirements:**
- `openevolve_examples/eplb/expert-load.json` (223MB) - **Must download separately**

**Performance:**
- Baseline score: ~0.656 (balancedness: 0.311, speed: 1.0)
- Target: 1.0 (perfect balancedness)

**Usage:**
```bash
# Download data first
cd openevolve_examples/eplb
curl -L -o expert-load.json "https://huggingface.co/datasets/abmfy/eplb-openevolve/resolve/main/expert-load.json"

# Run evolution
cd /Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve
python -m shinka.launch_hydra variant=eplb_example
```

---

### 2. Transaction Scheduling (txn_scheduling)

**Status:** ✅ Fully Adapted and Fixed

**Description:** Optimizes database transaction scheduling to minimize conflicts and maximize throughput.

**Files Created:**
- `examples/txn_scheduling/initial.py` - Algorithm with path fixes
- `examples/txn_scheduling/evaluate.py` - Evaluation wrapper
- `examples/txn_scheduling/run_evo.py` - Local runner
- `examples/txn_scheduling/README.md` - Documentation
- `configs/task/txn_scheduling.yaml` - Task configuration (67 lines)
- `configs/variant/txn_scheduling_example.yaml` - Variant configuration

**Critical Fixes Applied:**

1. **Path Resolution Fix** (Fix #1)
   - Added robust path-finding function:
     ```python
     def find_repo_root(start_path):
         """Find the repository root by looking for openevolve_examples directory."""
         current = os.path.abspath(start_path)
         while current != os.path.dirname(current):
             if os.path.exists(os.path.join(current, 'openevolve_examples', 'txn_scheduling')):
                 return current
             current = os.path.dirname(current)
         raise RuntimeError("Could not find openevolve_examples directory")
     
     repo_root = find_repo_root(os.path.dirname(__file__))
     sys.path.insert(0, os.path.join(repo_root, 'openevolve_examples', 'txn_scheduling'))
     ```
   - **Impact:** Before fix, 0/104 programs worked (100% failure rate). After fix, all programs run successfully.

**Dependencies:**
- Standard library only

**Data Requirements:**
- Workload definitions in `openevolve_examples/txn_scheduling/workloads.py` (included)
- Transaction simulator in `openevolve_examples/txn_scheduling/txn_simulator.py` (included)

**Performance:**
- Baseline score: ~2.6
- Lower is better (minimizes conflicts)

**Usage:**
```bash
cd /Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve
python -m shinka.launch_hydra variant=txn_scheduling_example
```

---

### 3. LLM SQL Optimization (llm_sql)

**Status:** ✅ Fully Adapted and Fixed

**Description:** Optimizes SQL DataFrame row ordering to maximize prefix hits for LLM caching.

**Files Created:**
- `examples/llm_sql/initial.py` - GGR algorithm with EVOLVE-BLOCK (435 lines)
- `examples/llm_sql/evaluate.py` - Evaluation on 5 datasets (225 lines)
- `examples/llm_sql/run_evo.py` - Local evolution runner (163 lines)
- `examples/llm_sql/README.md` - Comprehensive documentation (301 lines)
- `configs/task/llm_sql.yaml` - Task configuration
- `configs/variant/llm_sql_example.yaml` - Variant configuration

**Critical Fixes Applied:**

1. **Path Resolution Fix** (Fix #1)
   - Implemented `find_llm_sql_dir()` function
   - Works from any execution directory

2. **Data File Path Fix** (Fix #2)
   - Used absolute paths for datasets:
     ```python
     llm_sql_dir = find_llm_sql_dir(os.path.dirname(__file__))
     DATASET_DIR = os.path.join(llm_sql_dir, 'datasets')
     TEST_FILES = [
         os.path.join(DATASET_DIR, "movies.csv"),
         os.path.join(DATASET_DIR, "beer.csv"),
         # ...
     ]
     ```

3. **Input Mutation Fix** (Fix #4)
   - Added `df = df.copy()` to prevent input modification

4. **Dependency Fix** (Fix #5)
   - Removed unused `pyspark` import from `utils.py`

**Dependencies:**
- pandas
- numpy

**Data Requirements:**
- 5 CSV datasets in `examples/llm_sql/datasets/` (total ~66MB):
  - `movies.csv` (9.0 MB)
  - `beer.csv` (2.4 MB)
  - `BIRD.csv` (32.4 MB)
  - `PDMX.csv` (7.1 MB)
  - `products.csv` (15.4 MB)

**Performance:**
- Baseline hit rate: ~40-70% (varies by dataset)
- Target: 60-80% hit rate
- Combined score: Hit rate (95%) + Runtime score (5%)

**Usage:**
```bash
# All datasets already present in repo
cd /Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve
python -m shinka.launch_hydra variant=llm_sql_example
```

---

### 4. Circle Packing (circle_packing)

**Status:** ✅ Adapted (No fixes needed)

**Description:** Packs circles efficiently into a bounded space.

**Files Created:**
- `examples/circle_packing/initial.py`
- `examples/circle_packing/evaluate.py`
- `examples/circle_packing/run_evo.py`
- `configs/task/circle_packing.yaml`
- `configs/variant/circle_packing_example.yaml`

**No Critical Issues:** This example is self-contained with no external dependencies.

**Dependencies:**
- Standard library only

**Usage:**
```bash
cd /Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve
python -m shinka.launch_hydra variant=circle_packing_example
```

---

## Summary Table: All Examples

| Example | Status | Critical Fixes | Data Files | Dependencies |
|---------|--------|---------------|------------|--------------|
| **EPLB** | ✅ Fixed | Data file path + 223MB download | `expert-load.json` (223MB) | PyTorch |
| **txn_scheduling** | ✅ Fixed | Path resolution for imports | Included in repo | Standard lib |
| **llm_sql** | ✅ Fixed | Path resolution + input mutation + unused deps | 5 CSVs (~66MB, included) | pandas, numpy |
| **circle_packing** | ✅ Working | None needed | None | Standard lib |

---

## Best Practices

### 1. Path Resolution Strategy

**Always use upward-searching path resolution for external dependencies:**

```python
def find_dependency_dir(start_path, target_marker):
    """
    Generic function to find a directory by searching upward.
    
    Args:
        start_path: Starting location (usually __file__)
        target_marker: Unique file or directory that identifies the target
                      (e.g., 'openevolve_examples/task_name')
    """
    current = os.path.abspath(start_path)
    while current != os.path.dirname(current):
        target_path = os.path.join(current, target_marker)
        if os.path.exists(target_path):
            return target_path
        current = os.path.dirname(current)
    raise RuntimeError(f"Could not find {target_marker}")
```

**Why this works:**
- Works from `examples/task/initial.py`
- Works from `results/shinka_task/gen_X/main.py`
- Works from any arbitrary directory
- Fails clearly with meaningful error message

---

### 2. Data File Access Strategy

**Always use module-relative paths:**

```python
# Get the directory containing this module
module_dir = os.path.dirname(os.path.abspath(__file__))

# For data files in the same directory
DATA_FILE = os.path.join(module_dir, "data.json")

# For data files in openevolve_examples
task_dir = find_dependency_dir(module_dir, "openevolve_examples/task_name")
DATA_FILE = os.path.join(task_dir, "data.json")
```

**Validate data file existence:**

```python
if not os.path.exists(DATA_FILE):
    raise FileNotFoundError(
        f"Required data file not found: {DATA_FILE}\n"
        f"Download it from: {DOWNLOAD_URL}\n"
        f"See README.md for setup instructions."
    )
```

---

### 3. Testing Strategy

**Test your adaptation thoroughly before running evolution:**

1. **Test standalone execution:**
   ```bash
   cd examples/task_name
   python initial.py
   ```

2. **Test from repository root:**
   ```bash
   cd /path/to/ShinkaEvolve
   python examples/task_name/initial.py
   ```

3. **Test from arbitrary directory:**
   ```bash
   cd /tmp
   python /path/to/ShinkaEvolve/examples/task_name/initial.py
   ```

4. **Test evaluation:**
   ```bash
   cd examples/task_name
   python evaluate.py --program_path initial.py --results_dir test_results
   ```

5. **Simulate evolved program location:**
   ```bash
   mkdir -p /tmp/test_gen_0
   cp examples/task_name/initial.py /tmp/test_gen_0/main.py
   cd /tmp/test_gen_0
   python main.py  # Should still work!
   ```

---

### 4. Evaluation Function Design

**Return meaningful metrics:**

```python
def evaluate_program(program_path: str):
    # Run multiple test cases
    results = []
    for test_case in test_cases:
        result = run_test(test_case)
        results.append(result)
    
    # Aggregate into combined_score
    combined_score = aggregate(results)
    
    return {
        'combined_score': combined_score,  # Required: visible to LLM
        'private': {  # Optional: hidden from LLM for analysis
            'individual_scores': results,
            'mean': np.mean(results),
            'std': np.std(results),
            'min': min(results),
            'max': max(results),
        }
    }
```

**Make scores interpretable:**
- Normalize to [0, 1] range when possible
- Higher is better (consistent direction)
- Document the scoring formula in task_sys_msg
- Include component weights if multi-objective

---

### 5. Task System Message Design

**Be specific and actionable:**

✅ **Good Example:**

```yaml
task_sys_msg: |
  Optimize the load balancing algorithm for MoE models.
  
  SCORING (50% each):
  - Balancedness: average_load / max_load (0-1, higher better)
  - Speed: 0.02 / runtime_seconds (higher better)
  - Combined = 0.5 * balancedness + 0.5 * speed
  
  KEY OPPORTUNITIES:
  1. Better bin-packing heuristics (try LPT/FFD variants)
  2. Reduce sorting overhead (currently O(n log n))
  3. Vectorize operations where possible
  4. Consider approximation algorithms
  
  CONSTRAINTS:
  - Must return valid mappings (no gaps in indices)
  - Must respect num_replicas, num_groups, num_nodes
  - Output shape must be [num_layers, num_logical_experts]
```

❌ **Bad Example:**

```yaml
task_sys_msg: |
  Make it better and faster.
```

---

### 6. EVOLVE-BLOCK Placement

**Include the right amount of code:**

✅ **Good:** Include the full algorithm but exclude:
- Import statements that need to be outside the block
- Test code
- Main entry point boilerplate

❌ **Bad:** 
- Including too little (missing helper functions)
- Including too much (test code, main block)
- Splitting across multiple blocks

**Example:**

```python
import torch  # Outside EVOLVE-BLOCK

# EVOLVE-BLOCK-START
"""Algorithm docstring"""

def helper_function():
    """Include all helpers needed by the algorithm"""
    pass

def main_algorithm():
    """Main algorithm logic"""
    helper_function()
    return result

# EVOLVE-BLOCK-END

if __name__ == "__main__":  # Outside EVOLVE-BLOCK
    # Test code
    result = main_algorithm()
    print(result)
```

---

### 7. Dependency Management

**Minimize dependencies:**
- Only include what's actually used
- Document all dependencies in README
- Test with fresh environment
- Remove unused imports

**Document installation:**

```markdown
## Dependencies

Install required packages:

```bash
pip install torch pandas numpy
```

Or use requirements.txt:

```bash
pip install -r requirements.txt
```
```

---

### 8. Documentation Standards

**Every adapted example should have a README with:**

1. **Overview:** What the algorithm does
2. **Setup:** Dependencies, data files, installation steps
3. **Usage:** How to evaluate, how to run evolution
4. **Algorithm Details:** Key functions, approach, constraints
5. **Evaluation Metrics:** What's being measured, scoring formula
6. **Performance Targets:** Baseline scores, target scores
7. **Troubleshooting:** Common issues and solutions
8. **References:** Links to papers, original implementations

---

## Common Pitfalls to Avoid

### ❌ Pitfall #1: Relative Path Dependencies

**Problem:**
```python
sys.path.insert(0, "../../openevolve_examples/task")  # WRONG!
```

**Why it fails:** Path breaks when file is moved during evolution.

**Solution:** Use upward-searching path resolution (see Fix #1).

---

### ❌ Pitfall #2: Current Working Directory Assumptions

**Problem:**
```python
data = pd.read_csv("datasets/data.csv")  # WRONG!
```

**Why it fails:** Assumes script is run from specific directory.

**Solution:** Use `os.path.dirname(__file__)` for module-relative paths.

---

### ❌ Pitfall #3: Silent Fallbacks to Dummy Data

**Problem:**
```python
if os.path.exists("data.json"):
    data = load_real_data()
else:
    data = dummy_data()  # WRONG! LLM has no idea this is fake!
```

**Why it fails:** Evolution runs but produces meaningless results.

**Solution:** Fail loudly with clear error messages.

---

### ❌ Pitfall #4: Input Mutation

**Problem:**
```python
def process(df):
    df['new_col'] = df['old_col'] * 2  # WRONG! Mutates input
    return df
```

**Why it fails:** Inconsistent evaluation results across multiple runs.

**Solution:** Always copy inputs before modification.

---

### ❌ Pitfall #5: Insufficient Testing

**Problem:** Only testing from one location (e.g., `examples/task/`).

**Why it fails:** Doesn't catch path issues that appear in evolved programs.

**Solution:** Test from multiple locations (see Testing Strategy).

---

### ❌ Pitfall #6: Vague Task Messages

**Problem:**
```yaml
task_sys_msg: "Optimize this algorithm."
```

**Why it fails:** LLM doesn't know what to optimize or how it's scored.

**Solution:** Be specific about objectives, scoring, and opportunities.

---

### ❌ Pitfall #7: Missing Data Validation

**Problem:** Not checking if required data files exist.

**Why it fails:** Silent failures or cryptic errors during evaluation.

**Solution:** Validate file existence with clear error messages.

---

### ❌ Pitfall #8: Over-Engineering the Initial Program

**Problem:** Starting with an overly complex algorithm.

**Why it fails:** LLM struggles to understand and modify complex code.

**Solution:** Start with a simple, well-documented baseline that works.

---

## Checklist for New Adaptations

Use this checklist when adapting a new OpenEvolve example:

### Pre-Adaptation
- [ ] Read through original OpenEvolve example
- [ ] Identify external dependencies
- [ ] Check for large data files
- [ ] Understand the evaluation metrics
- [ ] Review the algorithm structure

### File Creation
- [ ] Create `examples/task_name/initial.py` with EVOLVE-BLOCK
- [ ] Create `examples/task_name/evaluate.py` with shinka integration
- [ ] Create `examples/task_name/run_evo.py` (optional)
- [ ] Create `examples/task_name/README.md` with documentation
- [ ] Create `configs/task/task_name.yaml` with task config
- [ ] Create `configs/variant/task_name_example.yaml` with variant config

### Critical Fixes
- [ ] Implement robust path resolution for imports
- [ ] Use absolute paths for data files
- [ ] Add data file existence validation
- [ ] Prevent input mutation (use `.copy()`)
- [ ] Remove unused dependencies
- [ ] Download/verify all required data files

### Testing
- [ ] Test `initial.py` from `examples/task_name/`
- [ ] Test from repository root
- [ ] Test from arbitrary directory (`/tmp`)
- [ ] Test evaluation pipeline
- [ ] Simulate evolved program location
- [ ] Verify all metrics are calculated correctly
- [ ] Check that scores vary appropriately

### Documentation
- [ ] Document all dependencies in README
- [ ] Include setup instructions for data files
- [ ] Explain evaluation metrics and scoring
- [ ] Provide usage examples
- [ ] Document known issues and fixes
- [ ] Add references to original work

### Final Checks
- [ ] No hardcoded absolute paths
- [ ] No relative paths that assume working directory
- [ ] All imports resolve correctly
- [ ] All data files accessible
- [ ] Evaluation produces varying scores
- [ ] README is comprehensive
- [ ] Configuration files are correct

---

## Quick Start: Adapting a New Example

1. **Copy the structure from an existing example:**
   ```bash
   cp -r examples/eplb examples/new_task
   ```

2. **Replace the algorithm logic in `initial.py`:**
   - Add your algorithm inside EVOLVE-BLOCK
   - Implement robust path resolution if needed
   - Add entry point function

3. **Update `evaluate.py`:**
   - Replace evaluation logic
   - Ensure it returns `combined_score` and optional `private` metrics
   - Add data file validation

4. **Create config files:**
   - Copy and modify `configs/task/eplb.yaml` → `configs/task/new_task.yaml`
   - Copy and modify `configs/variant/eplb_example.yaml` → `configs/variant/new_task_example.yaml`

5. **Test thoroughly:**
   ```bash
   # Test standalone
   python examples/new_task/initial.py
   
   # Test evaluation
   python examples/new_task/evaluate.py --program_path examples/new_task/initial.py --results_dir test_results
   
   # Test evolution (small run)
   python -m shinka.launch_hydra variant=new_task_example evolution=small_budget
   ```

6. **Write documentation:**
   - Update `examples/new_task/README.md`
   - Document dependencies, setup, usage, metrics

---

## Troubleshooting

### Issue: "ModuleNotFoundError" when running evolved programs

**Cause:** Path resolution failing in evolved program locations.

**Solution:** Implement upward-searching path resolution (Fix #1).

---

### Issue: All programs getting the same score

**Causes:**
1. Missing data files → falling back to dummy data
2. Evaluation not varying with algorithm changes
3. Bugs in scoring logic

**Solutions:**
1. Verify data files exist with absolute paths
2. Test evaluation with known good/bad algorithms
3. Add debug logging to see intermediate scores

---

### Issue: "FileNotFoundError" for data files

**Cause:** Relative paths failing when run from different directories.

**Solution:** Use module-relative paths with `os.path.dirname(__file__)` (Fix #2).

---

### Issue: Evolution runs but shows no improvement

**Possible Causes:**
1. Dummy data being used (all scores identical)
2. Scoring metric doesn't reflect algorithm quality
3. LLM doesn't understand the task
4. Initial program already optimal

**Solutions:**
1. Verify real data is being loaded
2. Review scoring formula and test with variations
3. Improve task_sys_msg with specific guidance
4. Check if there's room for improvement

---

## Additional Resources

### Related Documentation
- `DEBUG_FIXES_SUMMARY.md` - Overview of all fixes applied
- `EPLB_FIX.md` - Detailed EPLB debugging story
- `TXN_SCHEDULING_FIX.md` - Transaction scheduling fix details
- `LLM_SQL_INTEGRATION_SUMMARY.md` - LLM SQL adaptation details

### Example READMEs
- `examples/eplb/README.md` - EPLB-specific documentation
- `examples/txn_scheduling/README.md` - Transaction scheduling docs
- `examples/llm_sql/README.md` - LLM SQL comprehensive guide

### Configuration Examples
- `configs/task/` - Task-specific configurations
- `configs/variant/` - Variant combinations
- `configs/evolution/` - Evolution budget configurations
- `configs/database/` - Database structure configurations

---

## Version History

- **v1.0** (Nov 2025): Initial comprehensive guide
  - Documented all 4 adapted examples
  - Detailed all 5 critical bug fixes
  - Added best practices and checklists
  - Included troubleshooting guide

---

## Contributing

When adapting new OpenEvolve examples:

1. Follow this guide strictly
2. Test thoroughly using the checklist
3. Document any new issues discovered
4. Update this guide if new patterns emerge
5. Share lessons learned with the team

---

**Last Updated:** November 22, 2025  
**Maintainer:** ShinkaEvolve Team  
**Status:** ✅ Production Ready
