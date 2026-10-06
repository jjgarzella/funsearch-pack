# The copied problem cannot infer where the pack is installed. Before using
# make or funsearch check/run start, export FS_PACK to the absolute pack path:
#   export FS_PACK=/absolute/path/to/funsearch-pack
# Direct make also accepts: make FS_PACK=/absolute/path/to/funsearch-pack
# This locates the authoritative C ABI header at $FS_PACK/include/funsearch.h.
CC = cc
CFLAGS = -O2 -std=c11 -Wall -Wextra -Werror
CPPFLAGS =
LDFLAGS =

.PHONY: all clean check-pack
all: libevaluator.so

check-pack:
	@test -n "$(FS_PACK)" || { echo "Set FS_PACK to the absolute FunSearch pack path" >&2; exit 1; }
	@test -f "$(FS_PACK)/include/funsearch.h" || { echo "FS_PACK must contain include/funsearch.h" >&2; exit 1; }

# check-pack is order-only (GNU make): it still runs first, but as a phony
# target it must not make an up-to-date library look stale.
libevaluator.so: evaluator.c ../candidate.h | check-pack
	$(CC) $(CPPFLAGS) -I"$(FS_PACK)/include" $(CFLAGS) -shared -fPIC $(LDFLAGS) -o $@ evaluator.c

clean:
	rm -f libevaluator.so
