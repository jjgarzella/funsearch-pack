#ifndef CAPSET_CANDIDATE_H
#define CAPSET_CANDIDATE_H

#include <stdint.h>

/* v has n coordinates in {0, 1, 2}. Do not modify v or retain its address.
 * Return a finite priority: larger values are considered first by the greedy
 * construction; equal priorities are ordered lexicographically. */
double priority(const int8_t *v, int32_t n);

#endif
