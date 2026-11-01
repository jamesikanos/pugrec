/* GLSL ES 3.2 sources. Coordinates: image pixels, top-left origin, a pixel's index is its
 * centre (OpenCV's convention, which compose.py's maths assumes). Render-target textures are
 * stored GL bottom-up (fetchImg flips); uploaded/imported textures are top-down (texelFetch). */
#pragma once

static const char *VS_QUAD =
    "#version 320 es\n"
    "uniform vec4 uBox; uniform vec2 uTarget;\n"
    "void main(){\n"
    "  vec2 c = vec2((gl_VertexID & 1) == 0 ? uBox.x : uBox.z, (gl_VertexID & 2) == 0 ? uBox.y : uBox.w);\n"
    "  gl_Position = vec4(c.x / uTarget.x * 2.0 - 1.0, 1.0 - c.y / uTarget.y * 2.0, 0.0, 1.0);\n"
    "}\n";

#define FS_COMMON                                                                                 \
    "#version 320 es\n"                                                                           \
    "precision highp float; precision highp int; precision highp sampler2D;\n"                    \
    "uniform vec2 uTarget;\n"                                                                     \
    "out vec4 o;\n"                                                                               \
    "ivec2 pix(){ return ivec2(int(gl_FragCoord.x), int(uTarget.y) - 1 - int(gl_FragCoord.y)); }\n" \
    "vec4 fetchImg(sampler2D t, ivec2 p){ ivec2 s = textureSize(t, 0); p = clamp(p, ivec2(0), s - 1);\n" \
    "  return texelFetch(t, ivec2(p.x, s.y - 1 - p.y), 0); }\n"                                   \
    "float q8(float v){ return floor(clamp(v, 0.0, 1.0) * 255.0 + 0.5) / 255.0; }\n"              \
    "vec3 q8(vec3 v){ return floor(clamp(v, 0.0, 1.0) * 255.0 + 0.5) / 255.0; }\n"                \
    /* compose.mask() comes as an R8 texture (made by compose.mask itself: exact parity). \
       p is relative to the mask's box; has = 0: no mask (1). */                                  \
    "float maskOf(sampler2D t, int has, ivec2 p, ivec2 sz){\n"                                     \
    "  if (has == 0) return 1.0;\n"                                                               \
    "  if (p.x < 0 || p.y < 0 || p.x >= sz.x || p.y >= sz.y) return 0.0;\n"                       \
    "  return texelFetch(t, p, 0).r;\n"                                                           \
    "}\n"

/* NV12 (dmabuf planes) -> RGB as compose.Decoder's bgr24: the stream's range and matrix (unknown =
 * BT.601, swscale's default), chroma nearest (swscale's unscaled converter; measured). */
static const char *FS_YUV = FS_COMMON
    "uniform sampler2D uY, uUV; uniform int uFull, uBt709;\n"
    "void main(){\n"
    "  ivec2 p = pix();\n"
    "  float y = texelFetch(uY, p, 0).r;\n"
    "  vec2 uv = texelFetch(uUV, p / 2, 0).rg;\n"   /* swscale's same-size yuv420p -> rgb: chroma replicated */
    "  if (uFull == 1) { uv -= 128.0 / 255.0; }\n"
    "  else { y = (y - 16.0 / 255.0) * 255.0 / 219.0; uv = (uv - 128.0 / 255.0) * 255.0 / 224.0; }\n"
    "  vec3 c = uBt709 == 1 ? vec3(y + 1.5748 * uv.y, y - 0.187324 * uv.x - 0.468124 * uv.y, y + 1.8556 * uv.x)\n"
    "                       : vec3(y + 1.402 * uv.y, y - 0.344136 * uv.x - 0.714136 * uv.y, y + 1.772 * uv.x);\n"
    "  o = vec4(clamp(c, 0.0, 1.0), 1.0);\n"
    "}\n";

/* compose.draw_video: crop_scaled (integer crop, INTER_AREA or INTER_LINEAR resize, then a
 * bilinear sub-pixel shift with edge replicate), or the blurred small picture scaled up;
 * then dim, shape mask, opacity and border. Output premultiplied. */
static const char *FS_VIDEO = FS_COMMON
    "uniform sampler2D uSrc;\n"         /* stream RGB (mode 0) or the blurred small picture (mode 1) */
    "uniform int uMode, uPlain;\n"
    "uniform ivec4 uBoxI;\n"            /* x, y, w, h (output px) */
    "uniform ivec4 uCrop;\n"            /* x0, y0, x1, y1 (source px) */
    "uniform ivec2 uSmall;\n"           /* cw, ch */
    "uniform vec2 uShift; uniform int uArea;\n"
    "uniform float uDim, uOp; uniform sampler2D uMask, uIMask; uniform int uHasMask;\n"
    "uniform int uBorder, uIb; uniform vec3 uBRGB;\n"
    "vec3 cropPix(int cx, int cy){ return fetchImg(uSrc, ivec2(uCrop.x + cx, uCrop.y + cy)).rgb; }\n"
    "vec3 small(ivec2 q){\n"
    "  q = clamp(q, ivec2(0), uSmall - 1);\n"
    "  int cwid = uCrop.z - uCrop.x, chei = uCrop.w - uCrop.y;\n"
    "  float ix = float(cwid) / float(uSmall.x), iy = float(chei) / float(uSmall.y);\n"
    "  if (uArea == 1) {\n"
    "    float ax = float(q.x) * ix, bx = min(float(q.x + 1) * ix, float(cwid));\n"
    "    float ay = float(q.y) * iy, by = min(float(q.y + 1) * iy, float(chei));\n"
    "    vec3 s = vec3(0.0); float wsum = 0.0;\n"
    "    for (int yy = int(floor(ay)); yy < int(ceil(by)); yy++) {\n"
    "      float wy = min(by, float(yy + 1)) - max(ay, float(yy));\n"
    "      for (int xx = int(floor(ax)); xx < int(ceil(bx)); xx++) {\n"
    "        float w = wy * (min(bx, float(xx + 1)) - max(ax, float(xx)));\n"
    "        s += w * cropPix(xx, yy); wsum += w; } }\n"
    "    return q8(s / max(wsum, 1e-6));\n"
    "  }\n"
    "  float cx = max((float(q.x) + 0.5) * ix - 0.5, 0.0), cy = max((float(q.y) + 0.5) * iy - 0.5, 0.0);\n"
    "  int x0 = int(floor(cx)), y0 = int(floor(cy)); float fx = cx - float(x0), fy = cy - float(y0);\n"
    "  int x1 = min(x0 + 1, cwid - 1), y1 = min(y0 + 1, chei - 1); x0 = min(x0, cwid - 1); y0 = min(y0, chei - 1);\n"
    "  vec3 a = mix(cropPix(x0, y0), cropPix(x1, y0), fx), b = mix(cropPix(x0, y1), cropPix(x1, y1), fx);\n"
    "  return q8(mix(a, b, fy));\n"
    "}\n"
    "vec3 cropScaled(ivec2 u){\n"
    "  vec2 q = vec2(u) + uShift; ivec2 q0 = ivec2(floor(q)); vec2 f = q - vec2(q0);\n"
    "  vec3 a = mix(small(q0), small(q0 + ivec2(1, 0)), f.x), b = mix(small(q0 + ivec2(0, 1)), small(q0 + ivec2(1, 1)), f.x);\n"
    "  return q8(mix(a, b, f.y));\n"
    "}\n"
    "vec3 upscaled(ivec2 u){\n"   /* cv2.resize INTER_LINEAR of the blurred small picture to w x h */
    "  ivec2 s = textureSize(uSrc, 0);\n"
    "  vec2 c = max((vec2(u) + 0.5) * vec2(s) / vec2(uBoxI.zw) - 0.5, 0.0);\n"
    "  ivec2 a0 = ivec2(floor(c)); vec2 f = c - vec2(a0); ivec2 a1 = min(a0 + 1, s - 1); a0 = min(a0, s - 1);\n"
    "  vec3 a = mix(fetchImg(uSrc, a0).rgb, fetchImg(uSrc, ivec2(a1.x, a0.y)).rgb, f.x);\n"
    "  vec3 b = mix(fetchImg(uSrc, ivec2(a0.x, a1.y)).rgb, fetchImg(uSrc, a1).rgb, f.x);\n"
    "  return q8(mix(a, b, f.y));\n"
    "}\n"
    "void main(){\n"
    "  ivec2 u = pix() - uBoxI.xy;\n"
    "  vec3 v = uMode == 0 ? cropScaled(u) : upscaled(u);\n"
    "  if (uPlain == 1) { o = vec4(v, 1.0); return; }\n"
    "  if (uDim > 0.0) v = q8(v * (1.0 - uDim));\n"
    "  float m = maskOf(uMask, uHasMask, u, uBoxI.zw);\n"
    "  float a1 = m * uOp, a2 = 0.0;\n"
    "  if (uBorder == 1) {\n"
    "    float inner = maskOf(uIMask, 1, u - ivec2(uIb), textureSize(uIMask, 0));\n"
    "    a2 = clamp(m - inner, 0.0, 1.0) * uOp;\n"
    "  }\n"
    "  o = vec4(v * a1 * (1.0 - a2) + uBRGB * a2, 1.0 - (1.0 - a1) * (1.0 - a2));\n"
    "}\n";

/* Separable Gaussian, reflect-101 borders (OpenCV's default). Weights packed 4 per vec4. */
static const char *FS_GAUSS = FS_COMMON
    "uniform sampler2D uIn; uniform int uDir, uK; uniform vec4 uW4[80];\n"
    "int refl(int i, int n){ if (n == 1) return 0; for (int k = 0; k < 4; k++) { if (i < 0) i = -i; if (i >= n) i = 2 * n - 2 - i; } return clamp(i, 0, n - 1); }\n"
    "void main(){\n"
    "  ivec2 p = pix(); ivec2 s = textureSize(uIn, 0); int h = uK / 2; vec4 acc = vec4(0.0);\n"
    "  for (int i = 0; i < uK; i++) {\n"
    "    int off = i - h; ivec2 q = uDir == 0 ? ivec2(refl(p.x + off, s.x), p.y) : ivec2(p.x, refl(p.y + off, s.y));\n"
    "    acc += uW4[i / 4][i % 4] * fetchImg(uIn, q); }\n"
    "  o = acc;\n"
    "}\n";

/* Everything else: fill (also ring/star/stroke: OpenCV's coverage as the mask), gradient,
 * sprite (text/image), shadow, and a plain mask (the shadow's first pass). Premultiplied. */
static const char *FS_PAINT = FS_COMMON
    "uniform int uKind; uniform ivec4 uBoxI; uniform sampler2D uMask; uniform int uHasMask; uniform float uOp; uniform vec3 uRGB;\n"
    "uniform vec3 uStops[4]; uniform int uNStops; uniform vec3 uDir;\n"
    "uniform sampler2D uTex;\n"

    "void main(){\n"
    "  ivec2 u = pix() - uBoxI.xy;\n"
    "  vec3 c = uRGB; float a = 0.0;\n"
    "  if (uKind == 1) { a = maskOf(uMask, uHasMask, u, uBoxI.zw); }\n"                       /* fill */
    "  else if (uKind == 2) {\n"                                                          /* gradient */
    "    vec2 d = vec2(u) + 0.5 - vec2(uBoxI.zw) / 2.0;\n"
    "    float t = clamp((d.x * uDir.x + d.y * uDir.y) / (2.0 * uDir.z) + 0.5, 0.0, 1.0);\n"
    "    float seg = t * float(uNStops - 1); int i = min(int(floor(seg)), uNStops - 2);\n"
    "    c = floor(mix(uStops[i], uStops[i + 1], seg - float(i)) + 0.5) / 255.0;\n"
    "    a = maskOf(uMask, uHasMask, u, uBoxI.zw); }\n"
    "  else if (uKind == 3) {\n"                                                          /* sprite */
    "    vec4 t = texelFetch(uTex, clamp(u, ivec2(0), uBoxI.zw - 1), 0); c = t.rgb;\n"
    "    a = t.a * maskOf(uMask, uHasMask, u, uBoxI.zw); }\n"
    "  else if (uKind == 4) { c = vec3(0.0); a = fetchImg(uTex, u).r; }\n"                /* shadow */
    "  else if (uKind == 8) { o = vec4(maskOf(uMask, uHasMask, u, uBoxI.zw), 0.0, 0.0, 1.0); return; }\n" /* plain mask */
    "  a *= uOp;\n"
    "  o = vec4(c * a, a);\n"
    "}\n";

/* RGB canvas -> NV12, BT.601 limited (what bgr24 -> yuv420p gives today), packed 4 bytes per
 * RGBA texel so that the readback is always RGBA/UNSIGNED_BYTE. Rows top-down in memory. */
static const char *FS_PACKY = FS_COMMON
    "uniform sampler2D uIn;\n"
    "float Y(vec3 c){ return (16.0 + 219.0 * dot(c, vec3(0.299, 0.587, 0.114))) / 255.0; }\n"
    "void main(){\n"
    "  int x = int(gl_FragCoord.x) * 4, y = int(gl_FragCoord.y);\n"
    "  o = vec4(Y(fetchImg(uIn, ivec2(x, y)).rgb), Y(fetchImg(uIn, ivec2(x + 1, y)).rgb),\n"
    "           Y(fetchImg(uIn, ivec2(x + 2, y)).rgb), Y(fetchImg(uIn, ivec2(x + 3, y)).rgb));\n"
    "}\n";

/* swscale's bgr24 -> yuv420p chroma: horizontal pairs averaged (rgbToUV_half), vertical 2:1
 * bicubic (Keys a = -0.6, centred), edge rows replicated. Measured with impulses. */
static const char *FS_PACKUV = FS_COMMON
    "uniform sampler2D uIn;\n"
    "const float VW[8] = float[8](-0.0140625, -0.0421875, 0.1203125, 0.4359375, 0.4359375, 0.1203125, -0.0421875, -0.0140625);\n"
    "vec2 UV(int x, int j){\n"
    "  vec3 c = vec3(0.0); int H = textureSize(uIn, 0).y;\n"
    "  for (int k = 0; k < 8; k++) { int y = clamp(2 * j - 3 + k, 0, H - 1);\n"
    "    c += VW[k] * 0.5 * (fetchImg(uIn, ivec2(x, y)).rgb + fetchImg(uIn, ivec2(x + 1, y)).rgb); }\n"
    "  return vec2(128.0 + 224.0 * dot(c, vec3(-0.168736, -0.331264, 0.5)),\n"
    "              128.0 + 224.0 * dot(c, vec3(0.5, -0.418688, -0.081312))) / 255.0;\n"
    "}\n"
    "void main(){\n"
    "  int x = int(gl_FragCoord.x) * 4, j = int(gl_FragCoord.y);\n"
    "  o = vec4(UV(x, j), UV(x + 2, j));\n"
    "}\n";
