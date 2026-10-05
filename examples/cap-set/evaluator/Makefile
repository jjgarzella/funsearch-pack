.POSIX:

CC = cc
CFLAGS = -O2 -std=c11 -Wall -Wextra -Werror
JULIA = julia
FS_PACK = ../../..
JULIA_INCLUDE = $(shell $(JULIA) --startup-file=no -e 'print(joinpath(Sys.BINDIR, Base.INCLUDEDIR, "julia"))')
JULIA_LIB = $(shell $(JULIA) --startup-file=no -e 'print(abspath(Sys.BINDIR, Base.LIBDIR))')

all: libcapset.so

libcapset.so: capset_eval.c $(FS_PACK)/include/funsearch.h
	$(CC) $(CFLAGS) -shared -fPIC -I"$(FS_PACK)/include" -I"$(JULIA_INCLUDE)" \
		-o $@ capset_eval.c -L"$(JULIA_LIB)" -Wl,-rpath,"$(JULIA_LIB)" -ljulia -ldl

clean:
	rm -f libcapset.so

.PHONY: all clean
