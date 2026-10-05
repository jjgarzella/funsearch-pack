#include "funsearch.h"
#include "../candidate.h"
#include <stdio.h>

/* Implement instance parsing, candidate symbol resolution, construction,
 * independent verification, and scoring here. Never report FS_OK until all
 * required checks have succeeded. fs_init and fs_fini are optional.
 */
fs_result fs_score(fs_resolve_fn resolve, const char *instance)
{
    fs_result result = {0};
    (void)resolve;
    (void)instance;
    result.status = FS_ERROR;
    snprintf(result.msg, sizeof result.msg, "%s", "not implemented");
    return result;
}
