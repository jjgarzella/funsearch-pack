#define _POSIX_C_SOURCE 200809L

#include "funsearch.h"

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

static void *candidate_handle;
static FILE *proto;
/* Engine-chosen token echoed in the reply. A candidate shares this process,
 * so this only stops trivially forged replies on the inherited protocol fd;
 * the engine's SYNC barrier detects a forged extra line (see main). */
static char nonce[65];
/* Requests are read with read(2) into this buffer, never through stdio, and
 * wiped before candidate code runs, so no copy of the token is left behind in
 * a stdin FILE buffer the candidate could scan. */
static char request[PATH_MAX + 128];

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
    int sig_finite = 1;
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
    for (int32_t i = 0; i < result.nsig; ++i)
        sig_finite = sig_finite && isfinite(result.sig[i]);
    /* score and sig are meaningful only for FS_OK. A rejection may leave them
     * NaN; keep its status and msg, and emit a null score and no sig. */
    if (result.status == FS_OK && !isfinite(result.score)) {
        status = "ERROR";
        strcpy(result.msg, "non-finite score");
    } else if (result.status == FS_OK && !sig_finite) {
        /* JSON has no NaN/Inf, and a null entry would poison clustering. */
        status = "ERROR";
        strcpy(result.msg, "non-finite signature");
    }
    if (!sig_finite)
        result.nsig = 0;

    fputc('{', proto);
    if (nonce[0])
        fprintf(proto, "\"nonce\":\"%s\",", nonce);
    fprintf(proto, "\"status\":\"%s\",\"score\":", status);
    if (isfinite(result.score))
        fprintf(proto, "%.17g", result.score);
    else
        fputs("null", proto);
    fputs(",\"sig\":[", proto);
    for (int32_t i = 0; i < result.nsig; ++i) {
        if (i)
            fputc(',', proto);
        fprintf(proto, "%.17g", result.sig[i]);
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

/* Parse "<verb> #<hex token>" from line into nonce; return the text after
 * the token's terminating space (or its end), or NULL if malformed. */
static char *parse_token(char *line)
{
    size_t length;
    if (*line != '#')
        return NULL;
    length = strspn(line + 1, "0123456789abcdef");
    if (length == 0 || length >= sizeof(nonce) ||
        (line[length + 1] != ' ' && line[length + 1] != '\0'))
        return NULL;
    memcpy(nonce, line + 1, length);
    nonce[length] = '\0';
    return line[length + 1] ? line + length + 2 : line + length + 1;
}

/* Parse "SCORE <path>" or "SCORE #<hex token> <path>"; set nonce. */
static const char *score_path(char *line)
{
    char *path;
    nonce[0] = '\0';
    if (strncmp(line, "SCORE ", 6) != 0)
        return NULL;
    path = line + 6;
    if (*path == '#')
        path = parse_token(path);
    if (!path || !*path) {
        nonce[0] = '\0';
        return NULL;
    }
    return path;
}

/* "SYNC #<hex token>" is answered with {"sync":"<token>"} once the previous
 * reply has been written. The engine sends it after each scoring reply and
 * reads up to the echo: a second reply line before it means candidate code
 * wrote its own reply. */
static int sync_request(char *line)
{
    char *rest;
    nonce[0] = '\0';
    if (strncmp(line, "SYNC ", 5) != 0)
        return 0;
    rest = parse_token(line + 5);
    if (!rest || *rest) {
        nonce[0] = '\0';
        return 0;
    }
    fprintf(proto, "{\"sync\":\"%s\"}\n", nonce);
    fflush(proto);
    nonce[0] = '\0';
    return 1;
}

static void wipe(void *buffer, size_t size)
{
    volatile unsigned char *p = buffer;
    while (size--)
        *p++ = 0;
}

/* Read one request line into request without stdio buffering. Return its
 * length, -1 at EOF, -2 on a read error, or -3 for an overlong line (which is
 * consumed through its newline). */
static ssize_t read_request(void)
{
    size_t length = 0;
    int overlong = 0;
    for (;;) {
        char byte;
        ssize_t got = read(STDIN_FILENO, &byte, 1);
        if (got < 0 && errno == EINTR)
            continue;
        if (got < 0)
            return -2;
        if (got == 0) {
            if (length == 0 && !overlong)
                return -1;
            break;
        }
        if (byte == '\n')
            break;
        if (length + 1 < sizeof(request))
            request[length++] = byte;
        else
            overlong = 1;
    }
    request[length] = '\0';
    if (overlong) {
        wipe(request, sizeof(request));
        return -3;
    }
    return (ssize_t)length;
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

    /* Embedded runtimes such as Julia may make stdin nonblocking during
     * initialization. The host protocol waits for each engine request. */
    int input_flags = fcntl(STDIN_FILENO, F_GETFL);
    if (input_flags < 0 ||
        fcntl(STDIN_FILENO, F_SETFL, input_flags & ~O_NONBLOCK) < 0) {
        fatal("cannot restore blocking stdin");
        if (fini)
            fini();
        dlclose(evaluator);
        fclose(proto);
        return 3;
    }

    while ((length = read_request()) != -1) {
        if (length == -2) {
            fatal("stdin read failed");
            exit_code = 3;
            break;
        }
        if (length == -3) {
            error_reply("bad request");
            continue;
        }
        if (length && request[length - 1] == '\r')
            request[--length] = '\0';
        if (strcmp(request, "QUIT") == 0)
            break;
        if (sync_request(request))
            continue;
        const char *path = score_path(request);
        if (!path) {
            error_reply("bad request");
            continue;
        }
        candidate_handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
        /* Do not leave the token in a request buffer the candidate can scan. */
        wipe(request, sizeof(request));
        if (!candidate_handle) {
            error_reply(dlerror());
            continue;
        }
        fs_result result = score(resolve, argv[2]);
        dlclose(candidate_handle);
        candidate_handle = NULL;
        reply(result);
    }
    if (fini)
        fini();
    dlclose(evaluator);
    fclose(proto);
    return exit_code;
}
