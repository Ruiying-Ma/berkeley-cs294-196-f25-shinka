# Configuration Migration Summary

## Overview
Updated all four examples (eplb, prism, telemetry_repair, txn_scheduling) to:
1. Use the shared `island_medium.yaml` and `medium_budget.yaml` configuration files instead of hardcoded configurations
2. Use OpenEvolve-compatible system messages for fair comparison between frameworks

## Changes Made

### 1. Updated Variant Configurations
Modified the following files in `configs/variant/`:
- `eplb_example.yaml` - Changed from island_large/large_budget to island_medium/medium_budget
- `telemetry_repair_example.yaml` - Changed from island_large/large_budget to island_medium/medium_budget  
- `txn_scheduling_example.yaml` - Changed from island_large/large_budget to island_medium/medium_budget
- **Created** `prism_example.yaml` - New variant file using island_medium/medium_budget

### 2. Created Task Configuration
**Created** `configs/task/prism.yaml` - New task configuration for PRISM example

### 3. Updated Example Run Scripts
Converted all four `run_evo.py` files from hardcoded configurations to Hydra-based configuration loading:

**Before:**
```python
db_config = DatabaseConfig(
    db_path="evolution_db.sqlite",
    num_islands=2,
    archive_size=40,
    # ... many hardcoded parameters
)

evo_config = EvolutionConfig(
    task_sys_msg=search_task_sys_msg,
    patch_types=["diff", "full", "cross"],
    # ... many hardcoded parameters
)
```

**After:**
```python
with initialize(version_base=None, config_path="../../configs", job_name="<example>_evolution"):
    cfg = compose(config_name="config", overrides=[
        "variant=<example>_example",
        "job_config.eval_program_path=evaluate.py"
    ])
    
job_config = hydra.utils.instantiate(cfg.job_config)
db_config = hydra.utils.instantiate(cfg.db_config)
evo_config = hydra.utils.instantiate(cfg.evo_config)
```

## Configuration Parameters

### Database Config (island_medium.yaml)
- `num_islands`: 2
- `archive_size`: 40
- `exploitation_ratio`: 0.2
- `elite_selection_ratio`: 0.3
- `num_archive_inspirations`: 4
- `num_top_k_inspirations`: 2
- `migration_interval`: 10
- `migration_rate`: 0.0
- `island_elitism`: true
- `parent_selection_strategy`: "weighted"
- `parent_selection_lambda`: 10.0

### Evolution Config (medium_budget.yaml)
- `patch_types`: ["diff", "full", "cross"]
- `patch_type_probs`: [0.6, 0.3, 0.1]
- `num_generations`: 100
- `max_parallel_jobs`: 10
- `max_patch_resamples`: 3
- `max_patch_attempts`: 3
- `llm_models`: ["gemini-2.5-pro", "gemini-2.5-flash", "gpt-4.1-mini", "gpt-4.1-nano", "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0", "o4-mini"]
- `llm_dynamic_selection`: "ucb"
- `llm_kwargs.temperatures`: [0.0, 0.5, 1.0]
- `llm_kwargs.max_tokens`: 16384
- `meta_rec_interval`: 10
- `meta_llm_models`: ["gpt-4.1"]
- `meta_llm_kwargs.temperatures`: [0.0]
- `embedding_model`: "text-embedding-3-small"

## System Message Alignment

### 4. Updated Task System Messages
Aligned all four task configurations with OpenEvolve prompts for fair comparison:

**EPLB** (`configs/task/eplb.yaml`):
- **Before**: Detailed instructions with 10 exploration directions, scoring formulas, and implementation hints
- **After**: Concise OpenEvolve prompt focusing on the two-fold goal (load balancing + efficiency)
- **Key change**: Removed framework-specific implementation details, kept core objectives

**PRISM** (`configs/task/prism.yaml`):
- **Before**: Extensive documentation with problem definition, KVPR formula, optimization directions, evaluation metrics
- **After**: Concise OpenEvolve prompt with KVPR formula and core objective
- **Key change**: Simplified from ~100 lines to ~3 lines while preserving essential information

**Telemetry Repair** (`configs/task/telemetry_repair.yaml`):
- **Before & After**: Identical (already using OpenEvolve prompt)
- **Note**: Fixed typo "indiciations" → "indiciations" (kept as-is to match OpenEvolve exactly)

**Transaction Scheduling** (`configs/task/txn_scheduling.yaml`):
- **Before**: High-level exploration directions without specific problem details
- **After**: OpenEvolve prompt with explicit input format, operation syntax, and greedy algorithm suggestion
- **Key change**: Added concrete problem specification with example transaction format

### Rationale for Alignment
1. **Fair Benchmarking**: Eliminates prompt quality as a confounding variable when comparing Shinka vs OpenEvolve
2. **Isolates Framework Differences**: Performance differences now reflect architectural choices (island model, UCB selection, patch types) rather than prompt engineering
3. **OpenEvolve-Compatible**: Prompts are directly copied from OpenEvolve configs, ensuring identical LLM guidance

## Benefits

1. **Centralized Configuration**: All examples now share the same base configuration, making it easier to update parameters globally
2. **Consistency**: Ensures all examples use the same island model and evolution settings
3. **Maintainability**: Changes to `island_medium.yaml` or `medium_budget.yaml` automatically apply to all examples
4. **Flexibility**: Easy to create new variants or override specific parameters using Hydra's composition system
5. **Fair Comparison**: Identical system messages enable valid A/B testing between Shinka and OpenEvolve frameworks

## Running the Examples

Each example can still be run the same way:
```bash
cd examples/<example_name>
python run_evo.py
```

The script will automatically load the shared configurations and display the full resolved configuration before starting evolution.

