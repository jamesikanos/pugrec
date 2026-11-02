/* One stream decoded on the GPU (VAAPI), on the session clock, at the output fps:
 * the same frame choice as compose.Decoder's ffmpeg command, because it is the same
 * filters (setpts + fps start_time) running on hardware frames. */
#pragma once
#include <libavutil/buffer.h>
#include <libavutil/frame.h>

typedef struct Decoder Decoder;

/* start: session seconds of output frame 0; offset: the stream's first frame on the session
 * clock (camera), as in the plan's streams. Returns NULL (and prints why) on failure. */
Decoder *dec_open(const char *file, double offset, double start, int fps, AVBufferRef *hwdev);

/* The next output frame (a VAAPI AVFrame, owned by the decoder, valid until the next call),
 * or NULL if the stream has produced no frame at all. Past the end: the last frame again.
 * *changed = 0 when it's the same picture as the previous call. */
AVFrame *dec_next(Decoder *d, int *changed);

void dec_close(Decoder *d);
