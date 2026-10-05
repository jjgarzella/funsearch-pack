#include "candidate.h"
#include <signal.h>
double priority(const int8_t *v, int32_t n)
{
    (void)v;
    (void)n;
    raise(SIGSEGV);
    return 0.0;
}
