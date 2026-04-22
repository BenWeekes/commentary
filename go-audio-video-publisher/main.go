package main

// #cgo pkg-config: libavformat libavcodec libavutil libswresample libswscale
// #include <string.h>
// #include <stdlib.h>
// #include <libavutil/error.h>
// #include <libavutil/pixfmt.h>
// #include <libavutil/samplefmt.h>
// #include <libavutil/avutil.h>
// #include <libavcodec/avcodec.h>
// #include "decode_media.h"
import "C"

import (
	"io"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"time"
	"unsafe"

	agoraservice "github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2/go_sdk/rtc"
	rtctokenbuilder "github.com/AgoraIO/Tools/DynamicKey/AgoraDynamicKey/go/src/rtctokenbuilder2"
)

type config struct {
	appID      string
	appCert    string
	channel    string
	uid        string
	token      string
	input      string
	rawAudioFile string
	rawVideoFile string
	rawSampleRate int
	rawChannels int
	rawWidth int
	rawHeight int
	rawFPS int
	encodedAudioFile string
	encodedVideoFile string
	encodedAudioCodec string
	encodedAudioSampleRate int
	encodedAudioChannels int
	encodedAudioSamplesPerChannel int
	encodedVideoFPS int
	loop       bool
	videoMode  string
	audioOnly  bool
	videoOnly  bool
	debugSleep bool
}

type audioChunker struct {
	sampleRate int
	channels   int
	buffer     []byte
}

func main() {
	cfg, err := parseConfig()
	if err != nil {
		fmt.Fprintf(os.Stderr, "configuration error: %v\n", err)
		os.Exit(2)
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt)

	if err := run(cfg, stop); err != nil {
		fmt.Fprintf(os.Stderr, "publisher failed: %v\n", err)
		os.Exit(1)
	}
}

func parseConfig() (*config, error) {
	cfg := &config{}
	flag.StringVar(&cfg.appID, "app-id", envOr("AGORA_APP_ID", ""), "Agora App ID")
	flag.StringVar(&cfg.appCert, "app-certificate", envOr("AGORA_APP_CERTIFICATE", ""), "Agora App Certificate, used only to generate a token when --token is empty")
	flag.StringVar(&cfg.channel, "channel", envOr("AGORA_CHANNEL", ""), "Agora channel name")
	flag.StringVar(&cfg.uid, "uid", envOr("AGORA_UID", "0"), "Agora string UID / user account")
	flag.StringVar(&cfg.token, "token", envOr("AGORA_TOKEN", ""), "Agora RTC token; optional when App Certificate is supplied")
	flag.StringVar(&cfg.input, "input", envOr("MP4_INPUT", ""), "Path to an input MP4 file with H.264 video")
	flag.StringVar(&cfg.rawAudioFile, "raw-audio-file", envOr("RAW_AUDIO_FILE", ""), "Path to a raw PCM S16LE audio file")
	flag.StringVar(&cfg.rawVideoFile, "raw-video-file", envOr("RAW_VIDEO_FILE", ""), "Path to a raw YUV420P video file")
	flag.IntVar(&cfg.rawSampleRate, "raw-sample-rate", envIntOr("RAW_SAMPLE_RATE", 16000), "Raw PCM audio sample rate in Hz")
	flag.IntVar(&cfg.rawChannels, "raw-channels", envIntOr("RAW_CHANNELS", 1), "Raw PCM audio channel count")
	flag.IntVar(&cfg.rawWidth, "raw-width", envIntOr("RAW_WIDTH", 640), "Raw YUV video width")
	flag.IntVar(&cfg.rawHeight, "raw-height", envIntOr("RAW_HEIGHT", 360), "Raw YUV video height")
	flag.IntVar(&cfg.rawFPS, "raw-fps", envIntOr("RAW_FPS", 30), "Raw YUV video frame rate")
	flag.StringVar(&cfg.encodedAudioFile, "encoded-audio-file", envOr("ENCODED_AUDIO_FILE", ""), "Path to an encoded elementary audio file (for example ADTS AAC)")
	flag.StringVar(&cfg.encodedVideoFile, "encoded-video-file", envOr("ENCODED_VIDEO_FILE", ""), "Path to an encoded elementary H.264 video file")
	flag.StringVar(&cfg.encodedAudioCodec, "encoded-audio-codec", envOr("ENCODED_AUDIO_CODEC", "aac"), "Encoded audio codec: aac or opus")
	flag.IntVar(&cfg.encodedAudioSampleRate, "encoded-audio-sample-rate", envIntOr("ENCODED_AUDIO_SAMPLE_RATE", 16000), "Encoded audio sample rate in Hz")
	flag.IntVar(&cfg.encodedAudioChannels, "encoded-audio-channels", envIntOr("ENCODED_AUDIO_CHANNELS", 1), "Encoded audio channel count")
	flag.IntVar(&cfg.encodedAudioSamplesPerChannel, "encoded-audio-samples-per-channel", envIntOr("ENCODED_AUDIO_SAMPLES_PER_CHANNEL", 1024), "Encoded audio samples per channel")
	flag.IntVar(&cfg.encodedVideoFPS, "encoded-video-fps", envIntOr("ENCODED_VIDEO_FPS", 30), "Encoded video send rate in frames per second")
	flag.BoolVar(&cfg.loop, "loop", false, "Restart the input from the beginning when it reaches EOF")
	flag.StringVar(&cfg.videoMode, "video-mode", envOr("VIDEO_MODE", "yuv"), "Video publish mode: yuv or encoded")
	flag.BoolVar(&cfg.audioOnly, "audio-only", false, "Publish only audio from the MP4")
	flag.BoolVar(&cfg.videoOnly, "video-only", false, "Publish only video from the MP4")
	flag.BoolVar(&cfg.debugSleep, "debug-sleep-log", false, "Log pacing sleeps while sending media")
	flag.Parse()

	switch {
	case cfg.appID == "":
		return nil, errors.New("missing --app-id or AGORA_APP_ID")
	case cfg.channel == "":
		return nil, errors.New("missing --channel or AGORA_CHANNEL")
	case !cfg.hasEncodedInputs() && !cfg.hasRawInputs() && cfg.input == "":
		return nil, errors.New("missing --input, raw asset flags, or encoded asset flags")
	case cfg.audioOnly && cfg.videoOnly:
		return nil, errors.New("choose at most one of --audio-only or --video-only")
	case cfg.videoMode != "encoded" && cfg.videoMode != "yuv":
		return nil, errors.New("--video-mode must be one of: encoded, yuv")
	case cfg.hasRawInputs() && cfg.rawSampleRate <= 0:
		return nil, errors.New("--raw-sample-rate must be greater than 0")
	case cfg.hasRawInputs() && cfg.rawChannels <= 0:
		return nil, errors.New("--raw-channels must be greater than 0")
	case cfg.hasRawInputs() && cfg.rawWidth <= 0:
		return nil, errors.New("--raw-width must be greater than 0")
	case cfg.hasRawInputs() && cfg.rawHeight <= 0:
		return nil, errors.New("--raw-height must be greater than 0")
	case cfg.hasRawInputs() && cfg.rawFPS <= 0:
		return nil, errors.New("--raw-fps must be greater than 0")
	case cfg.hasRawInputs() && !cfg.videoOnly && cfg.rawAudioFile == "":
		return nil, errors.New("missing --raw-audio-file for raw AV mode")
	case cfg.hasRawInputs() && !cfg.audioOnly && cfg.rawVideoFile == "":
		return nil, errors.New("missing --raw-video-file for raw AV mode")
	case cfg.hasEncodedInputs() && cfg.encodedAudioCodec != "aac" && cfg.encodedAudioCodec != "opus":
		return nil, errors.New("--encoded-audio-codec must be one of: aac, opus")
	case cfg.hasEncodedInputs() && cfg.encodedVideoFPS <= 0:
		return nil, errors.New("--encoded-video-fps must be greater than 0")
	case cfg.hasEncodedInputs() && !cfg.videoOnly && cfg.encodedAudioFile == "":
		return nil, errors.New("missing --encoded-audio-file for encoded AV mode")
	case cfg.hasEncodedInputs() && !cfg.audioOnly && cfg.encodedVideoFile == "":
		return nil, errors.New("missing --encoded-video-file for encoded AV mode")
	}

	if cfg.input != "" {
		absInput, err := filepath.Abs(cfg.input)
		if err != nil {
			return nil, fmt.Errorf("resolve input path: %w", err)
		}
		if _, err := os.Stat(absInput); err != nil {
			return nil, fmt.Errorf("input file %q: %w", absInput, err)
		}
		cfg.input = absInput
	}
	if cfg.rawAudioFile != "" {
		absAudio, err := filepath.Abs(cfg.rawAudioFile)
		if err != nil {
			return nil, fmt.Errorf("resolve raw audio path: %w", err)
		}
		if _, err := os.Stat(absAudio); err != nil {
			return nil, fmt.Errorf("raw audio file %q: %w", absAudio, err)
		}
		cfg.rawAudioFile = absAudio
	}
	if cfg.rawVideoFile != "" {
		absVideo, err := filepath.Abs(cfg.rawVideoFile)
		if err != nil {
			return nil, fmt.Errorf("resolve raw video path: %w", err)
		}
		if _, err := os.Stat(absVideo); err != nil {
			return nil, fmt.Errorf("raw video file %q: %w", absVideo, err)
		}
		cfg.rawVideoFile = absVideo
	}
	if cfg.encodedAudioFile != "" {
		absAudio, err := filepath.Abs(cfg.encodedAudioFile)
		if err != nil {
			return nil, fmt.Errorf("resolve encoded audio path: %w", err)
		}
		if _, err := os.Stat(absAudio); err != nil {
			return nil, fmt.Errorf("encoded audio file %q: %w", absAudio, err)
		}
		cfg.encodedAudioFile = absAudio
	}
	if cfg.encodedVideoFile != "" {
		absVideo, err := filepath.Abs(cfg.encodedVideoFile)
		if err != nil {
			return nil, fmt.Errorf("resolve encoded video path: %w", err)
		}
		if _, err := os.Stat(absVideo); err != nil {
			return nil, fmt.Errorf("encoded video file %q: %w", absVideo, err)
		}
		cfg.encodedVideoFile = absVideo
	}

	if cfg.token == "" && cfg.appCert != "" {
		token, err := buildToken(cfg.appID, cfg.appCert, cfg.channel, cfg.uid)
		if err != nil {
			return nil, err
		}
		cfg.token = token
	}

	return cfg, nil
}

func run(cfg *config, stop <-chan os.Signal) error {
	if err := os.MkdirAll("agora_rtc_log", 0o755); err != nil {
		return fmt.Errorf("create agora log directory: %w", err)
	}

	svcCfg := agoraservice.NewAgoraServiceConfig()
	svcCfg.AppId = cfg.appID
	svcCfg.EnableVideo = !cfg.audioOnly
	svcCfg.EnableAudioProcessor = true
	svcCfg.LogPath = "./agora_rtc_log/agorasdk.log"
	svcCfg.LogSize = 2 * 1024
	agoraservice.Initialize(svcCfg)
	defer agoraservice.Release()

	publishConfig := agoraservice.NewRtcConPublishConfig()
	publishConfig.AudioScenario = agoraservice.AudioScenarioDefault
	publishConfig.IsPublishAudio = !cfg.videoOnly
	publishConfig.IsPublishVideo = !cfg.audioOnly
	publishConfig.AudioProfile = agoraservice.AudioProfileDefault
	if cfg.hasEncodedInputs() {
		if cfg.videoOnly {
			publishConfig.IsPublishAudio = true
			publishConfig.AudioPublishType = agoraservice.AudioPublishTypePcm
		} else {
			publishConfig.AudioPublishType = agoraservice.AudioPublishTypeEncodedPcm
		}
		publishConfig.VideoPublishType = agoraservice.VideoPublishTypeEncodedImage
		publishConfig.VideoEncodedImageSenderOptions.CcMode = agoraservice.VideoSendCcEnabled
		publishConfig.VideoEncodedImageSenderOptions.CodecType = agoraservice.VideoCodecTypeH264
		publishConfig.VideoEncodedImageSenderOptions.TargetBitrate = 5000
	} else {
		publishConfig.AudioPublishType = agoraservice.AudioPublishTypePcm
		if !cfg.audioOnly {
			if cfg.hasRawInputs() || cfg.videoMode == "yuv" {
				publishConfig.VideoPublishType = agoraservice.VideoPublishTypeYuv
			} else {
				publishConfig.VideoPublishType = agoraservice.VideoPublishTypeEncodedImage
				publishConfig.VideoEncodedImageSenderOptions.CcMode = agoraservice.VideoSendCcEnabled
				publishConfig.VideoEncodedImageSenderOptions.CodecType = agoraservice.VideoCodecTypeH264
				publishConfig.VideoEncodedImageSenderOptions.TargetBitrate = 5000
			}
		}
	}

	conCfg := &agoraservice.RtcConnectionConfig{
		AutoSubscribeAudio: cfg.hasEncodedInputs(),
		AutoSubscribeVideo: cfg.hasEncodedInputs(),
		ClientRole:         agoraservice.ClientRoleBroadcaster,
		ChannelProfile:     agoraservice.ChannelProfileLiveBroadcasting,
	}

	con := agoraservice.NewRtcConnection(conCfg, publishConfig)
	if con == nil {
		return errors.New("failed to create rtc connection")
	}
	defer con.Release()

	connected := make(chan struct{}, 1)
	disconnected := make(chan string, 1)
	con.RegisterObserver(&agoraservice.RtcConnectionObserver{
		OnConnected: func(_ *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
			fmt.Printf("connected: channel=%s uid=%s internal_uid=%d reason=%d\n", info.ChannelId, info.LocalUserId, info.InternalUid, reason)
			select {
			case connected <- struct{}{}:
			default:
			}
		},
		OnDisconnected: func(_ *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
			msg := fmt.Sprintf("disconnected: channel=%s uid=%s reason=%d", info.ChannelId, info.LocalUserId, reason)
			fmt.Println(msg)
			select {
			case disconnected <- msg:
			default:
			}
		},
		OnUserJoined: func(_ *agoraservice.RtcConnection, uid string) {
			fmt.Printf("remote user joined: %s\n", uid)
		},
		OnUserLeft: func(_ *agoraservice.RtcConnection, uid string, reason int) {
			fmt.Printf("remote user left: %s reason=%d\n", uid, reason)
		},
		OnAIQoSCapabilityMissing: func(_ *agoraservice.RtcConnection, defaultFallbackScenario int) int {
			fmt.Printf("AI QoS capability missing, falling back from scenario=%d to default audio scenario\n", defaultFallbackScenario)
			return int(agoraservice.AudioScenarioDefault)
		},
	})
	if cfg.hasEncodedInputs() {
		con.RegisterVideoFrameObserver(&agoraservice.VideoFrameObserver{
			OnFrame: func(channelID string, userID string, frame *agoraservice.VideoFrame) bool {
				return true
			},
		})
	}

	if rc := con.Connect(cfg.token, cfg.channel, cfg.uid); rc != 0 {
		return fmt.Errorf("connect failed: %d", rc)
	}

	select {
	case <-connected:
	case msg := <-disconnected:
		return errors.New(msg)
	case <-time.After(10 * time.Second):
		return errors.New("timed out waiting for Agora connection")
	case <-stop:
		return errors.New("interrupted before connection completed")
	}

	if cfg.hasRawInputs() && !cfg.audioOnly {
		videoEncoderConfig := &agoraservice.VideoEncoderConfiguration{
			CodecType:         agoraservice.VideoCodecTypeH264,
			Width:             cfg.rawWidth,
			Height:            cfg.rawHeight,
			Framerate:         cfg.rawFPS,
			Bitrate:           1000,
			MinBitrate:        100,
			OrientationMode:   agoraservice.OrientationModeAdaptive,
			DegradePreference: agoraservice.DegradeMaintainBalanced,
		}
		if rc := con.SetVideoEncoderConfiguration(videoEncoderConfig); rc != 0 {
			return fmt.Errorf("set video encoder configuration failed: %d", rc)
		}
	}

	if !cfg.videoOnly {
		if rc := con.PublishAudio(); rc != 0 {
			return fmt.Errorf("publish audio failed: %d", rc)
		}
	}
	if !cfg.audioOnly {
		if rc := con.PublishVideo(); rc != 0 {
			return fmt.Errorf("publish video failed: %d", rc)
		}
	}

	if cfg.hasEncodedInputs() {
		fmt.Printf("publishing encoded assets audio=%s video=%s to channel %s as uid %s\n", cfg.encodedAudioFile, cfg.encodedVideoFile, cfg.channel, cfg.uid)
	} else if cfg.hasRawInputs() {
		fmt.Printf("publishing raw assets audio=%s video=%s to channel %s as uid %s\n", cfg.rawAudioFile, cfg.rawVideoFile, cfg.channel, cfg.uid)
	} else {
		fmt.Printf("publishing input %s to channel %s as uid %s\n", cfg.input, cfg.channel, cfg.uid)
	}
	for {
		var err error
		if cfg.hasEncodedInputs() {
			err = streamEncodedAssets(con, cfg, stop, disconnected)
		} else if cfg.hasRawInputs() {
			err = streamRawAssets(con, cfg, stop, disconnected)
		} else {
			err = streamDecodedMedia(con, cfg, stop, disconnected)
		}
		if err != nil {
			con.Disconnect()
			return err
		}
		if !cfg.loop {
			break
		}
		fmt.Printf("restarting input from beginning: %s\n", cfg.input)
	}
	con.Disconnect()
	return nil
}

func streamMedia(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	fileName := C.CString(cfg.input)
	defer C.free(unsafe.Pointer(fileName))

	decoder := C.open_media_file(fileName)
	if decoder == nil {
		return fmt.Errorf("open media file %q", cfg.input)
	}
	defer C.close_media_file(decoder)

	var packet *C.struct__MediaPacket
	frame := C.struct__MediaFrame{}
	C.memset(unsafe.Pointer(&frame), 0, C.sizeof_struct__MediaFrame)
	audioChunker := &audioChunker{}

	var firstPTS int64
	startedAt := time.Now()

	for {
		select {
		case <-stop:
			return errors.New("interrupted")
		case msg := <-disconnected:
			return errors.New(msg)
		default:
		}

		totalSendTime := time.Since(startedAt).Milliseconds()
		ret := C.get_packet(decoder, &packet)
		if ret != 0 {
			fmt.Printf("finished reading input: code=%d\n", int(ret))
			return nil
		}
		if packet == nil {
			continue
		}

		switch packet.media_type {
		case C.AVMEDIA_TYPE_AUDIO:
			if cfg.videoOnly {
				C.free_packet(&packet)
				continue
			}
		case C.AVMEDIA_TYPE_VIDEO:
			if cfg.audioOnly {
				C.free_packet(&packet)
				continue
			}
		default:
			C.free_packet(&packet)
			continue
		}

		if packet.pts <= 0 {
			packet.pts = 1
		}

		if firstPTS == 0 {
			firstPTS = int64(packet.pts)
			startedAt = time.Now()
			totalSendTime = 0
			time.Sleep(50 * time.Millisecond)
			fmt.Printf("starting media stream at pts=%dms\n", firstPTS)
		}

		targetDelay := int64(packet.pts) - firstPTS - totalSendTime
		if targetDelay > 0 {
			sleepFor := time.Duration(min64(targetDelay, 100)) * time.Millisecond
			if cfg.debugSleep {
				fmt.Printf("pacing sleep: %s for packet pts=%d\n", sleepFor, int64(packet.pts))
			}
			time.Sleep(sleepFor)
		}

		switch packet.media_type {
		case C.AVMEDIA_TYPE_AUDIO:
			if err := sendAudioPacket(con, decoder, packet, &frame, audioChunker); err != nil {
				return err
			}
		case C.AVMEDIA_TYPE_VIDEO:
			if err := sendVideoPacket(con, cfg, decoder, packet, &frame); err != nil {
				return err
			}
		}
	}
}

func streamDecodedMedia(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	errCh := make(chan error, 2)
	var wg sync.WaitGroup

	if !cfg.videoOnly {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errCh <- sendDecodedAudioLoop(con, cfg, stop, disconnected)
		}()
	}
	if !cfg.audioOnly {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errCh <- sendDecodedVideoLoop(con, cfg, stop, disconnected)
		}()
	}

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	for {
		select {
		case <-stop:
			return errors.New("interrupted")
		case msg := <-disconnected:
			return errors.New(msg)
		case err := <-errCh:
			if err != nil {
				return err
			}
		case <-done:
			return nil
		}
	}
}

func sendDecodedAudioLoop(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	for {
		fileName := C.CString(cfg.input)
		decoder := C.open_media_file(fileName)
		C.free(unsafe.Pointer(fileName))
		if decoder == nil {
			return fmt.Errorf("open media file %q", cfg.input)
		}

		var packet *C.struct__MediaPacket
		frame := C.struct__MediaFrame{}
		C.memset(unsafe.Pointer(&frame), 0, C.sizeof_struct__MediaFrame)
		audioChunker := &audioChunker{}
		var nextChunkAt time.Time

		for {
			select {
			case <-stop:
				C.close_media_file(decoder)
				return errors.New("interrupted")
			case msg := <-disconnected:
				C.close_media_file(decoder)
				return errors.New(msg)
			default:
			}

			ret := C.get_packet(decoder, &packet)
			if ret != 0 {
				C.close_media_file(decoder)
				if cfg.loop {
					break
				}
				return nil
			}
			if packet == nil {
				continue
			}
			if packet.media_type != C.AVMEDIA_TYPE_AUDIO {
				C.free_packet(&packet)
				continue
			}

			ret = C.decode_packet(decoder, packet, &frame)
			C.free_packet(&packet)
			if ret != 0 {
				if ret == C.AVERROR_EAGAIN {
					continue
				}
				C.close_media_file(decoder)
				return fmt.Errorf("decode audio packet: %d", int(ret))
			}
			if frame.format != C.AV_SAMPLE_FMT_S16 {
				C.close_media_file(decoder)
				return fmt.Errorf("unsupported decoded audio sample format: %d", int(frame.format))
			}

			audioData := unsafe.Slice((*byte)(unsafe.Pointer(frame.buffer)), frame.buffer_size)
			sampleRate := int(frame.sample_rate)
			channels := int(frame.channels)
			for _, chunk := range audioChunker.append(audioData, sampleRate, channels) {
				if nextChunkAt.IsZero() {
					nextChunkAt = time.Now()
				}
				if wait := time.Until(nextChunkAt); wait > 0 {
					time.Sleep(wait)
				}
				if rc := con.PushAudioPcmData(chunk, sampleRate, channels, 0); rc != 0 {
					C.close_media_file(decoder)
					return fmt.Errorf("push audio pcm data: %d", rc)
				}
				nextChunkAt = nextChunkAt.Add(10 * time.Millisecond)
			}
		}
	}
}

func sendDecodedVideoLoop(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	for {
		fileName := C.CString(cfg.input)
		decoder := C.open_media_file(fileName)
		C.free(unsafe.Pointer(fileName))
		if decoder == nil {
			return fmt.Errorf("open media file %q", cfg.input)
		}

		var packet *C.struct__MediaPacket
		frame := C.struct__MediaFrame{}
		C.memset(unsafe.Pointer(&frame), 0, C.sizeof_struct__MediaFrame)
		var firstPTS int64 = -1
		startedAt := time.Now()

		for {
			select {
			case <-stop:
				C.close_media_file(decoder)
				return errors.New("interrupted")
			case msg := <-disconnected:
				C.close_media_file(decoder)
				return errors.New(msg)
			default:
			}

			ret := C.get_packet(decoder, &packet)
			if ret != 0 {
				C.close_media_file(decoder)
				if cfg.loop {
					break
				}
				return nil
			}
			if packet == nil {
				continue
			}
			if packet.media_type != C.AVMEDIA_TYPE_VIDEO {
				C.free_packet(&packet)
				continue
			}

			sendPTS := int64(packet.pts)
			if sendPTS < 0 {
				sendPTS = 0
			}
			if firstPTS < 0 {
				firstPTS = sendPTS
				startedAt = time.Now()
			}
			if wait := startedAt.Add(time.Duration(sendPTS-firstPTS) * time.Millisecond).Sub(time.Now()); wait > 0 {
				time.Sleep(wait)
			}

			ret = C.decode_packet(decoder, packet, &frame)
			C.free_packet(&packet)
			if ret != 0 {
				if ret == C.AVERROR_EAGAIN {
					continue
				}
				C.close_media_file(decoder)
				return fmt.Errorf("decode video packet: %d", int(ret))
			}
			if frame.format != C.AV_PIX_FMT_YUV420P {
				C.close_media_file(decoder)
				return fmt.Errorf("unsupported decoded video pixel format: %d", int(frame.format))
			}

			videoData := unsafe.Slice((*byte)(unsafe.Pointer(frame.buffer)), frame.buffer_size)
			videoFrame := &agoraservice.ExternalVideoFrame{
				Type:      agoraservice.VideoBufferRawData,
				Format:    agoraservice.VideoPixelI420,
				Buffer:    videoData,
				Stride:    int(frame.stride),
				Height:    int(frame.height),
				Rotation:  agoraservice.VideoOrientation0,
				Timestamp: 0,
			}
			if rc := con.PushVideoFrame(videoFrame); rc != 0 {
				C.close_media_file(decoder)
				return fmt.Errorf("push yuv video frame pts=%d: %d", int64(frame.pts), rc)
			}
		}
	}
}

func streamEncodedAssets(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	errCh := make(chan error, 2)
	var wg sync.WaitGroup

	if !cfg.videoOnly {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errCh <- sendEncodedAudioLoop(con, cfg, stop, disconnected)
		}()
	}
	if !cfg.audioOnly {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errCh <- sendEncodedVideoLoop(con, cfg, stop, disconnected)
		}()
	}

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	for {
		select {
		case <-stop:
			return errors.New("interrupted")
		case msg := <-disconnected:
			return errors.New(msg)
		case err := <-errCh:
			if err != nil {
				return err
			}
		case <-done:
			return nil
		}
	}
}

func streamRawAssets(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	errCh := make(chan error, 2)
	var wg sync.WaitGroup

	if !cfg.videoOnly {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errCh <- sendRawAudioLoop(con, cfg, stop, disconnected)
		}()
	}
	if !cfg.audioOnly {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errCh <- sendRawVideoLoop(con, cfg, stop, disconnected)
		}()
	}

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	for {
		select {
		case <-stop:
			return errors.New("interrupted")
		case msg := <-disconnected:
			return errors.New(msg)
		case err := <-errCh:
			if err != nil {
				return err
			}
		case <-done:
			return nil
		}
	}
}

func sendRawAudioLoop(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	bytesPerChunk := (cfg.rawSampleRate / 100) * cfg.rawChannels * 2
	if bytesPerChunk <= 0 {
		return fmt.Errorf("invalid raw audio chunk size")
	}
	chunk := make([]byte, bytesPerChunk)

	for {
		file, err := os.Open(cfg.rawAudioFile)
		if err != nil {
			return fmt.Errorf("open raw audio file %q: %w", cfg.rawAudioFile, err)
		}

		nextTick := time.Now()
		elapsedMs := int64(0)

		for {
			select {
			case <-stop:
				file.Close()
				return errors.New("interrupted")
			case msg := <-disconnected:
				file.Close()
				return errors.New(msg)
			default:
			}

			_, err := io.ReadFull(file, chunk)
			if err != nil {
				file.Close()
				if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
					if cfg.loop {
						break
					}
					return nil
				}
				return fmt.Errorf("read raw audio: %w", err)
			}

			if wait := time.Until(nextTick); wait > 0 {
				time.Sleep(wait)
			}

			if rc := con.PushAudioPcmData(chunk, cfg.rawSampleRate, cfg.rawChannels, elapsedMs); rc != 0 {
				file.Close()
				return fmt.Errorf("push raw audio pcm data: %d", rc)
			}
			nextTick = nextTick.Add(10 * time.Millisecond)
			elapsedMs += 10
		}
	}
}

func sendRawVideoLoop(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	frameSize := cfg.rawWidth * cfg.rawHeight * 3 / 2
	if frameSize <= 0 {
		return fmt.Errorf("invalid raw video frame size")
	}
	frame := make([]byte, frameSize)
	frameInterval := time.Second / time.Duration(cfg.rawFPS)

	for {
		file, err := os.Open(cfg.rawVideoFile)
		if err != nil {
			return fmt.Errorf("open raw video file %q: %w", cfg.rawVideoFile, err)
		}

		nextTick := time.Now()
		elapsedMs := int64(0)

		for {
			select {
			case <-stop:
				file.Close()
				return errors.New("interrupted")
			case msg := <-disconnected:
				file.Close()
				return errors.New(msg)
			default:
			}

			_, err := io.ReadFull(file, frame)
			if err != nil {
				file.Close()
				if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
					if cfg.loop {
						break
					}
					return nil
				}
				return fmt.Errorf("read raw video: %w", err)
			}

			if wait := time.Until(nextTick); wait > 0 {
				time.Sleep(wait)
			}

			videoFrame := &agoraservice.ExternalVideoFrame{
				Type:      agoraservice.VideoBufferRawData,
				Format:    agoraservice.VideoPixelI420,
				Buffer:    frame,
				Stride:    cfg.rawWidth,
				Height:    cfg.rawHeight,
				Rotation:  agoraservice.VideoOrientation0,
				Timestamp: elapsedMs,
			}
			if rc := con.PushVideoFrame(videoFrame); rc != 0 {
				file.Close()
				return fmt.Errorf("push raw video frame: %d", rc)
			}
			nextTick = nextTick.Add(frameInterval)
			elapsedMs += frameInterval.Milliseconds()
		}
	}
}

func sendEncodedAudioLoop(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	codec := agoraservice.AudioCodecAacLc
	if cfg.encodedAudioCodec == "opus" {
		codec = agoraservice.AudioCodecOpus
	}

	for {
		fileName := C.CString(cfg.encodedAudioFile)
		decoder := C.open_media_file(fileName)
		C.free(unsafe.Pointer(fileName))
		if decoder == nil {
			return fmt.Errorf("open encoded audio file %q", cfg.encodedAudioFile)
		}

		var packet *C.struct__MediaPacket
		var firstPTS int64 = -1
		startedAt := time.Now()

		for {
			select {
			case <-stop:
				C.close_media_file(decoder)
				return errors.New("interrupted")
			case msg := <-disconnected:
				C.close_media_file(decoder)
				return errors.New(msg)
			default:
			}

			ret := C.get_packet(decoder, &packet)
			if ret != 0 {
				C.close_media_file(decoder)
				if cfg.loop {
					break
				}
				return nil
			}
			if packet == nil {
				continue
			}
			if packet.media_type != C.AVMEDIA_TYPE_AUDIO {
				C.free_packet(&packet)
				continue
			}

			sendPTS := int64(packet.pts)
			if sendPTS < 0 {
				sendPTS = 0
			}
			if firstPTS < 0 {
				firstPTS = sendPTS
				startedAt = time.Now()
			}
			if wait := startedAt.Add(time.Duration(sendPTS-firstPTS) * time.Millisecond).Sub(time.Now()); wait > 0 {
				time.Sleep(wait)
			}

			data := C.GoBytes(unsafe.Pointer(packet.pkt.data), packet.pkt.size)
			rc := con.PushAudioEncodedData(data, &agoraservice.EncodedAudioFrameInfo{
				Speech:            false,
				Codec:             codec,
				SampleRateHz:      cfg.encodedAudioSampleRate,
				SamplesPerChannel: cfg.encodedAudioSamplesPerChannel,
				SendEvenIfEmpty:   true,
				NumberOfChannels:  cfg.encodedAudioChannels,
				CaptureTimeMs:     sendPTS,
			})
			C.free_packet(&packet)
			if rc != 0 {
				C.close_media_file(decoder)
				return fmt.Errorf("push encoded audio frame: %d", rc)
			}
		}
	}
}

func sendEncodedVideoLoop(con *agoraservice.RtcConnection, cfg *config, stop <-chan os.Signal, disconnected <-chan string) error {
	for {
		formatCtx, codecParam, stream, err := openEncodedVideoInput(cfg.encodedVideoFile)
		if err != nil {
			return err
		}
		packet := C.av_packet_alloc()
		if packet == nil {
			closeEncodedInput(&formatCtx)
			return errors.New("allocate encoded video packet")
		}

		fps := cfg.encodedVideoFPS
		if fps <= 0 && codecParam.framerate.den != 0 {
			fps = int(codecParam.framerate.num / codecParam.framerate.den)
		}
		if fps <= 0 {
			fps = 25
		}
		frameInterval := time.Second / time.Duration(fps)

		for {
			select {
			case <-stop:
				C.av_packet_free(&packet)
				closeEncodedInput(&formatCtx)
				return errors.New("interrupted")
			case msg := <-disconnected:
				C.av_packet_free(&packet)
				closeEncodedInput(&formatCtx)
				return errors.New(msg)
			default:
			}

			ret := C.av_read_frame(formatCtx, packet)
			if ret != 0 {
				C.av_packet_free(&packet)
				closeEncodedInput(&formatCtx)
				if cfg.loop {
					break
				}
				return nil
			}

			if packet.stream_index != stream.index {
				C.av_packet_unref(packet)
				continue
			}

			frameType := agoraservice.VideoFrameTypeDeltaFrame
			if packet.flags&C.AV_PKT_FLAG_KEY != 0 {
				frameType = agoraservice.VideoFrameTypeKeyFrame
			}
			data := C.GoBytes(unsafe.Pointer(packet.data), packet.size)
			rc := con.PushVideoEncodedData(data, &agoraservice.EncodedVideoFrameInfo{
				CodecType:       agoraservice.VideoCodecTypeH264,
				Width:           int(codecParam.width),
				Height:          int(codecParam.height),
				FramesPerSecond: fps,
				FrameType:       frameType,
				Rotation:        agoraservice.VideoOrientation0,
			})
			C.av_packet_unref(packet)
			if rc != 0 {
				return fmt.Errorf("push encoded video frame: %d", rc)
			}
			time.Sleep(frameInterval)
		}
	}
}

func sendAudioPacket(con *agoraservice.RtcConnection, decoder unsafe.Pointer, packet *C.struct__MediaPacket, frame *C.struct__MediaFrame, chunker *audioChunker) error {
	ret := C.decode_packet(decoder, packet, frame)
	C.free_packet(&packet)
	if ret != 0 {
		if ret == C.AVERROR_EAGAIN {
			return nil
		}
		return fmt.Errorf("decode audio packet: %d", int(ret))
	}
	if frame.format != C.AV_SAMPLE_FMT_S16 {
		return fmt.Errorf("unsupported decoded audio sample format: %d", int(frame.format))
	}

	audioData := unsafe.Slice((*byte)(unsafe.Pointer(frame.buffer)), frame.buffer_size)
	sampleRate := int(frame.sample_rate)
	channels := int(frame.channels)

	for _, chunk := range chunker.append(audioData, sampleRate, channels) {
		if rc := con.PushAudioPcmData(chunk, sampleRate, channels, 0); rc != 0 {
			return fmt.Errorf("push audio pcm data: %d", rc)
		}
	}
	return nil
}

func sendVideoPacket(con *agoraservice.RtcConnection, cfg *config, decoder unsafe.Pointer, packet *C.struct__MediaPacket, frame *C.struct__MediaFrame) error {
	if cfg.videoMode == "encoded" {
		return sendEncodedVideoPacket(con, decoder, packet)
	}
	return sendYUVVideoPacket(con, decoder, packet, frame)
}

func sendYUVVideoPacket(con *agoraservice.RtcConnection, decoder unsafe.Pointer, packet *C.struct__MediaPacket, frame *C.struct__MediaFrame) error {
	ret := C.decode_packet(decoder, packet, frame)
	C.free_packet(&packet)
	if ret != 0 {
		if ret == C.AVERROR_EAGAIN {
			return nil
		}
		return fmt.Errorf("decode video packet: %d", int(ret))
	}
	if frame.format != C.AV_PIX_FMT_YUV420P {
		return fmt.Errorf("unsupported decoded video pixel format: %d", int(frame.format))
	}

	videoData := unsafe.Slice((*byte)(unsafe.Pointer(frame.buffer)), frame.buffer_size)
	videoFrame := &agoraservice.ExternalVideoFrame{
		Type:      agoraservice.VideoBufferRawData,
		Format:    agoraservice.VideoPixelI420,
		Buffer:    videoData,
		Stride:    int(frame.stride),
		Height:    int(frame.height),
		Rotation:  agoraservice.VideoOrientation0,
		Timestamp: 0,
	}

	if rc := con.PushVideoFrame(videoFrame); rc != 0 {
		return fmt.Errorf("push yuv video frame pts=%d: %d", int64(frame.pts), rc)
	}
	return nil
}

func sendEncodedVideoPacket(con *agoraservice.RtcConnection, decoder unsafe.Pointer, packet *C.struct__MediaPacket) error {
	ret := C.h264_to_annexb(decoder, &packet)
	if ret != 0 {
		if ret == C.AVERROR_EAGAIN {
			return nil
		}
		return fmt.Errorf("convert h264 packet to annexb: %d", int(ret))
	}
	if packet == nil || packet.pkt == nil || packet.pkt.size <= 0 {
		return nil
	}
	defer C.free_packet(&packet)
	if packet.pts <= 0 {
		packet.pts = 1
	}

	frameType := agoraservice.VideoFrameTypeDeltaFrame
	if packet.pkt.flags&C.AV_PKT_FLAG_KEY != 0 {
		frameType = agoraservice.VideoFrameTypeKeyFrame
	}

	data := C.GoBytes(unsafe.Pointer(packet.pkt.data), packet.pkt.size)
	if rc := con.PushVideoEncodedData(data, &agoraservice.EncodedVideoFrameInfo{
		CodecType:       agoraservice.VideoCodecTypeH264,
		Width:           int(packet.width),
		Height:          int(packet.height),
		FramesPerSecond: 0,
		FrameType:       frameType,
		Rotation:        agoraservice.VideoOrientation0,
		CaptureTimeMs:   0,
		DecodeTimeMs:    0,
		PresentTimeMs:   0,
	}); rc != 0 {
		return fmt.Errorf("push encoded video frame pts=%d: %d", int64(packet.pts), rc)
	}
	return nil
}

func buildToken(appID string, appCert string, channel string, uid string) (string, error) {
	tokenExpirationInSeconds := uint32(3600)
	privilegeExpirationInSeconds := uint32(3600)
	token, err := rtctokenbuilder.BuildTokenWithUserAccount(
		appID,
		appCert,
		channel,
		uid,
		rtctokenbuilder.RolePublisher,
		tokenExpirationInSeconds,
		privilegeExpirationInSeconds,
	)
	if err != nil {
		return "", fmt.Errorf("build token: %w", err)
	}
	return token, nil
}

func envOr(name string, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func envIntOr(name string, fallback int) int {
	if value := os.Getenv(name); value != "" {
		var parsed int
		if _, err := fmt.Sscanf(value, "%d", &parsed); err == nil {
			return parsed
		}
	}
	return fallback
}

func min64(a int64, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func (c *audioChunker) append(data []byte, sampleRate int, channels int) [][]byte {
	if c.sampleRate != sampleRate || c.channels != channels {
		c.sampleRate = sampleRate
		c.channels = channels
		c.buffer = c.buffer[:0]
	}

	c.buffer = append(c.buffer, data...)
	bytesPer10ms := (sampleRate / 1000) * 2 * channels * 10
	if bytesPer10ms <= 0 {
		return nil
	}

	var chunks [][]byte
	for len(c.buffer) >= bytesPer10ms {
		chunk := make([]byte, bytesPer10ms)
		copy(chunk, c.buffer[:bytesPer10ms])
		chunks = append(chunks, chunk)
		c.buffer = c.buffer[bytesPer10ms:]
	}
	return chunks
}

func (cfg *config) hasEncodedInputs() bool {
	return cfg.encodedAudioFile != "" || cfg.encodedVideoFile != ""
}

func (cfg *config) hasRawInputs() bool {
	return cfg.rawAudioFile != "" || cfg.rawVideoFile != ""
}

func openEncodedVideoInput(path string) (*C.struct_AVFormatContext, *C.struct_AVCodecParameters, *C.struct_AVStream, error) {
	fileName := C.CString(path)
	defer C.free(unsafe.Pointer(fileName))

	var formatCtx *C.struct_AVFormatContext
	if C.avformat_open_input(&formatCtx, fileName, nil, nil) != 0 {
		return nil, nil, nil, fmt.Errorf("open encoded video file %q", path)
	}
	if C.avformat_find_stream_info(formatCtx, nil) < 0 {
		closeEncodedInput(&formatCtx)
		return nil, nil, nil, fmt.Errorf("find encoded video stream info for %q", path)
	}

	streams := unsafe.Slice((**C.struct_AVStream)(unsafe.Pointer(formatCtx.streams)), formatCtx.nb_streams)
	if len(streams) == 0 || streams[0] == nil {
		closeEncodedInput(&formatCtx)
		return nil, nil, nil, fmt.Errorf("no encoded video streams in %q", path)
	}
	stream := streams[0]
	codecParam := (*C.struct_AVCodecParameters)(unsafe.Pointer(stream.codecpar))
	if codecParam == nil || codecParam.codec_id != C.AV_CODEC_ID_H264 {
		closeEncodedInput(&formatCtx)
		return nil, nil, nil, fmt.Errorf("encoded video file %q is not H264", path)
	}
	return formatCtx, codecParam, stream, nil
}

func closeEncodedInput(formatCtx **C.struct_AVFormatContext) {
	if formatCtx == nil || *formatCtx == nil {
		return
	}
	C.avformat_close_input(formatCtx)
}
