#!/bin/sh
set -eu

FS_PACK=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
export FS_PACK
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
problem_dir="$scratch/problem"
mkdir -p "$problem_dir"
cp -R "$FS_PACK/skills/funsearch-problem-setup/templates/." "$problem_dir/"
make -C "$problem_dir/evaluator"
test -f "$problem_dir/evaluator/libevaluator.so"

if [ -f "$FS_PACK/bin/funsearch" ]; then
    exit_status=0
    "$FS_PACK/bin/funsearch" check "$problem_dir" >"$scratch/result.json" || exit_status=$?
    # check must fail for an evaluator error, while still printing its result.
    test "$exit_status" -eq 1
    python3 - "$scratch/result.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as stream:
    result = json.load(stream)
assert result["status"] == "ERROR", result
assert result["msg"] == "not implemented", result
PY
    echo 'skill templates: check reported ERROR "not implemented" as expected'
else
    echo 'skill templates: evaluator built; bin/funsearch not yet available'
fi
