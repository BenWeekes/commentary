package main

// #cgo pkg-config: libavformat libavcodec libavutil
// #include <libavformat/avformat.h>
// #include <libavutil/avutil.h>
// #include <libavcodec/avcodec.h>
import "C"

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"time"
	"unsafe"

	agoraservice "github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2/go_sdk/rtc"
	rtctokenbuilder "github.com/AgoraIO/Tools/DynamicKey/AgoraDynamicKey/go/src/rtctokenbuilder2"
)

func openMediaFile(file string) *C.struct_AVFormatContext {
	var pFormatContext *C.struct_AVFormatContext
	fn := C.CString(file)
	defer C.free(unsafe.Pointer(fn))
	if C.avformat_open_input(&pFormatContext, fn, nil, nil) != 0 {
		fmt.Printf("Unable to open file %s\n", file)
		return nil
	}
	if C.avformat_find_stream_info(pFormatContext, nil) < 0 {
		fmt.Println("Couldn't find stream information")
		return nil
	}
	return pFormatContext
}

func getStreamInfo(pFormatContext *C.struct_AVFormatContext) *C.struct_AVStream {
	streams := unsafe.Slice((**C.struct_AVStream)(unsafe.Pointer(pFormatContext.streams)), pFormatContext.nb_streams)
	return streams[0]
}

func closeMediaFile(pFormatContext **C.struct_AVFormatContext) {
	C.avformat_close_input(pFormatContext)
}

func main() {
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt)

	args := os.Args
	if len(args) < 3 {
		fmt.Println("usage: send_h264_pcm_uid73 <app_id> <channel> [h264_file] [raw_pcm_file]")
		return
	}
	appID := args[1]
	channelName := args[2]
	videoFile := "/Users/benweekes/work/codex/go-audio-video-publisher/encoded_assets/bundesliga_35_40_agora_like_352x288_25.h264"
	audioFile := "/Users/benweekes/work/codex/go-audio-video-publisher/raw_assets/bundesliga_35_40_16kmono.pcm"
	if len(args) >= 4 {
		videoFile = args[3]
	}
	if len(args) >= 5 {
		audioFile = args[4]
	}

	cert := os.Getenv("AGORA_APP_CERTIFICATE")
	userID := "73"
	if appID == "" {
		fmt.Println("Please set AGORA_APP_ID or pass it as arg 1")
		return
	}

	token := ""
	if cert != "" {
		var err error
		token, err = rtctokenbuilder.BuildTokenWithUid(appID, cert, channelName, 73, rtctokenbuilder.RolePublisher, 3600, 3600)
		if err != nil {
			fmt.Println("Failed to build token:", err)
			return
		}
	}

	svcCfg := agoraservice.NewAgoraServiceConfig()
	svcCfg.EnableVideo = true
	svcCfg.AppId = appID
	agoraservice.Initialize(svcCfg)
	defer agoraservice.Release()

	conSignal := make(chan struct{}, 1)
	conHandler := &agoraservice.RtcConnectionObserver{
		OnConnected: func(con *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
			fmt.Printf("Connected uid=%s internal_uid=%d reason=%d\n", info.LocalUserId, info.InternalUid, reason)
			select {
			case conSignal <- struct{}{}:
			default:
			}
		},
		OnDisconnected: func(con *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
			fmt.Printf("Disconnected, reason %d\n", reason)
		},
		OnUserJoined: func(con *agoraservice.RtcConnection, uid string) {
			fmt.Println("user joined,", uid)
		},
		OnUserLeft: func(con *agoraservice.RtcConnection, uid string, reason int) {
			fmt.Println("user left,", uid, "reason", reason)
		},
		OnAIQoSCapabilityMissing: func(con *agoraservice.RtcConnection, defaultFallbackScenario int) int {
			fmt.Printf("onAIQoSCapabilityMissing, defaultFallbackScenario: %d\n", defaultFallbackScenario)
			return int(agoraservice.AudioScenarioDefault)
		},
	}
	videoObserver := &agoraservice.VideoFrameObserver{
		OnFrame: func(channelID string, uid string, frame *agoraservice.VideoFrame) bool {
			fmt.Printf("recv video frame, from channel %s, user %s\n", channelID, uid)
			return true
		},
	}

	conCfg := &agoraservice.RtcConnectionConfig{
		AutoSubscribeAudio: true,
		AutoSubscribeVideo: true,
		ClientRole:         agoraservice.ClientRoleBroadcaster,
		ChannelProfile:     agoraservice.ChannelProfileLiveBroadcasting,
	}
	publishConfig := agoraservice.NewRtcConPublishConfig()
	publishConfig.AudioScenario = agoraservice.AudioScenarioAiServer
	publishConfig.IsPublishAudio = true
	publishConfig.IsPublishVideo = true
	publishConfig.AudioPublishType = agoraservice.AudioPublishTypePcm
	publishConfig.VideoPublishType = agoraservice.VideoPublishTypeEncodedImage
	publishConfig.VideoEncodedImageSenderOptions.CcMode = agoraservice.VideoSendCcEnabled
	publishConfig.VideoEncodedImageSenderOptions.CodecType = agoraservice.VideoCodecTypeH264
	publishConfig.VideoEncodedImageSenderOptions.TargetBitrate = 5000

	con := agoraservice.NewRtcConnection(conCfg, publishConfig)
	if con == nil {
		fmt.Println("failed to create connection")
		return
	}
	defer con.Release()

	con.RegisterObserver(conHandler)
	con.RegisterVideoFrameObserver(videoObserver)
	if rc := con.Connect(token, channelName, userID); rc != 0 {
		fmt.Printf("connect failed: %d\n", rc)
		return
	}
	<-conSignal

	if rc := con.PublishVideo(); rc != 0 {
		fmt.Printf("PublishVideo failed: %d\n", rc)
		return
	}

	audioErr := make(chan error, 1)
	audioStarted := false
	videoStart := time.Now()

	pFormatContext := openMediaFile(videoFile)
	if pFormatContext == nil {
		return
	}
	defer closeMediaFile(&pFormatContext)

	packet := C.av_packet_alloc()
	defer C.av_packet_free(&packet)
	streamInfo := getStreamInfo(pFormatContext)
	codecParam := (*C.struct_AVCodecParameters)(unsafe.Pointer(streamInfo.codecpar))
	sendInterval := 1000 * int64(codecParam.framerate.den) / int64(codecParam.framerate.num)

	for {
		select {
		case <-stop:
			fmt.Println("Application terminated")
			con.Disconnect()
			return
		case err := <-audioErr:
			if err != nil {
				fmt.Println("audio loop failed:", err)
			}
			con.Disconnect()
			return
		default:
		}

		if !audioStarted && time.Since(videoStart) >= 2*time.Second {
			if rc := con.PublishAudio(); rc != 0 {
				fmt.Printf("PublishAudio failed: %d\n", rc)
				con.Disconnect()
				return
			}
			go func() {
				audioErr <- sendRawPCM(con, audioFile, stop)
			}()
			audioStarted = true
			fmt.Println("audio publishing started after video warmup")
		}

		ret := int(C.av_read_frame(pFormatContext, packet))
		if ret < 0 {
			fmt.Println("Finished reading file:", ret)
			closeMediaFile(&pFormatContext)
			pFormatContext = openMediaFile(videoFile)
			if pFormatContext == nil {
				con.Disconnect()
				return
			}
			streamInfo = getStreamInfo(pFormatContext)
			codecParam = (*C.struct_AVCodecParameters)(unsafe.Pointer(streamInfo.codecpar))
			continue
		}

		isKeyFrame := packet.flags&C.AV_PKT_FLAG_KEY != 0
		frameType := agoraservice.VideoFrameTypeKeyFrame
		if !isKeyFrame {
			frameType = agoraservice.VideoFrameTypeDeltaFrame
		}
		data := C.GoBytes(unsafe.Pointer(packet.data), packet.size)
		ret = con.PushVideoEncodedData(data, &agoraservice.EncodedVideoFrameInfo{
			CodecType:       agoraservice.VideoCodecTypeH264,
			Width:           int(codecParam.width),
			Height:          int(codecParam.height),
			FramesPerSecond: int(codecParam.framerate.num / codecParam.framerate.den),
			FrameType:       frameType,
			Rotation:        agoraservice.VideoOrientation0,
		})
		if ret != 0 {
			fmt.Printf("PushVideoEncodedData ret=%d\n", ret)
		}
		C.av_packet_unref(packet)
		time.Sleep(time.Duration(sendInterval) * time.Millisecond)
	}
}

func sendRawPCM(con *agoraservice.RtcConnection, path string, stop <-chan os.Signal) error {
	const sampleRate = 16000
	const channels = 1
	const bytesPer10ms = 320

	chunk := make([]byte, bytesPer10ms)
	silence := make([]byte, bytesPer10ms) // all zeros = silence

	// Stdin mode: read from stdin, send silence when no data available
	if path == "-" || path == "stdin" {
		reader := io.Reader(os.Stdin)
		nextTick := time.Now()
		elapsedMs := int64(0)
		for {
			select {
			case <-stop:
				return nil
			default:
			}

			n, err := io.ReadFull(reader, chunk)
			if err != nil {
				if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
					// Send whatever partial data we got padded with silence
					if n > 0 {
						copy(chunk[n:], silence[n:])
					} else {
						copy(chunk, silence)
					}
				} else {
					return err
				}
			}

			if wait := time.Until(nextTick); wait > 0 {
				time.Sleep(wait)
			}

			ret := con.PushAudioPcmData(chunk, sampleRate, channels, elapsedMs)
			if ret != 0 {
				fmt.Printf("PushAudioPcmData ret=%d\n", ret)
			}
			nextTick = nextTick.Add(10 * time.Millisecond)
			elapsedMs += 10
		}
	}

	// File mode: read from file, loop on EOF
	for {
		file, err := os.Open(path)
		if err != nil {
			return err
		}

		nextTick := time.Now()
		elapsedMs := int64(0)
		for {
			select {
			case <-stop:
				file.Close()
				return nil
			default:
			}

			_, err := io.ReadFull(file, chunk)
			if err != nil {
				file.Close()
				if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
					break
				}
				return err
			}

			if wait := time.Until(nextTick); wait > 0 {
				time.Sleep(wait)
			}

			ret := con.PushAudioPcmData(chunk, sampleRate, channels, elapsedMs)
			if ret != 0 {
				fmt.Printf("PushAudioPcmData ret=%d\n", ret)
			}
			nextTick = nextTick.Add(10 * time.Millisecond)
			elapsedMs += 10
		}
	}
}
