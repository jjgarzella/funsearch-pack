#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
make worker
make -C examples/cap-set/evaluator
if [ "$(uname -s)" = Darwin ]; then
    JULIA_BIN=${JULIA:-julia}
    julia_lib=$("$JULIA_BIN" --startup-file=no -e 'print(abspath(Sys.BINDIR, Base.LIBDIR))')
    if [ -n "${DYLD_LIBRARY_PATH:-}" ]; then
        export DYLD_LIBRARY_PATH="$julia_lib/julia:$DYLD_LIBRARY_PATH"
    else
        export DYLD_LIBRARY_PATH="$julia_lib/julia"
    fi
fi
mkdir -p build/examples
shared_ldflags=
if [ "$(uname -s)" = Darwin ]; then
    shared_ldflags='-Wl,-undefined,dynamic_lookup'
fi
for source in tests/examples/*.c examples/cap-set/seed.c; do
    name=$(basename "$source" .c)
    ${CC:-cc} -O2 -std=c11 -Wall -Wextra -Werror -shared -fPIC $shared_ldflags \
        -Iexamples/cap-set -o "build/examples/$name.so" "$source"
done
${CC:-cc} -O1 -g -fsanitize=address,undefined -shared -fPIC $shared_ldflags \
    -o build/examples/seed-asan.so examples/cap-set/seed.c
# The standalone Julia check exercises the verifier with malformed caps and
# cross-checks the greedy algorithm against a simple reference construction.
${JULIA:-julia} --startup-file=no tests/examples/check_capset.jl
${PYTHON:-python3} tests/examples/check_examples.py
