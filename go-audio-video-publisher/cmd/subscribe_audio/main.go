package main

import (
	"bufio"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"sync/atomic"

	agoraservice "github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2/go_sdk/rtc"
	rtctokenbuilder "github.com/AgoraIO/Tools/DynamicKey/AgoraDynamicKey/go/src/rtctokenbuilder2"
)

const mediaTokenTTLSeconds = uint32(24 * 60 * 60)

func main() {
	appID := flag.String("app-id", envOr("AGORA_APP_ID", ""), "Agora App ID")
	appCert := flag.String("app-certificate", envOr("AGORA_APP_CERTIFICATE", ""), "Agora App Certificate")
	channel := flag.String("channel", "", "Source Agora channel to subscribe to")
	uid := flag.String("uid", "75", "Remote UID to capture audio from")
	localUID := flag.String("local-uid", "76", "Local UID for subscriber connection")
	flag.Parse()

	if *appID == "" {
		fatal("missing --app-id or AGORA_APP_ID")
	}
	if *channel == "" {
		fatal("missing --channel")
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt)

	if err := run(*appID, *appCert, *channel, *uid, *localUID, stop); err != nil {
		fatal("subscriber failed: %v", err)
	}
}

func run(appID, appCert, channel, targetUID, localUID string, stop <-chan os.Signal) error {
	// Build subscriber token (join only, no publish)
	token := ""
	if appCert != "" {
		var err error
		token, err = rtctokenbuilder.BuildTokenWithUserAccount(
			appID, appCert, channel, localUID,
			rtctokenbuilder.RoleSubscriber, mediaTokenTTLSeconds, mediaTokenTTLSeconds)
		if err != nil {
			return fmt.Errorf("build token: %w", err)
		}
	}

	// Initialize Agora service — audio only, no video
	if err := os.MkdirAll("agora_rtc_log", 0o755); err != nil {
		return fmt.Errorf("create log directory: %w", err)
	}
	svcCfg := agoraservice.NewAgoraServiceConfig()
	svcCfg.AppId = appID
	svcCfg.EnableVideo = false
	svcCfg.EnableAudioProcessor = true
	svcCfg.LogPath = "./agora_rtc_log/subscribe_audio.log"
	svcCfg.LogSize = 2 * 1024
	agoraservice.Initialize(svcCfg)
	defer agoraservice.Release()

	// Connection config: subscribe audio, no publish
	conCfg := &agoraservice.RtcConnectionConfig{
		AutoSubscribeAudio: true,
		AutoSubscribeVideo: false,
		ClientRole:         agoraservice.ClientRoleBroadcaster,
		ChannelProfile:     agoraservice.ChannelProfileLiveBroadcasting,
	}
	publishConfig := agoraservice.NewRtcConPublishConfig()
	publishConfig.IsPublishAudio = false
	publishConfig.IsPublishVideo = false

	con := agoraservice.NewRtcConnection(conCfg, publishConfig)
	if con == nil {
		return errors.New("failed to create rtc connection")
	}
	defer con.Release()

	connected := make(chan struct{}, 1)
	disconnected := make(chan string, 1)
	con.RegisterObserver(&agoraservice.RtcConnectionObserver{
		OnConnected: func(_ *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
			logStderr("connected: channel=%s uid=%s reason=%d", info.ChannelId, info.LocalUserId, reason)
			select {
			case connected <- struct{}{}:
			default:
			}
		},
		OnDisconnected: func(_ *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
			msg := fmt.Sprintf("disconnected: channel=%s uid=%s reason=%d", info.ChannelId, info.LocalUserId, reason)
			logStderr("%s", msg)
			select {
			case disconnected <- msg:
			default:
			}
		},
		OnUserJoined: func(_ *agoraservice.RtcConnection, uid string) {
			logStderr("remote user joined: %s", uid)
		},
		OnUserLeft: func(_ *agoraservice.RtcConnection, uid string, reason int) {
			logStderr("remote user left: %s reason=%d", uid, reason)
			if uid == targetUID {
				logStderr("target UID %s left channel, exiting", targetUID)
				select {
				case disconnected <- fmt.Sprintf("target UID %s left", targetUID):
				default:
				}
			}
		},
	})

	// Set up audio frame observer for per-UID PCM capture
	localUser := con.GetLocalUser()
	localUser.SetPlaybackAudioFrameBeforeMixingParameters(1, 16000) // mono, 16kHz

	// Buffered channel so the SDK callback never blocks on pipe I/O.
	// 500 frames ≈ 5s at 10ms/frame — enough to absorb reader stalls.
	pcmChan := make(chan []byte, 500)
	var droppedFrames int64
	var frameCount int64

	// Dedicated writer goroutine: drains pcmChan → stdout (buffered)
	writerDone := make(chan struct{})
	go func() {
		defer close(writerDone)
		w := bufio.NewWriterSize(os.Stdout, 32*1024) // 32KB buffer
		for buf := range pcmChan {
			if _, err := w.Write(buf); err != nil {
				logStderr("stdout write error: %v", err)
				return
			}
		}
		w.Flush()
	}()

	audioObserver := &agoraservice.AudioFrameObserver{
		OnPlaybackAudioFrameBeforeMixing: func(
			_ *agoraservice.LocalUser,
			channelId string,
			uid string,
			frame *agoraservice.AudioFrame,
			vadResultState agoraservice.VadState,
			vadResultFrame *agoraservice.AudioFrame,
		) bool {
			if uid != targetUID {
				return true
			}
			if len(frame.Buffer) > 0 {
				// Copy buffer — SDK may reuse the slice
				buf := make([]byte, len(frame.Buffer))
				copy(buf, frame.Buffer)
				select {
				case pcmChan <- buf:
				default:
					// Channel full — drop oldest, enqueue new (non-blocking)
					select {
					case <-pcmChan:
						atomic.AddInt64(&droppedFrames, 1)
					default:
					}
					select {
					case pcmChan <- buf:
					default:
						atomic.AddInt64(&droppedFrames, 1)
					}
				}
				n := atomic.AddInt64(&frameCount, 1)
				if n == 1 {
					logStderr("first audio frame received from UID %s (%d bytes)", uid, len(frame.Buffer))
				}
			}
			return true
		},
	}
	con.RegisterAudioFrameObserver(audioObserver, 0, nil) // VAD disabled

	// Connect
	if rc := con.Connect(token, channel, localUID); rc != 0 {
		close(pcmChan)
		return fmt.Errorf("connect failed: %d", rc)
	}

	select {
	case <-connected:
	case msg := <-disconnected:
		close(pcmChan)
		return errors.New(msg)
	case <-stop:
		close(pcmChan)
		return errors.New("interrupted before connection completed")
	}

	logStderr("audio subscribing started")

	// Block until signal or disconnect
	select {
	case <-stop:
		logStderr("received interrupt, shutting down")
	case msg := <-disconnected:
		logStderr("disconnect: %s", msg)
	}

	con.Disconnect()
	close(pcmChan)
	<-writerDone // wait for writer to flush
	dropped := atomic.LoadInt64(&droppedFrames)
	logStderr("subscriber exited cleanly, frames=%d dropped=%d", atomic.LoadInt64(&frameCount), dropped)
	return nil
}

func logStderr(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
}

func fatal(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}

func envOr(name string, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
