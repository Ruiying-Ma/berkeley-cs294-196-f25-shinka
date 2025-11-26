#!/bin/bash
# Test script to verify PRISM integration with ShinkaEvolve

set -e  # Exit on error

echo "======================================"
echo "PRISM Integration Verification Tests"
echo "======================================"
echo ""

# Test 1: Check directory structure
echo "Test 1: Verifying directory structure..."
if [ -d "examples/prism" ] && [ -d "openevolve_examples/prism" ]; then
    echo "✓ Directories exist"
else
    echo "✗ Missing directories"
    exit 1
fi

# Test 2: Check required files exist
echo ""
echo "Test 2: Checking required files..."
REQUIRED_FILES=(
    "examples/prism/initial.py"
    "examples/prism/evaluate.py"
    "examples/prism/run_evo.py"
    "examples/prism/README.md"
    "openevolve_examples/prism/evaluator.py"
    "openevolve_examples/prism/initial_program.py"
)

for file in "${REQUIRED_FILES[@]}"; do
    if [ -f "$file" ]; then
        echo "✓ $file"
    else
        echo "✗ Missing: $file"
        exit 1
    fi
done

# Test 3: Run initial program
echo ""
echo "Test 3: Running initial program..."
cd examples/prism
OUTPUT=$(python initial.py 2>&1)
if echo "$OUTPUT" | grep -q "Max KVPR"; then
    echo "✓ Initial program runs successfully"
    echo "  Output: $(echo "$OUTPUT" | grep "Max KVPR")"
else
    echo "✗ Initial program failed"
    echo "$OUTPUT"
    exit 1
fi

# Test 4: Run evaluation
echo ""
echo "Test 4: Running evaluation..."
RESULTS_DIR="test_results_$$"
python evaluate.py --program_path initial.py --results_dir "$RESULTS_DIR" > /dev/null 2>&1
if [ -f "$RESULTS_DIR/metrics.json" ] && [ -f "$RESULTS_DIR/correct.json" ]; then
    echo "✓ Evaluation produces required output files"
    
    # Check metrics
    COMBINED_SCORE=$(python -c "import json; print(json.load(open('$RESULTS_DIR/metrics.json'))['combined_score'])")
    echo "  Combined Score: $COMBINED_SCORE"
    
    # Clean up
    rm -rf "$RESULTS_DIR"
else
    echo "✗ Evaluation failed to produce output files"
    rm -rf "$RESULTS_DIR"
    exit 1
fi

# Test 5: Verify no __pycache__ in git
echo ""
echo "Test 5: Checking for unwanted cache files..."
cd ../..
if find examples/prism openevolve_examples/prism -name "__pycache__" -type d | grep -q .; then
    echo "⚠ Warning: __pycache__ directories found (should be .gitignored)"
else
    echo "✓ No cache directories found"
fi

# Summary
echo ""
echo "======================================"
echo "All tests passed! ✅"
echo "======================================"
echo ""
echo "The PRISM use case has been successfully integrated into ShinkaEvolve."
echo ""
echo "Next steps:"
echo "  1. Review documentation: cat examples/prism/README.md"
echo "  2. Review integration: cat examples/prism/INTEGRATION_SUMMARY.md"
echo "  3. Run evolution: cd examples/prism && python run_evo.py"
echo ""

