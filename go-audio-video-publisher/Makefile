APP := go-audio-video-publisher
SDK_DIR := ../server-custom-llm/go-audio-subscriber/sdk

.PHONY: build build-all run clean

build:
	CGO_ENABLED=1 go build -o bin/$(APP) .

build-subscribe-audio:
	CGO_ENABLED=1 go build -o bin/subscribe_audio ./cmd/subscribe_audio

build-relay-publish:
	CGO_ENABLED=1 go build -o bin/relay_publish ./cmd/relay_publish

build-all: build build-subscribe-audio build-relay-publish

run: build
	DYLD_LIBRARY_PATH=$(SDK_DIR)/agora_sdk_mac ./bin/$(APP) $(ARGS)

clean:
	rm -rf bin
