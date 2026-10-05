# Mathematical goal

Describe the object to construct, its mathematical constraints, and the score
to maximize. State how the score is independently verified.

## Instance and candidate interface

Explain each instance parameter and its supported range. The initial header
declares `double priority(const int8_t *v, int32_t n)`: `v` is a borrowed vector
of length `n` with entries in `{0,1,2}`, and the finite return value orders
vectors for construction. Replace this description when changing the header.

## Known results and starting candidate

Record useful constructions, known bounds, references, and the expected seed
score. The initial seed assigns every vector priority zero; replace it if that
does not yield a valid baseline for your problem.

## Hints and edge cases

Suggest promising ideas and state public rules for degeneracies, numerical
accuracy, determinism, and output ownership. Keep private tests and evaluator
implementation secrets out of this file: candidate authors read it.
