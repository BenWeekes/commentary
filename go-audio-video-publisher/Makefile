APP := go-audio-video-publisher
SDK_DIR := ../server-custom-llm/go-audio-subscriber/sdk

.PHONY: build run clean

build:
	CGO_ENABLED=1 go build -o bin/$(APP) .

run: build
	DYLD_LIBRARY_PATH=$(SDK_DIR)/agora_sdk_mac ./bin/$(APP) $(ARGS)

clean:
	rm -rf bin
