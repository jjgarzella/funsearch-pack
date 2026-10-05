#define _GNU_SOURCE
#include "funsearch.h"
#include <julia.h>

#include <ctype.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static jl_value_t *score_function;
static int32_t dimension;
static int initialized;

static double seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void timing(const char *phase, double start)
{
    if (getenv("FS_CAPSET_TIMINGS"))
        fprintf(stderr, "capset %s %.6f s\n", phase, seconds() - start);
}

/* n is evaluator configuration, never supplied by a candidate. Bound the
 * exponential point enumeration to dimensions 1..8 for this example. */
static int parse_instance(const char *instance, int32_t *n)
{
    int found = 0;
    char *copy = strdup(instance ? instance : "");
    char *save = NULL;
    if (!copy)
        return -1;
    for (char *token = strtok_r(copy, ",", &save); token;
         token = strtok_r(NULL, ",", &save)) {
        while (isspace((unsigned char)*token))
            ++token;
        if (strncmp(token, "n=", 2) != 0)
            continue;
        char *end;
        errno = 0;
        long value = strtol(token + 2, &end, 10);
        while (isspace((unsigned char)*end))
            ++end;
        if (found || errno || end == token + 2 || *end || value < 1 || value > 8) {
            free(copy);
            return -1;
        }
        *n = (int32_t)value;
        found = 1;
    }
    free(copy);
    return found ? 0 : -1;
}

/* Root the exception before asking Julia to format it (which may allocate). */
static fs_status exception_message(char *message, size_t capacity)
{
    jl_value_t *exception = jl_exception_occurred();
    jl_value_t *formatted = NULL;
    JL_GC_PUSH2(&exception, &formatted);
    jl_module_t *module = (jl_module_t *)jl_get_global(jl_main_module, jl_symbol("CapSetEvaluator"));
    jl_value_t *invalid_priority = module ? jl_get_global(module, jl_symbol("InvalidPriority")) : NULL;
    fs_status status = invalid_priority && jl_isa(exception, invalid_priority) ? FS_INVALID : FS_ERROR;
    formatted = jl_call2(jl_get_function(jl_base_module, "sprint"),
                        (jl_value_t *)jl_get_function(jl_base_module, "showerror"), exception);
    const char *text = formatted && !jl_exception_occurred() && jl_is_string(formatted)
                       ? jl_string_ptr(formatted) : "Julia exception (could not format)";
    snprintf(message, capacity, "%s", text);
    fprintf(stderr, "capset Julia exception: %s\n", text);
    JL_GC_POP();
    return status;
}

/* A private anchor avoids symbol interposition when locating this library. */
static void source_anchor(void) {}

int fs_init(const char *instance)
{
    double start = seconds();
    if (parse_instance(instance, &dimension)) {
        fprintf(stderr, "capset instance requires one n=<integer> in 1..8\n");
        return 1;
    }
    Dl_info info;
    if (!dladdr((void *)source_anchor, &info) || !info.dli_fname) {
        fprintf(stderr, "capset cannot locate evaluator library\n");
        return 1;
    }
    char *library = realpath(info.dli_fname, NULL);
    if (!library) {
        perror("capset evaluator path");
        return 1;
    }
    char *slash = strrchr(library, '/');
    if (!slash) {
        free(library);
        return 1;
    }
    *slash = '\0';
    size_t capacity = strlen(library) + sizeof("/capset.jl");
    char *path = malloc(capacity);
    if (!path) {
        free(library);
        return 1;
    }
    snprintf(path, capacity, "%s/capset.jl", library);
    free(library);

    /* ASan trial workers preload their runtime. Julia's BLAS loader must avoid
     * deep binding in that process, even though this evaluator uses no BLAS.
     * See libblastrampoline/src/dl_utils.c and its sanitizer support. */
    if (dlsym(RTLD_DEFAULT, "__asan_init") &&
        setenv("LBT_USE_RTLD_DEEPBIND", "0", 1) != 0) {
        perror("capset ASan loader configuration");
        free(path);
        return 1;
    }
    jl_init();
    initialized = 1;
    jl_value_t *source = NULL, *loader = NULL;
    JL_GC_PUSH2(&source, &loader);
    source = jl_cstr_to_string(path);
    free(path);
    /* Pass the filename as data, so quotes and backslashes in paths are safe. */
    loader = jl_eval_string("path -> Base.include(Main, path)");
    if (loader && !jl_exception_occurred())
        jl_call1(loader, source);
    if (jl_exception_occurred()) {
        char message[256];
        exception_message(message, sizeof(message));
        JL_GC_POP();
        return 1;
    }
    jl_module_t *module = (jl_module_t *)jl_get_global(jl_main_module, jl_symbol("CapSetEvaluator"));
    score_function = module ? jl_get_function(module, "score_instance") : NULL;
    JL_GC_POP();
    if (!score_function) {
        fprintf(stderr, "capset cannot find Julia score_instance\n");
        return 1;
    }
    timing("fs_init", start);
    return 0;
}

fs_result fs_score(fs_resolve_fn resolve, const char *instance)
{
    fs_result result = { .status = FS_ERROR };
    int32_t requested;
    if (!score_function || parse_instance(instance, &requested) || requested != dimension) {
        snprintf(result.msg, sizeof(result.msg), "uninitialized evaluator or changed instance");
        return result;
    }
    void *fptr = resolve("priority");
    if (!fptr) {
        snprintf(result.msg, sizeof(result.msg), "missing candidate symbol: priority");
        return result;
    }
    double start = seconds();
    jl_value_t *pointer = NULL, *n = NULL, *value = NULL;
    JL_GC_PUSH3(&pointer, &n, &value);
    pointer = jl_box_voidpointer(fptr);
    n = jl_box_int32(dimension);
    value = jl_call2(score_function, pointer, n);
    if (jl_exception_occurred()) {
        result.status = exception_message(result.msg, sizeof(result.msg));
    } else if (value) {
        result.status = (fs_status)jl_unbox_int32(jl_get_nth_field(value, 0));
        result.score = (double)jl_unbox_int64(jl_get_nth_field(value, 1));
        jl_array_t *sizes = (jl_array_t *)jl_get_nth_field(value, 2);
        size_t count = jl_array_len(sizes);
        result.nsig = (int32_t)(count < 8 ? count : 8);
        for (int32_t i = 0; i < result.nsig; ++i)
            result.sig[i] = (double)jl_array_data(sizes, int64_t)[i];
        snprintf(result.msg, sizeof(result.msg), "%s", jl_string_ptr(jl_get_nth_field(value, 3)));
    } else {
        snprintf(result.msg, sizeof(result.msg), "Julia score_instance returned no result");
    }
    JL_GC_POP();
    timing("fs_score", start);
    return result;
}

void fs_fini(void)
{
    if (initialized)
        jl_atexit_hook(0);
    initialized = 0;
    score_function = NULL;
}
