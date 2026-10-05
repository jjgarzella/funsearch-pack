#ifndef FUNSEARCH_CANDIDATE_H
#define FUNSEARCH_CANDIDATE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* v: borrowed array of n entries, each in {0,1,2}; n is positive.
 * The array remains valid only during the call and must not be modified.
 * Return a finite priority used to order vectors (higher first).
 * A priority is not a claim that a resulting mathematical object is valid.
 * Replace this illustrative interface with your problem's exact contract.
 */
double priority(const int8_t *v, int32_t n);

#ifdef __cplusplus
}
#endif

#endif
