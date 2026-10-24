.POSIX:

CC = cc
CFLAGS = -O2 -std=c11 -Wall -Wextra -Werror
JULIA = julia
FS_PACK = ../../..
JULIA_INCLUDE = $(shell $(JULIA) --startup-file=no -e 'print(joinpath(Sys.BINDIR, Base.INCLUDEDIR, "julia"))')
JULIA_LIB = $(shell $(JULIA) --startup-file=no -e 'print(abspath(Sys.BINDIR, Base.LIBDIR))')
ifeq ($(shell uname -s),Darwin)
DL_LIBS =
SHARED_LDFLAGS = -Wl,-undefined,dynamic_lookup
else
DL_LIBS = -ldl
SHARED_LDFLAGS =
endif

all: libcapset.so

libcapset.so: capset_eval.c $(FS_PACK)/include/funsearch.h
	$(CC) $(CFLAGS) -shared -fPIC $(SHARED_LDFLAGS) -I"$(FS_PACK)/include" -isystem "$(JULIA_INCLUDE)" \
		-o $@ capset_eval.c -L"$(JULIA_LIB)" -Wl,-rpath,"$(JULIA_LIB)" -ljulia $(DL_LIBS)

clean:
	rm -f libcapset.so

.PHONY: all clean
