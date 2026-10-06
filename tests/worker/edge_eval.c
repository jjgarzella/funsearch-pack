#include "funsearch.h"
#include <math.h>
#include <string.h>
#include <sys/resource.h>

/* Deliberately malformed results exercise the worker's ABI boundaries. */
fs_result fs_score(fs_resolve_fn resolve, const char *instance)
{
    fs_result result = { .status = FS_OK, .score = 2 };
    (void)resolve;
    if (strcmp(instance, "escape") == 0) {
        strcpy(result.msg, "quote\" slash\\ newline\n tab\t back\b form\f return\r low\001");
    } else if (strcmp(instance, "unterminated") == 0) {
        memset(result.msg, 'x', sizeof(result.msg));
        result.nsig = 99;
        for (int i = 0; i < 8; ++i)
            result.sig[i] = i;
    } else if (strcmp(instance, "negative-nsig") == 0) {
        result.nsig = -10;
    } else if (strcmp(instance, "nan") == 0) {
        result.score = NAN;
    } else if (strcmp(instance, "infinity") == 0) {
        result.score = INFINITY;
    } else if (strcmp(instance, "nan-sig") == 0) {
        result.nsig = 2;
        result.sig[0] = 1;
        result.sig[1] = NAN;
    } else if (strcmp(instance, "limit") == 0) {
        struct rlimit limit;
        if (getrlimit(RLIMIT_AS, &limit) != 0) {
            result.status = FS_ERROR;
        } else {
            result.score = (double)limit.rlim_cur / (1024 * 1024);
            result.nsig = 1;
            result.sig[0] = (double)limit.rlim_max / (1024 * 1024);
        }
    }
    return result;
}
