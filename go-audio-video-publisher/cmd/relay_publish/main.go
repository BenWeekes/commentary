package main

import (
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"time"

	agoraservice "github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2/go_sdk/rtc"
	rtctokenbuilder "github.com/AgoraIO/Tools/DynamicKey/AgoraDynamicKey/go/src/rtctokenbuilder2"
	"github.com/benweekes/go-audio-video-publisher/internal/localstream"
)

// videoFrame holds a received encoded video frame for delay buffering.
type videoFrame struct {
	data      []byte
	frameInfo *agoraservice.EncodedVideoFrameInfo
	receiveAt time.Time
}

// atmosChunk holds a 10ms PCM audio chunk for delay buffering.
type atmosChunk struct {
	pcm       []byte // 320 bytes = 10ms at 16kHz mono S16LE
	receiveAt time.Time
}

func main() {
	appID := flag.String("app-id", envOr("AGORA_APP_ID", ""), "Agora App ID")
	appCert := flag.String("app-certificate", envOr("AGORA_APP_CERTIFICATE", ""), "Agora App Certificate")
	sourceChannel := flag.String("source-channel", "", "Source Agora channel to subscribe to")
	outputChannel := flag.String("output-channel", "", "Output Agora channel to publish to")
	videoSourceTCP := flag.String("video-source-tcp", "", "Local TCP source for cleaned H264 frames (bypasses Agora video subscription)")
	videoUID := flag.String("video-uid", "73", "Remote UID for video in source channel")
	atmosUID := flag.String("atmos-uid", "74", "Remote UID for atmosphere audio in source channel")
	atmosEnabled := flag.Bool("atmos-enabled", true, "Whether to subscribe to and mix source atmosphere audio")
	videoDelay := flag.Float64("video-delay", 7.0, "Delay in seconds for video and atmosphere")
	startAt := flag.Float64("start-at", 0, "Absolute Unix timestamp to start video (overrides video-delay)")
	subUID := flag.String("sub-uid", "77", "Local UID for subscriber connection")
	pubUID := flag.String("pub-uid", "73", "Local UID for publisher connection")
	flag.Parse()

	if *appID == "" {
		fatal("missing --app-id or AGORA_APP_ID")
	}
	if *sourceChannel == "" && *videoSourceTCP == "" {
		fatal("missing --source-channel or --video-source-tcp")
	}
	if *outputChannel == "" {
		fatal("missing --output-channel")
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt)

	var startAtTime time.Time
	if *startAt > 0 {
		sec := int64(*startAt)
		nsec := int64((*startAt - float64(sec)) * 1e9)
		startAtTime = time.Unix(sec, nsec)
	}

	cfg := &relayConfig{
		appID:          *appID,
		appCert:        *appCert,
		sourceChannel:  *sourceChannel,
		outputChannel:  *outputChannel,
		videoSourceTCP: *videoSourceTCP,
		videoUID:       *videoUID,
		atmosUID:       *atmosUID,
		atmosEnabled:   *atmosEnabled && *videoSourceTCP == "",
		videoDelay:     time.Duration(*videoDelay * float64(time.Second)),
		startAt:        startAtTime,
		subUID:         *subUID,
		pubUID:         *pubUID,
	}

	if err := run(cfg, stop); err != nil {
		fatal("relay failed: %v", err)
	}
}

type relayConfig struct {
	appID          string
	appCert        string
	sourceChannel  string
	outputChannel  string
	videoSourceTCP string
	videoUID       string
	atmosUID       string
	atmosEnabled   bool
	videoDelay     time.Duration
	startAt        time.Time // absolute start time (zero = use relative videoDelay)
	subUID         string
	pubUID         string
}

func run(cfg *relayConfig, stop <-chan os.Signal) error {
	if err := os.MkdirAll("agora_rtc_log", 0o755); err != nil {
		return fmt.Errorf("create log directory: %w", err)
	}

	// Initialize Agora service — video enabled for encoded passthrough
	svcCfg := agoraservice.NewAgoraServiceConfig()
	svcCfg.AppId = cfg.appID
	svcCfg.EnableVideo = true
	svcCfg.EnableAudioProcessor = true
	svcCfg.LogPath = "./agora_rtc_log/relay_publish.log"
	svcCfg.LogSize = 2 * 1024
	agoraservice.Initialize(svcCfg)
	defer agoraservice.Release()

	// --- Optional subscriber connection (Agora source mode only) ---
	var subCon *agoraservice.RtcConnection
	var subDisconnected <-chan string
	subDisconnectedCh := make(chan string, 1)
	disconnectSub := func() {
		if subCon != nil {
			subCon.Disconnect()
		}
	}

	// Delay buffer sizes: capacity = delay * rate * 1.5 headroom
	delaySec := cfg.videoDelay.Seconds()
	videoBufferCap := int(delaySec*30*1.5) + 100  // ~30fps assumed, +100 safety
	atmosBufferCap := int(delaySec*100*1.5) + 100 // 100 chunks/sec (10ms each)

	videoBuffer := make(chan *videoFrame, videoBufferCap)
	atmosBuffer := make(chan *atmosChunk, atmosBufferCap)
	// atmosReady receives chunks that have waited the full delay — order preserved.
	atmosReady := make(chan []byte, 200)

	var droppedVideoFrames int64
	var droppedAtmosChunks int64
	var droppedCatchupFrames int64
	var videoFrameCount int64

	if cfg.videoSourceTCP == "" {
		subToken := ""
		if cfg.appCert != "" {
			var err error
			subToken, err = rtctokenbuilder.BuildTokenWithUserAccount(
				cfg.appID, cfg.appCert, cfg.sourceChannel, cfg.subUID,
				rtctokenbuilder.RoleSubscriber, 3600, 3600)
			if err != nil {
				return fmt.Errorf("build subscriber token: %w", err)
			}
		}

		subConCfg := &agoraservice.RtcConnectionConfig{
			AutoSubscribeAudio: true,
			AutoSubscribeVideo: true,
			ClientRole:         agoraservice.ClientRoleBroadcaster,
			ChannelProfile:     agoraservice.ChannelProfileLiveBroadcasting,
		}
		subPublish := agoraservice.NewRtcConPublishConfig()
		subPublish.IsPublishAudio = false
		subPublish.IsPublishVideo = false

		subCon = agoraservice.NewRtcConnection(subConCfg, subPublish)
		if subCon == nil {
			return errors.New("failed to create subscriber connection")
		}
		defer subCon.Release()

		subConnected := make(chan struct{}, 1)
		subDisconnected = subDisconnectedCh
		subCon.RegisterObserver(&agoraservice.RtcConnectionObserver{
			OnConnected: func(_ *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
				logStderr("[SUB] connected: channel=%s uid=%s reason=%d", info.ChannelId, info.LocalUserId, reason)
				select {
				case subConnected <- struct{}{}:
				default:
				}
			},
			OnDisconnected: func(_ *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
				msg := fmt.Sprintf("[SUB] disconnected: channel=%s reason=%d", info.ChannelId, reason)
				logStderr("%s", msg)
				select {
				case subDisconnectedCh <- msg:
				default:
				}
			},
			OnUserJoined: func(_ *agoraservice.RtcConnection, uid string) {
				logStderr("[SUB] remote user joined: %s", uid)
			},
			OnUserLeft: func(_ *agoraservice.RtcConnection, uid string, reason int) {
				logStderr("[SUB] remote user left: %s reason=%d", uid, reason)
			},
		})

		subCon.RegisterVideoEncodedFrameObserver(&agoraservice.VideoEncodedFrameObserver{
			OnEncodedVideoFrame: func(uid string, imageBuffer []byte, frameInfo *agoraservice.EncodedVideoFrameInfo) bool {
				if uid != cfg.videoUID {
					return true
				}
				atomic.AddInt64(&videoFrameCount, 1)
				dataCopy := make([]byte, len(imageBuffer))
				copy(dataCopy, imageBuffer)
				infoCopy := *frameInfo
				frame := &videoFrame{
					data:      dataCopy,
					frameInfo: &infoCopy,
					receiveAt: time.Now(),
				}
				select {
				case videoBuffer <- frame:
				default:
					<-videoBuffer
					videoBuffer <- frame
					atomic.AddInt64(&droppedVideoFrames, 1)
				}
				return true
			},
		})

		subLocalUser := subCon.GetLocalUser()
		subLocalUser.SubscribeAllVideo(&agoraservice.VideoSubscriptionOptions{
			EncodedFrameOnly: true,
		})

		if cfg.atmosEnabled {
			subLocalUser.SetPlaybackAudioFrameBeforeMixingParameters(1, 16000)
			subCon.RegisterAudioFrameObserver(&agoraservice.AudioFrameObserver{
				OnPlaybackAudioFrameBeforeMixing: func(
					_ *agoraservice.LocalUser,
					channelId string,
					uid string,
					frame *agoraservice.AudioFrame,
					vadResultState agoraservice.VadState,
					vadResultFrame *agoraservice.AudioFrame,
				) bool {
					if uid != cfg.atmosUID {
						return true
					}
					now := time.Now()
					for off := 0; off+320 <= len(frame.Buffer); off += 320 {
						chunk := make([]byte, 320)
						copy(chunk, frame.Buffer[off:off+320])
						ac := &atmosChunk{pcm: chunk, receiveAt: now}
						select {
						case atmosBuffer <- ac:
						default:
							<-atmosBuffer
							atmosBuffer <- ac
							atomic.AddInt64(&droppedAtmosChunks, 1)
						}
					}
					return true
				},
			}, 0, nil)
		}

		if rc := subCon.Connect(subToken, cfg.sourceChannel, cfg.subUID); rc != 0 {
			return fmt.Errorf("subscriber connect failed: %d", rc)
		}
		select {
		case <-subConnected:
		case msg := <-subDisconnected:
			return errors.New(msg)
		case <-stop:
			return errors.New("interrupted before subscriber connected")
		}
		if cfg.atmosEnabled {
			logStderr("[SUB] subscribed to source channel %s (video UID %s, atmos UID %s)", cfg.sourceChannel, cfg.videoUID, cfg.atmosUID)
		} else {
			logStderr("[SUB] subscribed to source channel %s (video UID %s, atmosphere disabled)", cfg.sourceChannel, cfg.videoUID)
		}
		subCon.SendIntraRequest(cfg.videoUID)
	} else {
		logStderr("[SRC] using local video source %s", cfg.videoSourceTCP)
		go func() {
			for {
				conn, err := net.Dial("tcp", cfg.videoSourceTCP)
				if err != nil {
					select {
					case <-stop:
						return
					case <-time.After(500 * time.Millisecond):
						continue
					}
				}
				if tcp, ok := conn.(*net.TCPConn); ok {
					_ = tcp.SetNoDelay(true)
				}
				for {
					frame, err := localstream.ReadVideoFrame(conn)
					if err != nil {
						_ = conn.Close()
						break
					}
					atomic.AddInt64(&videoFrameCount, 1)
					frameType := agoraservice.VideoFrameTypeDeltaFrame
					if frame.KeyFrame {
						frameType = agoraservice.VideoFrameTypeKeyFrame
					}
					vf := &videoFrame{
						data: frame.Data,
						frameInfo: &agoraservice.EncodedVideoFrameInfo{
							CodecType:       agoraservice.VideoCodecTypeH264,
							Width:           frame.Width,
							Height:          frame.Height,
							FramesPerSecond: frame.FPS,
							FrameType:       frameType,
							Rotation:        agoraservice.VideoOrientation0,
							CaptureTimeMs:   time.Now().UnixMilli(),
							StreamType:      int(agoraservice.VideoStreamHigh),
						},
						receiveAt: frame.ReceiveAt,
					}
					select {
					case videoBuffer <- vf:
					default:
						<-videoBuffer
						videoBuffer <- vf
						atomic.AddInt64(&droppedVideoFrames, 1)
					}
				}
			}
		}()
	}

	// --- Publisher connection (output channel) ---
	pubToken := ""
	if cfg.appCert != "" {
		var err error
		pubToken, err = rtctokenbuilder.BuildTokenWithUserAccount(
			cfg.appID, cfg.appCert, cfg.outputChannel, cfg.pubUID,
			rtctokenbuilder.RolePublisher, 3600, 3600)
		if err != nil {
			return fmt.Errorf("build publisher token: %w", err)
		}
	}

	pubConCfg := &agoraservice.RtcConnectionConfig{
		AutoSubscribeAudio: false,
		AutoSubscribeVideo: false,
		ClientRole:         agoraservice.ClientRoleBroadcaster,
		ChannelProfile:     agoraservice.ChannelProfileLiveBroadcasting,
	}
	pubPublish := agoraservice.NewRtcConPublishConfig()
	pubPublish.IsPublishAudio = true
	pubPublish.IsPublishVideo = true
	pubPublish.AudioPublishType = agoraservice.AudioPublishTypePcm
	pubPublish.VideoPublishType = agoraservice.VideoPublishTypeEncodedImage
	pubPublish.VideoEncodedImageSenderOptions.CcMode = agoraservice.VideoSendCcEnabled
	pubPublish.VideoEncodedImageSenderOptions.CodecType = agoraservice.VideoCodecTypeH264
	pubPublish.VideoEncodedImageSenderOptions.TargetBitrate = 5000

	pubCon := agoraservice.NewRtcConnection(pubConCfg, pubPublish)
	if pubCon == nil {
		disconnectSub()
		return errors.New("failed to create publisher connection")
	}
	defer pubCon.Release()

	pubConnected := make(chan struct{}, 1)
	pubDisconnected := make(chan string, 1)
	pubCon.RegisterObserver(&agoraservice.RtcConnectionObserver{
		OnConnected: func(_ *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
			logStderr("[PUB] connected: channel=%s uid=%s reason=%d", info.ChannelId, info.LocalUserId, reason)
			select {
			case pubConnected <- struct{}{}:
			default:
			}
		},
		OnDisconnected: func(_ *agoraservice.RtcConnection, info *agoraservice.RtcConnectionInfo, reason int) {
			msg := fmt.Sprintf("[PUB] disconnected: channel=%s reason=%d", info.ChannelId, reason)
			logStderr("%s", msg)
			select {
			case pubDisconnected <- msg:
			default:
			}
		},
	})

	if rc := pubCon.Connect(pubToken, cfg.outputChannel, cfg.pubUID); rc != 0 {
		disconnectSub()
		return fmt.Errorf("publisher connect failed: %d", rc)
	}
	select {
	case <-pubConnected:
	case msg := <-pubDisconnected:
		disconnectSub()
		return errors.New(msg)
	case <-stop:
		disconnectSub()
		return errors.New("interrupted before publisher connected")
	}

	if rc := pubCon.PublishAudio(); rc != 0 {
		disconnectSub()
		pubCon.Disconnect()
		return fmt.Errorf("publish audio failed: %d", rc)
	}
	if rc := pubCon.PublishVideo(); rc != 0 {
		disconnectSub()
		pubCon.Disconnect()
		return fmt.Errorf("publish video failed: %d", rc)
	}

	// Signal readiness on stdout (Python _wait_for_publisher_signal reads stdout)
	fmt.Println("audio publishing started")

	// --- Start goroutines ---
	done := make(chan error, 3)
	var wg sync.WaitGroup

	// Video relay goroutine: pop from buffer, delay, push to publisher.
	// In --start-at mode, waits until the absolute start time before publishing
	// any frames (ensures all language relays start in sync).
	wg.Add(1)
	go func() {
		defer wg.Done()
		videoDelayComplete := false
		catchupTolerance := 200 * time.Millisecond

		// If startAt is set, wait until that time before publishing anything
		if !cfg.startAt.IsZero() {
			if wait := time.Until(cfg.startAt); wait > 0 {
				logStderr("[PUB] waiting %.1fs until shared start_at=%d", wait.Seconds(), cfg.startAt.Unix())
				select {
				case <-time.After(wait):
				case <-stop:
					done <- nil
					return
				}
			}
		}

		for {
			select {
			case <-stop:
				done <- nil
				return
			case msg := <-subDisconnected:
				done <- errors.New(msg)
				return
			case msg := <-pubDisconnected:
				done <- errors.New(msg)
				return
			case frame := <-videoBuffer:
				if !cfg.startAt.IsZero() {
					publishAt := frame.receiveAt.Add(cfg.videoDelay)
					if lateBy := time.Since(publishAt); lateBy > catchupTolerance {
						atomic.AddInt64(&droppedCatchupFrames, 1)
						continue
					}
					if wait := time.Until(publishAt); wait > 0 {
						time.Sleep(wait)
					}
				} else {
					elapsed := time.Since(frame.receiveAt)
					if remaining := cfg.videoDelay - elapsed; remaining > 0 {
						time.Sleep(remaining)
					}
				}
				rc := pubCon.PushVideoEncodedData(frame.data, frame.frameInfo)
				if rc != 0 {
					logStderr("[PUB] PushVideoEncodedData failed: %d", rc)
				}
				if !videoDelayComplete {
					videoDelayComplete = true
					// Signal on stdout for Python
					fmt.Println("video delay complete")
					logStderr("[PUB] video delay complete, first frame published to %s", cfg.outputChannel)
				}
			}
		}
	}()

	// Atmosphere delay drainer: reads from atmosBuffer (FIFO order from callback),
	// sleeps until the video delay has elapsed, then sends to atmosReady.
	// Preserves chunk ordering — the mixer never touches atmosBuffer directly.
	wg.Add(1)
	go func() {
		defer wg.Done()
		defer close(atmosReady)
		for {
			select {
			case <-stop:
				return
			case ac, ok := <-atmosBuffer:
				if !ok {
					return
				}
				if remaining := cfg.videoDelay - time.Since(ac.receiveAt); remaining > 0 {
					time.Sleep(remaining)
				}
				select {
				case atmosReady <- ac.pcm:
				case <-stop:
					return
				}
			}
		}
	}()

	// TTS stdin reader goroutine
	ttsChan := make(chan []byte, 200) // ~2 seconds of buffered TTS
	wg.Add(1)
	go func() {
		defer wg.Done()
		defer close(ttsChan)
		chunk := make([]byte, 320)
		for {
			_, err := io.ReadFull(os.Stdin, chunk)
			if err != nil {
				if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
					logStderr("[STDIN] EOF on stdin")
					done <- nil
					return
				}
				done <- fmt.Errorf("stdin read error: %w", err)
				return
			}
			c := make([]byte, 320)
			copy(c, chunk)
			select {
			case ttsChan <- c:
			case <-stop:
				return
			}
		}
	}()

	// Audio mixer goroutine: 10ms tick, mix delayed atmosphere + TTS, push to publisher
	wg.Add(1)
	go func() {
		defer wg.Done()
		silence := make([]byte, 320)
		nextTick := time.Now()
		elapsedMs := int64(0)

		for {
			select {
			case <-stop:
				return
			default:
			}

			if wait := time.Until(nextTick); wait > 0 {
				time.Sleep(wait)
			}

			// Get delayed atmosphere chunk (non-blocking, already delay-buffered)
			var atmosPCM []byte
			select {
			case pcm := <-atmosReady:
				atmosPCM = pcm
			default:
			}

			// Get TTS chunk (non-blocking)
			var ttsPCM []byte
			select {
			case t, ok := <-ttsChan:
				if ok {
					ttsPCM = t
				}
			default:
			}

			// Mix
			var output []byte
			switch {
			case atmosPCM != nil && ttsPCM != nil:
				output = mixAudio(atmosPCM, ttsPCM, 0.5)
			case atmosPCM != nil:
				output = scaleAudio(atmosPCM, 0.5)
			case ttsPCM != nil:
				output = ttsPCM
			default:
				output = silence
			}

			rc := pubCon.PushAudioPcmData(output, 16000, 1, elapsedMs)
			if rc != 0 {
				logStderr("[PUB] PushAudioPcmData failed: %d", rc)
			}
			nextTick = nextTick.Add(10 * time.Millisecond)
			elapsedMs += 10
		}
	}()

	// Periodic stats logging
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				logStderr("[STATS] video_frames=%d atmos_dropped=%d video_dropped=%d catchup_dropped=%d",
					atomic.LoadInt64(&videoFrameCount),
					atomic.LoadInt64(&droppedAtmosChunks),
					atomic.LoadInt64(&droppedVideoFrames),
					atomic.LoadInt64(&droppedCatchupFrames))
			case <-stop:
				return
			}
		}
	}()

	// Wait for shutdown
	select {
	case <-stop:
		logStderr("received interrupt, shutting down")
	case err := <-done:
		if err != nil {
			logStderr("goroutine error: %v", err)
		}
	}

	if subCon != nil {
		subCon.Disconnect()
	}
	pubCon.Disconnect()
	logStderr("relay exited cleanly")
	return nil
}

// mixAudio mixes two S16LE PCM buffers: atmosPCM at atmosVol + ttsPCM at 1.0.
func mixAudio(atmosPCM, ttsPCM []byte, atmosVol float64) []byte {
	n := len(atmosPCM)
	if len(ttsPCM) < n {
		n = len(ttsPCM)
	}
	out := make([]byte, n)
	for i := 0; i+1 < n; i += 2 {
		a := int32(int16(binary.LittleEndian.Uint16(atmosPCM[i:])))
		t := int32(int16(binary.LittleEndian.Uint16(ttsPCM[i:])))
		mixed := int32(float64(a)*atmosVol) + t
		if mixed > math.MaxInt16 {
			mixed = math.MaxInt16
		} else if mixed < math.MinInt16 {
			mixed = math.MinInt16
		}
		binary.LittleEndian.PutUint16(out[i:], uint16(int16(mixed)))
	}
	return out
}

// scaleAudio scales S16LE PCM by a volume factor.
func scaleAudio(pcm []byte, vol float64) []byte {
	out := make([]byte, len(pcm))
	for i := 0; i+1 < len(pcm); i += 2 {
		s := int32(int16(binary.LittleEndian.Uint16(pcm[i:])))
		scaled := int32(float64(s) * vol)
		if scaled > math.MaxInt16 {
			scaled = math.MaxInt16
		} else if scaled < math.MinInt16 {
			scaled = math.MinInt16
		}
		binary.LittleEndian.PutUint16(out[i:], uint16(int16(scaled)))
	}
	return out
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
