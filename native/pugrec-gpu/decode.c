#include "decode.h"

#include <libavcodec/avcodec.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavformat/avformat.h>
#include <libavutil/opt.h>
#include <libavutil/pixdesc.h>
#include <stdio.h>

struct Decoder {
    AVFormatContext *fmt;
    AVCodecContext *dec;
    int vidx;
    double offset, start;
    int fps;
    AVFilterGraph *graph;
    AVFilterContext *src, *sink;
    AVPacket *pkt;
    AVFrame *dframe, *out, *last;
    int dec_eof, out_eof;
};

static enum AVPixelFormat get_vaapi(AVCodecContext *c, const enum AVPixelFormat *fmts) {
    (void)c;
    for (const enum AVPixelFormat *p = fmts; *p != AV_PIX_FMT_NONE; p++)
        if (*p == AV_PIX_FMT_VAAPI) return *p;
    fprintf(stderr, "pugrec-gpu: the decoder offers no VAAPI format\n");
    return AV_PIX_FMT_NONE;
}

Decoder *dec_open(const char *file, double offset, double start, int fps, AVBufferRef *hwdev) {
    Decoder *d = av_mallocz(sizeof *d);
    d->offset = offset;
    d->start = start;
    d->fps = fps;
    int r;
    if ((r = avformat_open_input(&d->fmt, file, NULL, NULL)) < 0 || (r = avformat_find_stream_info(d->fmt, NULL)) < 0) {
        fprintf(stderr, "pugrec-gpu: can't open %s (%s)\n", file, av_err2str(r));
        goto fail;
    }
    const AVCodec *codec = NULL;
    d->vidx = av_find_best_stream(d->fmt, AVMEDIA_TYPE_VIDEO, -1, -1, &codec, 0);
    if (d->vidx < 0) {
        fprintf(stderr, "pugrec-gpu: no video stream in %s\n", file);
        goto fail;
    }
    /* The codec with a VAAPI hwaccel (for AV1 that's "av1", not libdav1d, which comes first). */
    codec = NULL;
    void *it = NULL;
    for (const AVCodec *c; (c = av_codec_iterate(&it));) {
        if (!av_codec_is_decoder(c) || c->id != d->fmt->streams[d->vidx]->codecpar->codec_id) continue;
        for (int i = 0;; i++) {
            const AVCodecHWConfig *hc = avcodec_get_hw_config(c, i);
            if (!hc) break;
            if (hc->device_type == AV_HWDEVICE_TYPE_VAAPI && (hc->methods & AV_CODEC_HW_CONFIG_METHOD_HW_DEVICE_CTX)) {
                codec = c;
                break;
            }
        }
        if (codec) break;
    }
    if (!codec) {
        fprintf(stderr, "pugrec-gpu: no VAAPI decoder for %s\n", file);
        goto fail;
    }
    for (int i = 0; i < (int)d->fmt->nb_streams; i++)
        if (i != d->vidx) d->fmt->streams[i]->discard = AVDISCARD_ALL;
    d->dec = avcodec_alloc_context3(codec);
    avcodec_parameters_to_context(d->dec, d->fmt->streams[d->vidx]->codecpar);
    d->dec->hw_device_ctx = av_buffer_ref(hwdev);
    d->dec->get_format = get_vaapi;
    d->dec->extra_hw_frames = 4;  /* the fps filter and dec->last hold a few surfaces */
    d->dec->pkt_timebase = d->fmt->streams[d->vidx]->time_base;
    if ((r = avcodec_open2(d->dec, codec, NULL)) < 0) {
        fprintf(stderr, "pugrec-gpu: can't open the decoder for %s (%s)\n", file, av_err2str(r));
        goto fail;
    }
    /* Like ffmpeg's input -ss (relative to the file's start_time, as ffmpeg does) with -copyts:
     * seek to a keyframe a second before, keep the file's own timestamps. */
    double seek = start - offset - 1.0;
    if (seek > 0) {
        int64_t ts = (int64_t)(seek * AV_TIME_BASE);
        if (d->fmt->start_time != AV_NOPTS_VALUE) ts += d->fmt->start_time;
        avformat_seek_file(d->fmt, -1, INT64_MIN, ts, ts, 0);
    }
    d->pkt = av_packet_alloc();
    d->dframe = av_frame_alloc();
    d->out = av_frame_alloc();
    return d;
fail:
    dec_close(d);
    return NULL;
}

static void push(Decoder *d, AVFrame *f) {
    int r = av_buffersrc_add_frame(d->src, f);
    if (r < 0) fprintf(stderr, "pugrec-gpu: filter input: %s\n", av_err2str(r));
}

static int init_graph(Decoder *d, AVFrame *f) {
    AVStream *st = d->fmt->streams[d->vidx];
    char desc[256];
    int r;
    d->graph = avfilter_graph_alloc();
    /* A hardware-frame buffer source needs its frames context before init: set params, then init. */
    d->src = avfilter_graph_alloc_filter(d->graph, avfilter_get_by_name("buffer"), "in");
    AVBufferSrcParameters *par = av_buffersrc_parameters_alloc();
    par->format = f->format;
    par->width = f->width;
    par->height = f->height;
    par->time_base = st->time_base;
    par->sample_aspect_ratio = (AVRational){1, 1};
    par->hw_frames_ctx = f->hw_frames_ctx;
    par->color_range = f->color_range;
    par->color_space = f->colorspace;
    r = av_buffersrc_parameters_set(d->src, par);
    av_free(par);
    if (r < 0 || (r = avfilter_init_dict(d->src, NULL)) < 0) return r;
    if ((r = avfilter_graph_create_filter(&d->sink, avfilter_get_by_name("buffersink"), "out", NULL, NULL, d->graph)) < 0)
        return r;
    /* compose.Decoder: -vf setpts=PTS+off/TB,fps=fps:start_time=start */
    snprintf(desc, sizeof desc, "setpts=PTS+%.6f/TB,fps=%d:start_time=%.6f", d->offset, d->fps, d->start);
    AVFilterInOut *outs = avfilter_inout_alloc(), *ins = avfilter_inout_alloc();
    outs->name = av_strdup("in");
    outs->filter_ctx = d->src;
    ins->name = av_strdup("out");
    ins->filter_ctx = d->sink;
    r = avfilter_graph_parse_ptr(d->graph, desc, &ins, &outs, NULL);
    avfilter_inout_free(&ins);
    avfilter_inout_free(&outs);
    if (r < 0) return r;
    return avfilter_graph_config(d->graph, NULL);
}

AVFrame *dec_next(Decoder *d, int *changed) {
    *changed = 0;
    for (;;) {
        if (d->graph && !d->out_eof) {
            int r = av_buffersink_get_frame(d->sink, d->out);
            if (r >= 0) {
                if (!d->last) d->last = av_frame_alloc();
                av_frame_unref(d->last);
                av_frame_move_ref(d->last, d->out);
                *changed = 1;
                return d->last;
            }
            if (r == AVERROR_EOF) {
                d->out_eof = 1;
                return d->last;  /* ended early: hold the last frame, like compose.Decoder */
            }
        }
        if (d->out_eof || (d->dec_eof && !d->graph)) return d->last;
        int r = avcodec_receive_frame(d->dec, d->dframe);
        if (r >= 0) {
            d->dframe->pts = d->dframe->best_effort_timestamp;
            if (!d->graph && (r = init_graph(d, d->dframe)) < 0) {
                fprintf(stderr, "pugrec-gpu: filter graph: %s\n", av_err2str(r));
                d->dec_eof = d->out_eof = 1;
                return d->last;
            }
            push(d, d->dframe);
            av_frame_unref(d->dframe);
            continue;
        }
        if (r == AVERROR_EOF) {
            d->dec_eof = 1;
            if (d->graph) push(d, NULL);
            continue;
        }
        if (r != AVERROR(EAGAIN)) {
            fprintf(stderr, "pugrec-gpu: decode error: %s\n", av_err2str(r));
            d->dec_eof = 1;
            if (d->graph) push(d, NULL);
            continue;
        }
        r = av_read_frame(d->fmt, d->pkt);
        if (r < 0) {
            avcodec_send_packet(d->dec, NULL);
        } else {
            if (d->pkt->stream_index == d->vidx) avcodec_send_packet(d->dec, d->pkt);
            av_packet_unref(d->pkt);
        }
    }
}

void dec_close(Decoder *d) {
    if (!d) return;
    avfilter_graph_free(&d->graph);
    av_frame_free(&d->last);
    av_frame_free(&d->out);
    av_frame_free(&d->dframe);
    av_packet_free(&d->pkt);
    avcodec_free_context(&d->dec);
    avformat_close_input(&d->fmt);
    av_free(d);
}
