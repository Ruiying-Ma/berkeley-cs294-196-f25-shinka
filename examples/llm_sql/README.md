# LLM SQL Prefix Optimizer

## Overview

This example demonstrates automated evolution of a DataFrame reordering algorithm designed to maximize **prefix hit count** for efficient LLM prompt caching. The algorithm reorders columns (and rows) to maximize the reuse of cached prefixes when rows are processed sequentially by an LLM.

## Problem Description

### Context
When processing database query results row-by-row with an LLM, many consecutive rows share common prefixes (identical starting values). Modern LLM APIs cache these prefixes, significantly reducing API costs and latency. The challenge is to reorder DataFrame columns to maximize this prefix reuse.

### Objective
Evolve an algorithm that:
1. **Maximizes prefix hit rate**: Arrange data so consecutive rows share long common prefixes
2. **Minimizes runtime**: Keep the reordering algorithm computationally efficient

### Scoring
- **Prefix Hit Rate**: Measured by counting character-level prefix matches using a Trie data structure
- **Runtime Penalty**: Algorithms running over 12 seconds are penalized
- **Combined Score**: `0.95 × hit_rate + 0.05 × runtime_score`

## Algorithm Overview

### GGR (Greedy Grouped Reordering)
The baseline algorithm uses a recursive grouping strategy:

1. **Column Statistics**: Score columns by `length² × (group_count - 1)` 
2. **Value Selection**: Find the value with maximum weighted count
3. **Row Grouping**: Group all rows containing that value
4. **Column Reordering**: Move columns with the target value to the front
5. **Recursive Processing**: 
   - Column recursion: Process remaining columns in each group
   - Row recursion: Process remaining rows not in the group
6. **Parallel Processing**: Split large DataFrames for parallel processing

### Key Features
- Recursive grouping by high-frequency, high-value-length columns
- Adaptive column ordering based on value distribution
- Early stopping for computational efficiency
- Parallel processing for large datasets
- Support for column merging and dependency tracking

## Setup

### Prerequisites
```bash
# Required packages (should be installed with shinka)
pip install pandas networkx pyspark
```

### Dataset Verification
Ensure all datasets are present in the `datasets/` directory:
```bash
ls datasets/
# Should show: movies.csv, beer.csv, BIRD.csv, PDMX.csv, products.csv
```

All datasets are included in the repository. No additional downloads required.

## Usage

### Standalone Evaluation
Test the initial algorithm on all datasets:
```bash
cd /path/to/ShinkaEvolve/examples/llm_sql
python evaluate.py --program_path initial.py --results_dir results
```

### Run Evolution (Local)
Start local evolution to optimize the algorithm:
```bash
cd /path/to/ShinkaEvolve/examples/llm_sql
python run_evo.py
```

This will:
- Evaluate the initial algorithm on 5 datasets
- Generate improved variants using LLMs
- Track progress in `results_llm_sql/`
- Store evolution database in `evolution_db.sqlite`

### Run Evolution (Hydra)
Use Hydra configuration for distributed execution:
```bash
cd /path/to/ShinkaEvolve
python -m shinka.launch_hydra variant=llm_sql_example
```

Configuration files:
- Task config: `configs/task/llm_sql.yaml`
- Variant config: `configs/variant/llm_sql_example.yaml`

## File Structure

```
examples/llm_sql/
├── README.md              # This file
├── initial.py             # Initial algorithm (with EVOLVE-BLOCK markers)
├── evaluate.py            # Evaluation script using shinka.core
├── run_evo.py             # Local evolution runner
├── solver.py              # Base Algorithm class
├── utils.py               # Trie and evaluation utilities
├── quick_greedy.py        # Alternative baseline algorithm
├── initial_program.py     # Original evolved version
├── config.yaml            # Legacy config (for reference)
└── datasets/
    ├── movies.csv         # Entertainment data
    ├── beer.csv           # Product reviews
    ├── BIRD.csv           # Forum posts
    ├── PDMX.csv           # Metadata
    └── products.csv       # E-commerce data
```

## Datasets

### 1. Movies (movies.csv)
- Entertainment database with movie information
- Columns: movieinfo, movietitle, rottentomatoeslink, etc.
- Column merge: `['movieinfo', 'movietitle', 'rottentomatoeslink']`

### 2. Beer (beer.csv)
- Beer product reviews
- Columns: beer/beerId, beer/name, rating, etc.
- Column merge: `['beer/beerId', 'beer/name']`

### 3. BIRD (BIRD.csv)
- Forum post database
- Columns: PostId, Body, etc.
- Column merge: `['PostId', 'Body']`

### 4. PDMX (PDMX.csv)
- Metadata repository
- Columns: path, metadata, hasmetadata, isofficial, etc.
- Column merges: `['path', 'metadata']`, `['hasmetadata', 'isofficial', 'isuserpublisher', 'isdraft', 'hasannotations', 'subsetall']`

### 5. Products (products.csv)
- E-commerce product catalog
- Columns: product_title, parent_asin, etc.
- Column merge: `['product_title', 'parent_asin']`

## Expected Performance

### Baseline (Initial Algorithm)
- Average hit rate: ~40-60% (varies by dataset)
- Average runtime: 2-8 seconds per dataset
- Combined score: ~0.40-0.57

### Evolution Goals
- Target hit rate: 60-80%
- Target runtime: <8 seconds per dataset
- Target combined score: >0.65

## Technical Details

### Path Resolution
The code uses robust path resolution to avoid the bugs seen in other examples:
```python
def find_llm_sql_dir(start_path):
    """Find the llm_sql directory by searching upward from current location."""
    # Searches for solver.py/utils.py to ensure correct directory
```

This ensures the code works when:
- Executed from the llm_sql directory
- Executed from repository root
- Evolved programs are placed in arbitrary result directories

### Data Loading
All dataset paths use absolute paths relative to the llm_sql directory:
```python
DATASET_DIR = os.path.join(llm_sql_dir, 'datasets')
TEST_FILES = [os.path.join(DATASET_DIR, "movies.csv"), ...]
```

This prevents `FileNotFoundError` issues when running from different locations.

### Evaluation Metrics
- **Prefix Hit Count**: Total character-level matches using Trie structure
- **Hit Rate**: Prefix hits / total string length
- **Runtime**: Wall-clock time for reordering
- **Combined Score**: Weighted average favoring hit rate (95%) over speed (5%)

## Evolution Strategy

### Parent Selection
The default strategy is **weighted prioritization**:
- Programs sampled proportionally to their fitness scores
- Balances exploration of novel solutions with exploitation of good programs

### Patch Types
- **Diff patches (60%)**: Focused modifications to existing code
- **Full rewrites (30%)**: Complete reimplementations
- **Crossover (10%)**: Combine features from multiple parents

### LLM Models
Multiple models for diversity:
- Gemini 2.5 Pro/Flash
- Claude Sonnet 4
- GPT-5/o4 variants
- Dynamic model selection based on UCB1 (Upper Confidence Bound)

### Meta-Recommendations
Every 10 generations, a meta-LLM analyzes:
- Evolution progress
- Promising directions
- Potential improvements
- Algorithm insights

## Troubleshooting

### "Dataset not found" Error
**Problem**: Evaluation fails with missing dataset files.

**Solution**: Verify datasets are in the correct location:
```bash
ls examples/llm_sql/datasets/
# Should show 5 CSV files
```

### "ModuleNotFoundError: No module named 'solver'"
**Problem**: Import path resolution fails.

**Solution**: The code includes robust path finding. If this fails, check that:
1. `solver.py` and `utils.py` exist in `examples/llm_sql/`
2. You're running from a location within the repository

### Slow Evaluation
**Problem**: Each dataset takes too long to process.

**Solution**: Adjust recursion depth limits:
```python
# In run_llm_sql_optimizer call:
row_stop=2,  # Reduce from 4
col_stop=1,  # Reduce from 2
```

### Low Hit Rates
**Problem**: Algorithm produces poor prefix reuse.

**Possible causes**:
1. Dataset has high cardinality (many unique values)
2. Recursive grouping stopped too early
3. Column scoring doesn't match data characteristics

**Solutions**: Evolution should discover better strategies, but you can try:
- Adjusting `distinct_value_threshold` (default: 0.7)
- Modifying `early_stop` threshold
- Changing base chunk size for parallel processing

## Key Files to Evolve

The evolution focuses on the `EVOLVE-BLOCK` in `initial.py`:
- **Class: Evolved**
  - `find_max_group_value()`: Select grouping value
  - `reorder_columns_for_value()`: Determine column order
  - `recursive_reorder()`: Main recursive logic
  - `column_recursion()`: Column-wise processing
  - `recursive_split_and_reorder()`: Parallel processing

## References

- Original paper: [LLM Prefix Caching Optimization]
- Shinka framework: [GitHub Repository]
- Related: `txn_scheduling`, `eplb` examples

## Version History

- **v1.0**: Initial Shinka integration
  - Added robust path resolution
  - Integrated with shinka.core evaluation framework
  - Created Hydra configuration files
  - Added comprehensive documentation

## Contact

For questions or issues, please refer to the main Shinka repository documentation.


