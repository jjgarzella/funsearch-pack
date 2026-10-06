.POSIX:

CC = cc
CFLAGS = -O2 -std=c11 -Wall -Wextra -Werror
CPPFLAGS = -Iinclude
LDFLAGS =
LDLIBS = -ldl

all: worker

worker: build/funsearch-worker

# Install with a rename so a concurrent engine never executes a partial binary.
build/funsearch-worker: worker/funsearch-worker.c include/funsearch.h
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@.tmp worker/funsearch-worker.c $(LDLIBS)
	mv -f $@.tmp $@

test: worker
	@if test -n "$$(find tests -type f -name 'test_*.py' -print)"; then \
		python3 -m unittest discover -s tests -t . -p 'test_*.py'; \
	fi
	@set -e; for suite in tests/*/run.sh; do \
		if test -f "$$suite"; then sh "$$suite"; fi; \
	done

clean:
	rm -rf build

.PHONY: all worker test clean
