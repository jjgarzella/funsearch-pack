#include "funsearch.h"

#include <errno.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

fs_result fs_score(fs_resolve_fn resolve, const char *instance)
{
    pid_t child;
    fs_result result = { .status = FS_ERROR };
    (void)resolve;
    (void)instance;

    child = fork();
    if (child == 0)
        _exit(0);
    if (child > 0) {
        int status;
        waitpid(child, &status, 0);
        snprintf(result.msg, sizeof(result.msg), "fork unexpectedly succeeded");
        return result;
    }
    if (errno == EAGAIN || errno == EPERM) {
        result.status = FS_OK;
        result.score = 1;
        snprintf(result.msg, sizeof(result.msg), "fork denied");
    } else {
        snprintf(result.msg, sizeof(result.msg), "fork failed unexpectedly: %d", errno);
    }
    return result;
}
