/* pugrec-gpu: paints a pugrec plan on the GPU (the build compositor, second implementation).
 *
 * stdin: JSON lines from pugrec/edit/gpu.py:
 *   {"job": {"w", "h", "fps", "streams": [{"file", "offset"}], "vram_mb"}}   once, first
 *   {"run": {"streams": [idx...], "start": s, "hold": bool}}                 a run of frames
 *   {"f": [layer...]}                                                        one output frame
 * stdout: raw NV12 frames (w x h). stderr: "p <frames>" progress lines and errors.
 * Layers arrive already snapped to pixels by gpu.py (the same rounding as compose.py), so
 * this file only draws. Exit 99 = GPU memory over the limit (the caller falls back to the CPU).
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl32.h>
#include <GLES2/gl2ext.h>
#include <drm_fourcc.h>
#include <fcntl.h>
#include <gbm.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_drm.h>
#include <libavutil/pixdesc.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "cJSON.h"
#include "decode.h"
#include "shaders.h"

#define RENDER_NODE "/dev/dri/renderD128"
#define MAX_STREAMS 16

static void die(const char *msg) {
    fprintf(stderr, "pugrec-gpu: %s\n", msg);
    exit(2);
}

/* ---------------------------------------------------------------- GPU memory guard */

static long long vram_used(void) {
    FILE *f = fopen("/sys/class/drm/renderD128/device/mem_info_vram_used", "r");
    long long v = -1;
    if (f) {
        if (fscanf(f, "%lld", &v) != 1) v = -1;
        fclose(f);
    }
    return v;
}

static long long vram_total(void) {
    FILE *f = fopen("/sys/class/drm/renderD128/device/mem_info_vram_total", "r");
    long long v = -1;
    if (f) {
        if (fscanf(f, "%lld", &v) != 1) v = -1;
        fclose(f);
    }
    return v;
}

static long long g_vram_limit = -1;

static void vram_check(void) {
    long long v = vram_used();
    if (g_vram_limit > 0 && v > g_vram_limit) {
        fprintf(stderr, "pugrec-gpu: GPU memory %lld MB over the limit %lld MB - stopping\n", v >> 20,
                g_vram_limit >> 20);
        fflush(stdout);
        exit(99);
    }
}

/* ---------------------------------------------------------------- EGL / GL */

static EGLDisplay g_dpy;
static PFNEGLCREATEIMAGEKHRPROC p_eglCreateImageKHR;
static PFNEGLDESTROYIMAGEKHRPROC p_eglDestroyImageKHR;
static PFNGLEGLIMAGETARGETTEXTURE2DOESPROC p_glEGLImageTargetTexture2DOES;

static void egl_init(void) {
    int fd = open(RENDER_NODE, O_RDWR | O_CLOEXEC);
    if (fd < 0) die("can't open " RENDER_NODE);
    struct gbm_device *gbm = gbm_create_device(fd);
    if (!gbm) die("gbm_create_device failed");
    PFNEGLGETPLATFORMDISPLAYEXTPROC getdpy = (void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
    g_dpy = getdpy(EGL_PLATFORM_GBM_KHR, gbm, NULL);
    if (!g_dpy || !eglInitialize(g_dpy, NULL, NULL)) die("eglInitialize failed");
    eglBindAPI(EGL_OPENGL_ES_API);
    EGLint attr[] = {EGL_CONTEXT_MAJOR_VERSION, 3, EGL_CONTEXT_MINOR_VERSION, 2, EGL_NONE};
    EGLContext ctx = eglCreateContext(g_dpy, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, attr);
    if (!ctx || !eglMakeCurrent(g_dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) die("no GLES 3.2 context");
    p_eglCreateImageKHR = (void *)eglGetProcAddress("eglCreateImageKHR");
    p_eglDestroyImageKHR = (void *)eglGetProcAddress("eglDestroyImageKHR");
    p_glEGLImageTargetTexture2DOES = (void *)eglGetProcAddress("glEGLImageTargetTexture2DOES");
    if (!p_eglCreateImageKHR || !p_glEGLImageTargetTexture2DOES) die("no dmabuf import (EGL_KHR_image)");
}

static GLuint compile(GLenum type, const char *src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, NULL);
    glCompileShader(s);
    GLint ok;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[4096];
        glGetShaderInfoLog(s, sizeof log, NULL, log);
        fprintf(stderr, "pugrec-gpu: shader:\n%s\n", log);
        exit(2);
    }
    return s;
}

static GLuint program(const char *fs) {
    static GLuint vs;
    if (!vs) vs = compile(GL_VERTEX_SHADER, VS_QUAD);
    GLuint p = glCreateProgram();
    glAttachShader(p, vs);
    glAttachShader(p, compile(GL_FRAGMENT_SHADER, fs));
    glLinkProgram(p);
    GLint ok;
    glGetProgramiv(p, GL_LINK_STATUS, &ok);
    if (!ok) die("program link failed");
    return p;
}

#define U(p, name) glGetUniformLocation(p, name)

typedef struct {
    GLuint tex, fbo;
    int w, h;
    GLenum fmt;
} Target;

static void target_alloc(Target *t, int w, int h, GLenum fmt) {
    if (t->tex && t->w == w && t->h == h && t->fmt == fmt) return;
    if (t->tex) {
        glDeleteTextures(1, &t->tex);
        glDeleteFramebuffers(1, &t->fbo);
    }
    t->w = w, t->h = h, t->fmt = fmt;
    glActiveTexture(GL_TEXTURE7);  /* a spare unit: never disturb a binding a draw is about to use */
    glGenTextures(1, &t->tex);
    glBindTexture(GL_TEXTURE_2D, t->tex);
    glTexStorage2D(GL_TEXTURE_2D, 1, fmt, w, h);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glGenFramebuffers(1, &t->fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, t->fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t->tex, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) die("framebuffer incomplete");
    glActiveTexture(GL_TEXTURE0);
}

static void target_free(Target *t) {
    if (!t->tex) return;
    glDeleteTextures(1, &t->tex);
    glDeleteFramebuffers(1, &t->fbo);
    memset(t, 0, sizeof *t);
}

/* Bind a target and a program, set the common uniforms. */
static void use(Target *t, GLuint prog) {
    glBindFramebuffer(GL_FRAMEBUFFER, t->fbo);
    glViewport(0, 0, t->w, t->h);
    glUseProgram(prog);
    glUniform2f(U(prog, "uTarget"), (float)t->w, (float)t->h);
}

static void quad(GLuint prog, float x0, float y0, float x1, float y1) {
    glUniform4f(U(prog, "uBox"), x0, y0, x1, y1);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
}

static GLuint P_YUV, P_VIDEO, P_GAUSS, P_PAINT, P_PACKY, P_PACKUV;

/* ---------------------------------------------------------------- JSON helpers */

static double num(const cJSON *o, const char *k, double def) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    return cJSON_IsNumber(v) ? v->valuedouble : def;
}

static int inum(const cJSON *o, const char *k, int def) { return (int)lround(num(o, k, def)); }

static void arr(const cJSON *o, const char *k, double *out, int n) {
    const cJSON *a = cJSON_GetObjectItemCaseSensitive(o, k);
    for (int i = 0; i < n; i++) {
        const cJSON *v = cJSON_GetArrayItem(a, i);
        out[i] = cJSON_IsNumber(v) ? v->valuedouble : 0;
    }
}

static void iarr(const cJSON *o, const char *k, int *out, int n) {
    double t[16];
    arr(o, k, t, n);
    for (int i = 0; i < n; i++) out[i] = (int)lround(t[i]);
}

/* ---------------------------------------------------------------- Gaussian kernels (OpenCV) */

/* cv::getGaussianKernel: the fixed small kernels when sigma <= 0 and k <= 7, else sampled. */
static int gauss_kernel(int k, double sigma, float *w) {
    static const float tab[4][7] = {{1.f}, {0.25f, 0.5f, 0.25f}, {0.0625f, 0.25f, 0.375f, 0.25f, 0.0625f},
                                    {0.03125f, 0.109375f, 0.21875f, 0.28125f, 0.21875f, 0.109375f, 0.03125f}};
    if (k > 319) k = 319;
    if (k <= 7 && sigma <= 0) {
        memcpy(w, tab[k / 2], k * sizeof(float));
        return k;
    }
    double s = sigma > 0 ? sigma : ((k - 1) * 0.5 - 1) * 0.3 + 0.8, sum = 0, tmp[320];
    for (int i = 0; i < k; i++) {
        double x = i - (k - 1) * 0.5;
        tmp[i] = exp(-x * x / (2 * s * s));
        sum += tmp[i];
    }
    for (int i = 0; i < k; i++) w[i] = (float)(tmp[i] / sum);
    return k;
}

static void gauss_pass(Target *dst, Target *src, int dir, int k, const float *w) {
    use(dst, P_GAUSS);
    glDisable(GL_BLEND);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, src->tex);
    glUniform1i(U(P_GAUSS, "uIn"), 0);
    glUniform1i(U(P_GAUSS, "uDir"), dir);
    glUniform1i(U(P_GAUSS, "uK"), k);
    float w4[320] = {0};
    memcpy(w4, w, k * sizeof(float));
    glUniform4fv(U(P_GAUSS, "uW4"), 80, w4);
    quad(P_GAUSS, 0, 0, dst->w, dst->h);
}

/* ---------------------------------------------------------------- streams */

typedef struct {
    char *file;
    double offset;
    Decoder *dec;
    Target rgb;       /* the current picture, RGB, full source size */
    int has;          /* a picture is there */
} Stream;

static Stream g_streams[MAX_STREAMS];
static int g_nstreams;
static AVBufferRef *g_hwdev;

/* Import a VAAPI frame's planes (dmabuf, zero copy) and convert to the stream's RGB target. */
static void upload_frame(Stream *s, AVFrame *f) {
    AVFrame *drm = av_frame_alloc();
    drm->format = AV_PIX_FMT_DRM_PRIME;
    if (av_hwframe_map(drm, f, AV_HWFRAME_MAP_READ) < 0) die("can't map a VAAPI frame to DRM PRIME");
    const AVDRMFrameDescriptor *d = (const AVDRMFrameDescriptor *)drm->data[0];
    int w = f->width, h = f->height;
    /* Two images: luma (R8, w x h) and chroma (GR88, w/2 x h/2), from either layout. */
    struct { int obj; ptrdiff_t off, pitch; } pl[2];
    if (d->nb_layers >= 2) {
        for (int i = 0; i < 2; i++)
            pl[i].obj = d->layers[i].planes[0].object_index, pl[i].off = d->layers[i].planes[0].offset,
            pl[i].pitch = d->layers[i].planes[0].pitch;
    } else if (d->nb_layers == 1 && d->layers[0].nb_planes >= 2) {
        for (int i = 0; i < 2; i++)
            pl[i].obj = d->layers[0].planes[i].object_index, pl[i].off = d->layers[0].planes[i].offset,
            pl[i].pitch = d->layers[0].planes[i].pitch;
    } else {
        die("unexpected DRM frame layout (not NV12)");
    }
    EGLImageKHR img[2];
    GLuint tex[2];
    glGenTextures(2, tex);
    for (int i = 0; i < 2; i++) {
        const AVDRMObjectDescriptor *o = &d->objects[pl[i].obj];
        EGLint a[32], n = 0;
        a[n++] = EGL_WIDTH, a[n++] = i ? (w + 1) / 2 : w;
        a[n++] = EGL_HEIGHT, a[n++] = i ? (h + 1) / 2 : h;
        a[n++] = EGL_LINUX_DRM_FOURCC_EXT, a[n++] = i ? DRM_FORMAT_GR88 : DRM_FORMAT_R8;
        a[n++] = EGL_DMA_BUF_PLANE0_FD_EXT, a[n++] = o->fd;
        a[n++] = EGL_DMA_BUF_PLANE0_OFFSET_EXT, a[n++] = (EGLint)pl[i].off;
        a[n++] = EGL_DMA_BUF_PLANE0_PITCH_EXT, a[n++] = (EGLint)pl[i].pitch;
        if (o->format_modifier != DRM_FORMAT_MOD_INVALID) {
            a[n++] = EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT, a[n++] = (EGLint)(o->format_modifier & 0xffffffff);
            a[n++] = EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT, a[n++] = (EGLint)(o->format_modifier >> 32);
        }
        a[n++] = EGL_NONE;
        img[i] = p_eglCreateImageKHR(g_dpy, EGL_NO_CONTEXT, EGL_LINUX_DMA_BUF_EXT, NULL, a);
        if (img[i] == EGL_NO_IMAGE_KHR) die("eglCreateImage (dmabuf import) failed");
        glActiveTexture(GL_TEXTURE7);
        glBindTexture(GL_TEXTURE_2D, tex[i]);
        p_glEGLImageTargetTexture2DOES(GL_TEXTURE_2D, img[i]);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    }
    target_alloc(&s->rgb, w, h, GL_RGBA8);
    use(&s->rgb, P_YUV);
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, tex[0]);
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, tex[1]);
    glUniform1i(U(P_YUV, "uY"), 0);
    glUniform1i(U(P_YUV, "uUV"), 1);
    glUniform1i(U(P_YUV, "uFull"), f->color_range == AVCOL_RANGE_JPEG);
    glUniform1i(U(P_YUV, "uBt709"), f->colorspace == AVCOL_SPC_BT709);
    quad(P_YUV, 0, 0, w, h);
    glActiveTexture(GL_TEXTURE0);
    glDeleteTextures(2, tex);
    for (int i = 0; i < 2; i++) p_eglDestroyImageKHR(g_dpy, img[i]);
    av_frame_free(&drm);
    s->has = 1;
}

/* ---------------------------------------------------------------- caches: sprites, shadows */

typedef struct {
    char *key;
    GLuint tex;
    Target t;
    int w, h;
    long long bytes, used;
} Entry;

#define MAX_SPRITES 512
static Entry g_sprites[MAX_SPRITES];
static long long g_sprite_bytes, g_tick;
#define SPRITE_CAP (256LL << 20)

static Entry *sprite_get(const char *file, int w, int h, int ch) {
    for (int i = 0; i < MAX_SPRITES; i++)
        if (g_sprites[i].key && !strcmp(g_sprites[i].key, file)) {
            g_sprites[i].used = ++g_tick;
            return &g_sprites[i];
        }
    long long bytes = (long long)w * h * ch;
    for (;;) {  /* evict least recently used until it fits */
        int n = 0, lru = -1;
        for (int i = 0; i < MAX_SPRITES; i++)
            if (g_sprites[i].key) {
                n++;
                if (lru < 0 || g_sprites[i].used < g_sprites[lru].used) lru = i;
            }
        if ((g_sprite_bytes + bytes <= SPRITE_CAP && n < MAX_SPRITES) || lru < 0) break;
        glDeleteTextures(1, &g_sprites[lru].tex);
        g_sprite_bytes -= g_sprites[lru].bytes;
        free(g_sprites[lru].key);
        memset(&g_sprites[lru], 0, sizeof(Entry));
    }
    Entry *e = NULL;
    for (int i = 0; i < MAX_SPRITES && !e; i++)
        if (!g_sprites[i].key) e = &g_sprites[i];
    unsigned char *buf = malloc(bytes);
    FILE *f = fopen(file, "rb");
    if (!f || fread(buf, 1, bytes, f) != (size_t)bytes) die("can't read a sprite file");
    fclose(f);
    glActiveTexture(GL_TEXTURE7);  /* a spare unit (see target_alloc) */
    glGenTextures(1, &e->tex);
    glBindTexture(GL_TEXTURE_2D, e->tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    if (ch == 1)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_R8, w, h, 0, GL_RED, GL_UNSIGNED_BYTE, buf);
    else
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, buf);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    free(buf);
    glActiveTexture(GL_TEXTURE0);
    e->key = strdup(file);
    e->w = w, e->h = h, e->bytes = bytes, e->used = ++g_tick;
    g_sprite_bytes += bytes;
    return e;
}

/* A layer's mask (an R8 file from compose.mask, the size of its box) on texture unit `unit`. */
static void bind_mask(GLuint prog, const cJSON *l, const char *key, const char *sampler, const char *has, int unit,
                      int w, int h) {
    const char *file = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(l, key));
    if (has) glUniform1i(U(prog, has), file != NULL);
    glUniform1i(U(prog, sampler), unit);
    if (!file) return;
    Entry *e = sprite_get(file, w, h, 1);
    glActiveTexture(GL_TEXTURE0 + unit);
    glBindTexture(GL_TEXTURE_2D, e->tex);
    glActiveTexture(GL_TEXTURE0);
}

#define MAX_SHADOWS 16
static Entry g_shadows[MAX_SHADOWS];

/* compose.draw_shadow's alpha: the card's rounded mask in a (w+2m) x (h+2m) float buffer,
 * Gaussian-blurred (sigma, OpenCV's float kernel size), cached per key like _shadows. */
static Target *shadow_get(const char *key, const cJSON *l, int w, int h, int m, double sigma) {
    int lru = 0;
    for (int i = 0; i < MAX_SHADOWS; i++) {
        if (g_shadows[i].key && !strcmp(g_shadows[i].key, key)) {
            g_shadows[i].used = ++g_tick;
            return &g_shadows[i].t;
        }
        if (g_shadows[i].used < g_shadows[lru].used) lru = i;
    }
    Entry *e = &g_shadows[lru];
    free(e->key);
    target_free(&e->t);
    e->key = strdup(key);
    e->used = ++g_tick;
    int W = w + 2 * m, H = h + 2 * m;
    static Target tmp;
    target_alloc(&e->t, W, H, GL_RGBA16F);
    target_alloc(&tmp, W, H, GL_RGBA16F);
    use(&e->t, P_PAINT);
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);
    glUniform1i(U(P_PAINT, "uKind"), 8);
    glUniform4i(U(P_PAINT, "uBoxI"), m, m, w, h);
    bind_mask(P_PAINT, l, "mask", "uMask", "uHasMask", 1, w, h);
    quad(P_PAINT, 0, 0, W, H);
    float wts[320];
    int k = gauss_kernel((int)lrint(sigma * 8 + 1) | 1, sigma, wts);
    gauss_pass(&tmp, &e->t, 0, k, wts);
    gauss_pass(&e->t, &tmp, 1, k, wts);
    return &e->t;
}

/* ---------------------------------------------------------------- drawing */

static Target g_canvas, g_small, g_small2, g_packy, g_packuv;
static int g_W, g_H;

static void blend_on(void) {
    glEnable(GL_BLEND);
    glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ZERO, GL_ONE);
}

static void set_clip(const cJSON *l) {
    const cJSON *c = cJSON_GetObjectItemCaseSensitive(l, "clip");
    glBindFramebuffer(GL_FRAMEBUFFER, g_canvas.fbo);
    if (!c) {
        glDisable(GL_SCISSOR_TEST);
        return;
    }
    int r[4];
    iarr(l, "clip", r, 4);  /* x0, y0, x1, y1, image px */
    glEnable(GL_SCISSOR_TEST);
    glScissor(r[0], g_H - r[3], r[2] - r[0], r[3] - r[1]);
}

static void set_crop(const cJSON *c) {
    /* [x0, y0, x1, y1, cw, ch, shift_x, shift_y, area] from gpu.crop_params (compose.crop_scaled) */
    double v[9];
    for (int i = 0; i < 9; i++) v[i] = cJSON_GetArrayItem(c, i)->valuedouble;
    glUniform4i(U(P_VIDEO, "uCrop"), (int)v[0], (int)v[1], (int)v[2], (int)v[3]);
    glUniform2i(U(P_VIDEO, "uSmall"), (int)v[4], (int)v[5]);
    glUniform2f(U(P_VIDEO, "uShift"), (float)v[6], (float)v[7]);
    glUniform1i(U(P_VIDEO, "uArea"), (int)v[8]);
}

static void draw_video(const cJSON *l) {
    int s = inum(l, "s", -1);
    if (s < 0 || s >= g_nstreams || !g_streams[s].has) return;
    int box[4];
    iarr(l, "box", box, 4);
    const cJSON *blur = cJSON_GetObjectItemCaseSensitive(l, "blur");
    GLuint src = g_streams[s].rgb.tex;
    if (blur) {
        /* quarter-size crop, Gaussian (OpenCV kernel for k), then scaled up in the final pass */
        int sz[2];
        iarr(blur, "size", sz, 2);
        target_alloc(&g_small, sz[0], sz[1], GL_RGBA8);
        target_alloc(&g_small2, sz[0], sz[1], GL_RGBA16F);
        glDisable(GL_SCISSOR_TEST);
        use(&g_small, P_VIDEO);
        glDisable(GL_BLEND);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, src);
        glUniform1i(U(P_VIDEO, "uSrc"), 0);
        glUniform1i(U(P_VIDEO, "uMode"), 0);
        glUniform1i(U(P_VIDEO, "uPlain"), 1);
        glUniform4i(U(P_VIDEO, "uBoxI"), 0, 0, sz[0], sz[1]);
        set_crop(cJSON_GetObjectItemCaseSensitive(blur, "crop"));
        quad(P_VIDEO, 0, 0, sz[0], sz[1]);
        float w[320];
        int k = gauss_kernel(inum(blur, "k", 3), 0, w);
        gauss_pass(&g_small2, &g_small, 0, k, w);
        gauss_pass(&g_small, &g_small2, 1, k, w);
        src = g_small.tex;
    }
    use(&g_canvas, P_VIDEO);
    set_clip(l);
    blend_on();
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, src);
    glUniform1i(U(P_VIDEO, "uSrc"), 0);
    glUniform1i(U(P_VIDEO, "uMode"), blur ? 1 : 0);
    glUniform1i(U(P_VIDEO, "uPlain"), 0);
    glUniform4i(U(P_VIDEO, "uBoxI"), box[0], box[1], box[2], box[3]);
    if (!blur) set_crop(cJSON_GetObjectItemCaseSensitive(l, "crop"));
    bind_mask(P_VIDEO, l, "mask", "uMask", "uHasMask", 1, box[2], box[3]);
    glUniform1f(U(P_VIDEO, "uDim"), (float)num(l, "dim", 0));
    glUniform1f(U(P_VIDEO, "uOp"), (float)num(l, "op", 1));
    const cJSON *b = cJSON_GetObjectItemCaseSensitive(l, "border");
    glUniform1i(U(P_VIDEO, "uBorder"), b != NULL);
    if (b) {
        double v[4];
        for (int i = 0; i < 4; i++) v[i] = cJSON_GetArrayItem(b, i)->valuedouble;  /* ib, r, g, b */
        int ib = (int)v[0];
        glUniform1i(U(P_VIDEO, "uIb"), ib);
        glUniform3f(U(P_VIDEO, "uBRGB"), v[1] / 255, v[2] / 255, v[3] / 255);
        int iw = box[2] - 2 * ib, ih = box[3] - 2 * ib;
        bind_mask(P_VIDEO, l, "imask", "uIMask", NULL, 2, iw < 1 ? 1 : iw, ih < 1 ? 1 : ih);
    } else {
        glUniform3f(U(P_VIDEO, "uBRGB"), 0, 0, 0);
    }
    quad(P_VIDEO, box[0], box[1], box[0] + box[2], box[1] + box[3]);
}

static void paint_common(const cJSON *l, int kind, int bx, int by, int bw, int bh) {
    use(&g_canvas, P_PAINT);
    set_clip(l);
    blend_on();
    glUniform1i(U(P_PAINT, "uKind"), kind);
    glUniform4i(U(P_PAINT, "uBoxI"), bx, by, bw, bh);
    bind_mask(P_PAINT, l, kind == 4 ? "-" : "mask", "uMask", "uHasMask", 1, bw, bh);
    glUniform1f(U(P_PAINT, "uOp"), (float)num(l, "op", 1));
    double c[3] = {0, 0, 0};
    if (cJSON_GetObjectItemCaseSensitive(l, "rgb")) arr(l, "rgb", c, 3);
    glUniform3f(U(P_PAINT, "uRGB"), c[0] / 255, c[1] / 255, c[2] / 255);
}

static void draw_layer(const cJSON *l) {
    const char *k = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(l, "k"));
    if (!k) return;
    int b[4];
    if (!strcmp(k, "video")) {
        draw_video(l);
    } else if (!strcmp(k, "fill")) {
        iarr(l, "box", b, 4);
        paint_common(l, 1, b[0], b[1], b[2], b[3]);
        quad(P_PAINT, b[0], b[1], b[0] + b[2], b[1] + b[3]);
    } else if (!strcmp(k, "grad")) {
        iarr(l, "box", b, 4);
        paint_common(l, 2, b[0], b[1], b[2], b[3]);
        const cJSON *st = cJSON_GetObjectItemCaseSensitive(l, "stops");
        int n = cJSON_GetArraySize(st);
        if (n > 4) n = 4;
        float s[12] = {0};
        for (int i = 0; i < n; i++)
            for (int j = 0; j < 3; j++) s[i * 3 + j] = (float)cJSON_GetArrayItem(cJSON_GetArrayItem(st, i), j)->valuedouble;
        glUniform3fv(U(P_PAINT, "uStops"), 4, s);
        glUniform1i(U(P_PAINT, "uNStops"), n);
        double d[3];
        arr(l, "dir", d, 3);
        glUniform3f(U(P_PAINT, "uDir"), d[0], d[1], d[2]);
        quad(P_PAINT, b[0], b[1], b[0] + b[2], b[1] + b[3]);
    } else if (!strcmp(k, "sprite")) {
        iarr(l, "box", b, 4);
        Entry *e = sprite_get(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(l, "file")), b[2], b[3], 4);
        paint_common(l, 3, b[0], b[1], b[2], b[3]);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, e->tex);
        glUniform1i(U(P_PAINT, "uTex"), 0);
        quad(P_PAINT, b[0], b[1], b[0] + b[2], b[1] + b[3]);
    } else if (!strcmp(k, "shadow")) {
        int c[3];  /* card w, h, margin m */
        iarr(l, "box", b, 4);
        iarr(l, "card", c, 3);
        Target *t = shadow_get(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(l, "key")), l, c[0], c[1], c[2],
                               num(l, "sigma", 1));
        paint_common(l, 4, b[0], b[1], b[2], b[3]);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, t->tex);
        glUniform1i(U(P_PAINT, "uTex"), 0);
        quad(P_PAINT, b[0], b[1], b[0] + b[2], b[1] + b[3]);
    }
}

/* ---------------------------------------------------------------- output */

static unsigned char *g_out;

static void emit_frame(void) {
    glDisable(GL_SCISSOR_TEST);
    glDisable(GL_BLEND);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, g_canvas.tex);
    use(&g_packy, P_PACKY);
    glUniform1i(U(P_PACKY, "uIn"), 0);
    quad(P_PACKY, 0, 0, g_packy.w, g_packy.h);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, g_packy.w, g_packy.h, GL_RGBA, GL_UNSIGNED_BYTE, g_out);
    use(&g_packuv, P_PACKUV);
    glUniform1i(U(P_PACKUV, "uIn"), 0);
    quad(P_PACKUV, 0, 0, g_packuv.w, g_packuv.h);
    glReadPixels(0, 0, g_packuv.w, g_packuv.h, GL_RGBA, GL_UNSIGNED_BYTE, g_out + (size_t)g_W * g_H);
    size_t n = (size_t)g_W * g_H * 3 / 2;
    if (fwrite(g_out, 1, n, stdout) != n) die("stdout closed (the encoder stopped)");
}

/* ---------------------------------------------------------------- main loop */

static void close_decoders(void) {
    for (int i = 0; i < g_nstreams; i++) {
        dec_close(g_streams[i].dec);
        g_streams[i].dec = NULL;
    }
}

int main(void) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t len;
    long long frames = 0;
    int fps = 0, hold = 0;
    int active[MAX_STREAMS], nactive = 0;
    setvbuf(stdout, NULL, _IOFBF, 1 << 22);

    while ((len = getline(&line, &cap, stdin)) > 0) {
        cJSON *msg = cJSON_Parse(line);
        if (!msg) die("bad JSON line on stdin");
        const cJSON *job = cJSON_GetObjectItemCaseSensitive(msg, "job");
        const cJSON *run = cJSON_GetObjectItemCaseSensitive(msg, "run");
        const cJSON *f = cJSON_GetObjectItemCaseSensitive(msg, "f");
        if (job) {
            g_W = inum(job, "w", 1920), g_H = inum(job, "h", 1080), fps = inum(job, "fps", 60);
            if (g_W % 4 || g_H % 2) die("output width must be a multiple of 4 and height even");
            long long base = vram_used(), total = vram_total(), lim = (long long)num(job, "vram_mb", 2048) << 20;
            if (base > 0) {
                g_vram_limit = base + lim;
                if (total > 0 && g_vram_limit > total * 3 / 4) g_vram_limit = total * 3 / 4;
            }
            egl_init();
            P_YUV = program(FS_YUV), P_VIDEO = program(FS_VIDEO), P_GAUSS = program(FS_GAUSS);
            P_PAINT = program(FS_PAINT), P_PACKY = program(FS_PACKY), P_PACKUV = program(FS_PACKUV);
            target_alloc(&g_canvas, g_W, g_H, GL_RGBA8);
            target_alloc(&g_packy, g_W / 4, g_H, GL_RGBA8);
            target_alloc(&g_packuv, g_W / 4, g_H / 2, GL_RGBA8);
            g_out = malloc((size_t)g_W * g_H * 3 / 2);
            const cJSON *ss = cJSON_GetObjectItemCaseSensitive(job, "streams"), *s;
            cJSON_ArrayForEach(s, ss) {
                if (g_nstreams == MAX_STREAMS) die("too many streams");
                g_streams[g_nstreams].file = strdup(cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(s, "file")));
                g_streams[g_nstreams].offset = num(s, "offset", 0);
                g_nstreams++;
            }
            if (av_hwdevice_ctx_create(&g_hwdev, AV_HWDEVICE_TYPE_VAAPI, RENDER_NODE, NULL, 0) < 0)
                die("no VAAPI device");
        } else if (run) {
            close_decoders();
            nactive = 0;
            hold = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(run, "hold"));
            double start = num(run, "start", 0);
            const cJSON *ids = cJSON_GetObjectItemCaseSensitive(run, "streams"), *v;
            for (int i = 0; i < g_nstreams; i++) g_streams[i].has = 0;
            cJSON_ArrayForEach(v, ids) {
                int i = (int)v->valuedouble;
                if (i < 0 || i >= g_nstreams) die("bad stream index");
                Stream *s = &g_streams[i];
                s->dec = dec_open(s->file, s->offset, start, fps, g_hwdev);
                if (!s->dec) exit(2);
                active[nactive++] = i;
                if (hold) {  /* one picture, decoded once, under every frame of the hold */
                    int ch;
                    AVFrame *fr = dec_next(s->dec, &ch);
                    if (fr) upload_frame(s, fr);
                }
            }
        } else if (f) {
            if (!hold)
                for (int i = 0; i < nactive; i++) {
                    Stream *s = &g_streams[active[i]];
                    int ch;
                    AVFrame *fr = dec_next(s->dec, &ch);
                    if (fr && (ch || !s->has)) upload_frame(s, fr);
                }
            glBindFramebuffer(GL_FRAMEBUFFER, g_canvas.fbo);
            glDisable(GL_SCISSOR_TEST);
            glClearColor(0, 0, 0, 1);
            glClear(GL_COLOR_BUFFER_BIT);
            const cJSON *l;
            cJSON_ArrayForEach(l, f) draw_layer(l);
            emit_frame();
            if (++frames % 30 == 0) {
                fprintf(stderr, "p %lld\n", frames);
                vram_check();
            }
        }
        cJSON_Delete(msg);
    }
    close_decoders();
    fflush(stdout);
    fprintf(stderr, "p %lld\n", frames);
    return 0;
}
