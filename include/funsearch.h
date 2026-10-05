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
  int32_t   nsig;       /* 0..8 */
  double    sig[8];
  char      msg[256];   /* shown to the mutator during `try`; NUL-terminated */
} fs_result;

/* resolve(sym) returns the address of symbol `sym` in the candidate, or NULL. */
typedef void *(*fs_resolve_fn)(const char *sym);

/* REQUIRED */
fs_result fs_score(fs_resolve_fn resolve, const char *instance);
/* OPTIONAL: called once per worker process before any fs_score / at shutdown. */
int  fs_init(const char *instance);   /* nonzero = fatal: the run fails at startup */
void fs_fini(void);

#ifdef __cplusplus
}
#endif

#endif /* FUNSEARCH_H */
