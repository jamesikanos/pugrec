# Built by scripts/build-gpu.sh inside the pugrec-build distrobox (host: no compilers).
PKGS = egl glesv2 gbm libdrm libavcodec libavformat libavfilter libavutil
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wno-unused-parameter $(shell pkg-config --cflags $(PKGS))
LDLIBS = $(shell pkg-config --libs $(PKGS)) -lm

pugrec-gpu: main.c decode.c cJSON.c decode.h shaders.h cJSON.h
	$(CC) $(CFLAGS) -o $@ main.c decode.c cJSON.c $(LDLIBS)

clean:
	rm -f pugrec-gpu
