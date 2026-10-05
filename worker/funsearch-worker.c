#define _POSIX_C_SOURCE 200809L

#include "funsearch.h"

#include <dlfcn.h>
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

static void *candidate_handle;
static FILE *proto;

static void *resolve(const char *sym)
{
    return dlsym(candidate_handle, sym);
}

static void json_string(const char *value)
{
    const unsigned char *p = (const unsigned char *)value;
    fputc('"', proto);
    for (; *p; ++p) {
        switch (*p) {
        case '"': fputs("\\\"", proto); break;
        case '\\': fputs("\\\\", proto); break;
        case '\b': fputs("\\b", proto); break;
        case '\f': fputs("\\f", proto); break;
        case '\n': fputs("\\n", proto); break;
        case '\r': fputs("\\r", proto); break;
        case '\t': fputs("\\t", proto); break;
        default:
            if (*p < 0x20)
                fprintf(proto, "\\u%04x", (unsigned)*p);
            else
                fputc(*p, proto);
        }
    }
    fputc('"', proto);
}

static void fatal(const char *msg)
{
    fputs("{\"fatal\":", proto);
    json_string(msg);
    fputs("}\n", proto);
    fflush(proto);
}

static void reply(fs_result result)
{
    const char *status;
    result.msg[sizeof(result.msg) - 1] = '\0';
    if (result.nsig < 0)
        result.nsig = 0;
    if (result.nsig > 8)
        result.nsig = 8;

    switch (result.status) {
    case FS_OK: status = "OK"; break;
    case FS_INVALID: status = "INVALID"; break;
    case FS_ERROR: status = "ERROR"; break;
    default:
        status = "ERROR";
        strcpy(result.msg, "unknown evaluator status");
        break;
    }
    if (!isfinite(result.score)) {
        status = "ERROR";
        strcpy(result.msg, "non-finite score");
    }

    fprintf(proto, "{\"status\":\"%s\",\"score\":", status);
    if (isfinite(result.score))
        fprintf(proto, "%.17g", result.score);
    else
        fputs("null", proto);
    fputs(",\"sig\":[", proto);
    for (int32_t i = 0; i < result.nsig; ++i) {
        if (i)
            fputc(',', proto);
        if (isfinite(result.sig[i]))
            fprintf(proto, "%.17g", result.sig[i]);
        else
            fputs("null", proto);
    }
    fputs("],\"msg\":", proto);
    json_string(result.msg);
    fputs("}\n", proto);
    fflush(proto);
}

static void error_reply(const char *msg)
{
    fs_result result = { .status = FS_ERROR };
    snprintf(result.msg, sizeof(result.msg), "%s", msg);
    reply(result);
}

static int memory_limit(void)
{
    const char *value = getenv("FS_MEMORY_MB");
    char *end;
    unsigned long long mb;
    struct rlimit limit;
    if (!value)
        return 0;
    errno = 0;
    mb = strtoull(value, &end, 10);
    if (errno || end == value || *end || *value == '-' ||
        mb > ((unsigned long long)RLIM_INFINITY - 1) / (1024 * 1024)) {
        fatal("invalid FS_MEMORY_MB");
        return -1;
    }
    limit.rlim_cur = limit.rlim_max = (rlim_t)(mb * 1024 * 1024);
    if (setrlimit(RLIMIT_AS, &limit) != 0) {
        fatal(strerror(errno));
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    void *evaluator;
    fs_result (*score)(fs_resolve_fn, const char *);
    int (*init)(const char *);
    void (*fini)(void);
    char *line = NULL;
    size_t capacity = 0;
    ssize_t length;
    int exit_code = 0;
    int protocol_fd;

    if (argc != 3) {
        fprintf(stderr, "usage: funsearch-worker <evaluator.so> <instance>\n");
        return 2;
    }

    /* Preserve the protocol fd before loading any evaluator/candidate code. */
    protocol_fd = dup(STDOUT_FILENO);
    if (protocol_fd < 0) {
        perror("dup stdout");
        return 3;
    }
    proto = fdopen(protocol_fd, "w");
    if (!proto) {
        perror("fdopen protocol");
        close(protocol_fd);
        return 3;
    }
    if (dup2(STDERR_FILENO, STDOUT_FILENO) < 0) {
        fatal("cannot redirect stdout to stderr");
        fclose(proto);
        return 3;
    }
    /* Flush evaluator printf output promptly, including before a crash. */
    setvbuf(stdout, NULL, _IONBF, 0);
    if (memory_limit() != 0) {
        fclose(proto);
        return 3;
    }

    evaluator = dlopen(argv[1], RTLD_NOW | RTLD_GLOBAL);
    if (!evaluator) {
        fatal(dlerror());
        fclose(proto);
        return 3;
    }
    score = (fs_result (*)(fs_resolve_fn, const char *))dlsym(evaluator, "fs_score");
    init = (int (*)(const char *))dlsym(evaluator, "fs_init");
    fini = (void (*)(void))dlsym(evaluator, "fs_fini");
    if (!score) {
        fatal("missing fs_score");
        dlclose(evaluator);
        fclose(proto);
        return 3;
    }
    if (init) {
        int result = init(argv[2]);
        if (result) {
            char msg[64];
            snprintf(msg, sizeof(msg), "fs_init returned %d", result);
            fatal(msg);
            dlclose(evaluator);
            fclose(proto);
            return 3;
        }
    }

    while ((length = getline(&line, &capacity, stdin)) >= 0) {
        if (length && line[length - 1] == '\n')
            line[--length] = '\0';
        if (length && line[length - 1] == '\r')
            line[--length] = '\0';
        if (strcmp(line, "QUIT") == 0)
            break;
        if (strncmp(line, "SCORE ", 6) != 0 || !line[6]) {
            error_reply("bad request");
            continue;
        }
        candidate_handle = dlopen(line + 6, RTLD_NOW | RTLD_LOCAL);
        if (!candidate_handle) {
            error_reply(dlerror());
            continue;
        }
        fs_result result = score(resolve, argv[2]);
        dlclose(candidate_handle);
        candidate_handle = NULL;
        reply(result);
    }
    if (ferror(stdin)) {
        fatal("stdin read failed");
        exit_code = 3;
    }
    free(line);
    if (fini)
        fini();
    dlclose(evaluator);
    fclose(proto);
    return exit_code;
}
