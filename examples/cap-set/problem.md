# Cap sets in F_3^n

A cap set is a collection of distinct vectors in `{0,1,2}^n` with no three
distinct points `x, y, z` satisfying `x + y + z = 0 (mod 3)` in every
coordinate. Such a triple is a line in the affine space over the field F_3.

Write the C function in `candidate.h` to assign a finite priority to each
vector. The evaluator sorts all `3^n` vectors by descending priority, breaking
ties lexicographically (coordinate 0 first, with `0 < 1 < 2`). It visits them
in that order and adds a vector exactly when the result remains a cap set.
The score to maximize is the resulting set's size at the requested dimension,
by default **n=6**. The evaluator independently verifies every resulting cap.

The known optimum sizes for n=1 through 6 are 2, 4, 9, 20, 45, and 112.
The constant seed is a baseline, not an optimum. Signature entries report
the sizes at dimensions 4, 5, and 6, when those dimensions are requested.

Ideas to explore: exploit coordinate symmetry; count coordinates equal to
0, 1, or 2; assign different weights to those counts or to different
positions; consider interactions between coordinates. A useful ordering at
one dimension may behave differently at another. Keep the function simple,
deterministic, and finite, and leave its input unchanged.
