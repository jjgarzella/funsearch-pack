#include "funsearch.h"
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

fs_result fs_score(fs_resolve_fn resolve, const char *instance)
{
    const size_t size = 96 * 1024 * 1024;
    volatile unsigned char *memory = malloc(size);
    (void)resolve;
    (void)instance;
    if (!memory)
        return (fs_result){ .status = FS_ERROR };
    for (size_t index = 0; index < size; index += 4096)
        memory[index] = 1;
    sleep(5);
    free((void *)memory);
    return (fs_result){ .status = FS_OK, .score = 1 };
}
