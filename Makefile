.POSIX:

CC = cc
PYTHON ?= python3
export PYTHON
CFLAGS = -O2 -std=c11 -Wall -Wextra -Werror
CPPFLAGS = -Iinclude
LDFLAGS =
ifeq ($(shell uname -s),Darwin)
LDLIBS =
else
LDLIBS = -ldl
endif

all: worker

worker: build/funsearch-worker

# Install with a rename so a concurrent engine never executes a partial binary.
build/funsearch-worker: worker/funsearch-worker.c include/funsearch.h
	mkdir -p build
	@flock -x build/.worker-build.lock sh -ec '\
		if $(PYTHON) -c "import os,sys; t=sys.argv[1]; sys.exit(not (os.path.isfile(t) and all(os.stat(t).st_mtime_ns >= os.stat(p).st_mtime_ns for p in sys.argv[2:])))" "$@" worker/funsearch-worker.c include/funsearch.h; then exit 0; fi; \
		tmp=$$(mktemp build/.funsearch-worker.XXXXXX); \
		trap "rm -f $$tmp" EXIT HUP INT TERM; \
		$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o "$$tmp" worker/funsearch-worker.c $(LDLIBS); \
		mv -f "$$tmp" "$@"; \
		touch "$@"'

test: worker
	@if test -n "$$(find tests -type f -name 'test_*.py' -print)"; then \
		$(PYTHON) -m unittest discover -s tests -t . -p 'test_*.py'; \
	fi
	@set -e; for suite in tests/*/run.sh; do \
		if test -f "$$suite"; then sh "$$suite"; fi; \
	done

clean:
	rm -rf build

.PHONY: all worker test clean
