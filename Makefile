.POSIX:

CC = cc
CFLAGS = -O2 -Wall -Wextra
CPPFLAGS = -Iinclude
LDFLAGS =
LDLIBS = -ldl

all: worker

worker:
	@if test -f worker/funsearch-worker.c; then \
		mkdir -p build && \
		$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o build/funsearch-worker worker/funsearch-worker.c $(LDLIBS); \
	fi

test:
	@if test -n "$$(find tests -type f -name 'test_*.py' -print)"; then \
		python3 -m unittest discover -s tests -t . -p 'test_*.py'; \
	fi
	@set -e; for suite in tests/*/run.sh; do \
		if test -f "$$suite"; then sh "$$suite"; fi; \
	done

clean:
	rm -rf build

.PHONY: all worker test clean
