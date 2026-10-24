#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$script_dir/fs-python" "$script_dir/gc_lifecycle.py" on-start "$@"
