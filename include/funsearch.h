#ifndef FUNSEARCH_H
#define FUNSEARCH_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Evaluator-author rules:
 * - Never return FS_OK without independently checking the object the candidate
 *   produced. The check must not call the candidate.
 * - Use exact or interval arithmetic where floating-point error could be exploited.
 * - Reject degenerate outputs explicitly with FS_INVALID and a clear msg.
 * - Any internal structure (several sizes, cheap-first cascade, early exit) is the
 *   evaluator's own business. It returns ONE score. It may report per-size values
 *   through sig.
 */
typedef enum { FS_OK = 0, FS_INVALID = 1, FS_ERROR = 2 } fs_status;
typedef struct {
  fs_status status;
  double    score;      /* higher is better; meaningful only if FS_OK */
  /* Optional signature: up to 8 finite values (e.g. per-size scores) that
   * group programs into clusters (rounded to 8 decimals). It never ranks.
   * An FS_OK submission with exactly the same score and sig as an active OK
   * program is rejected as a duplicate, so with nsig = 0 every submission
   * that ties an existing score is a duplicate whatever its code. */
  int32_t   nsig;       /* 0..8 */
  double    sig[8];
  char      msg[256];   /* shown to the mutator during `try`; NUL-terminated */
} fs_result;

/* resolve(sym) returns the address of symbol `sym` in the candidate, or NULL. */
typedef void *(*fs_resolve_fn)(const char *sym);

/*
 * REQUIRED. One long-lived worker process calls fs_score many times, for
 * different, unrelated candidates in sequence. Reset all per-candidate state on
 * every call: static buffers, caches and counters otherwise carry one
 * candidate's data into the next. Do not keep pointers into the candidate
 * (functions or data from resolve) after returning; its library is unloaded.
 * Several workers run in parallel processes.
 */
fs_result fs_score(fs_resolve_fn resolve, const char *instance);
/*
 * OPTIONAL: called once per worker process before any fs_score / at shutdown.
 * A run starts several workers and replaces them after a crash, a timeout or a
 * fixed number of scores, so fs_init may run many times over one run.
 */
int  fs_init(const char *instance);   /* nonzero = fatal: the run fails at startup */
void fs_fini(void);

#ifdef __cplusplus
}
#endif

#endif /* FUNSEARCH_H */
