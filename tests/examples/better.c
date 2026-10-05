#include "candidate.h"

// IDEA: prefer balanced counts of the three coordinate values.
double priority(const int8_t *v, int32_t n)
{
    int counts[3] = {0};
    for (int32_t i = 0; i < n; ++i)
        ++counts[v[i]];
    return -(counts[0] * counts[0] + counts[1] * counts[1] + counts[2] * counts[2]);
}
