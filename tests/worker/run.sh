#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
mkdir -p build/test
for source in tests/worker/*.c; do
    name=$(basename "$source" .c)
    ${CC:-cc} -O2 -std=c11 -Wall -Wextra -Werror -shared -fPIC -Iinclude \
        -o "build/test/lib${name}.so" "$source"
done
# Run directly so top-level unittest discovery does not run this suite twice.
python3 tests/worker/check_worker.py
