#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
make worker
make -C examples/cap-set/evaluator
mkdir -p build/examples
for source in tests/examples/*.c examples/cap-set/seed.c; do
    name=$(basename "$source" .c)
    ${CC:-cc} -O2 -std=c11 -Wall -Wextra -Werror -shared -fPIC \
        -Iexamples/cap-set -o "build/examples/$name.so" "$source"
done
${CC:-cc} -O1 -g -fsanitize=address,undefined -shared -fPIC \
    -o build/examples/seed-asan.so examples/cap-set/seed.c
# The standalone Julia check exercises the verifier with malformed caps and
# cross-checks the greedy algorithm against a simple reference construction.
${JULIA:-julia} --startup-file=no tests/examples/check_capset.jl
python3 tests/examples/check_examples.py
