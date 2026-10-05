#include "funsearch.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int fs_init(const char *instance)
{
    puts("toy evaluator init on stdout");
    return strstr(instance, "fail=1") ? 1 : 0;
}

fs_result fs_score(fs_resolve_fn resolve, const char *instance)
{
    double (*f)(void) = (double (*)(void))resolve("f");
    fs_result result = { .status = FS_ERROR };
    (void)instance;
    puts("toy evaluator score on stdout");
    if (!f) {
        strcpy(result.msg, "missing f");
        return result;
    }
    result.score = f();
    if (result.score < 0) {
        result.status = FS_INVALID;
        strcpy(result.msg, "negative");
        return result;
    }
    result.status = FS_OK;
    result.nsig = 2;
    result.sig[0] = result.score;
    result.sig[1] = 1.0;
    strcpy(result.msg, "ok");
    return result;
}

void fs_fini(void)
{
    const char *path = getenv("TOY_FINI_MARK");
    if (path) {
        FILE *mark = fopen(path, "w");
        if (mark)
            fclose(mark);
    }
}
