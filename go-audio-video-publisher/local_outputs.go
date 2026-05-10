package main

import (
	"encoding/binary"
	"fmt"
	"math"
	"net"
	"sync"
	"time"

	agoraservice "github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2/go_sdk/rtc"
	"github.com/benweekes/go-audio-video-publisher/internal/localstream"
)

type pcmServer struct {
	listener net.Listener
	addr     string

	mu      sync.Mutex
	nextID  int
	clients map[int]net.Conn
}

func startPCMServer(addr string) (*pcmServer, error) {
	if addr == "" {
		return nil, nil
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("listen pcm: %w", err)
	}
	s := &pcmServer{
		listener: ln,
		addr:     ln.Addr().String(),
		clients:  map[int]net.Conn{},
	}
	go s.acceptLoop()
	return s, nil
}

func (s *pcmServer) acceptLoop() {
	for {
		conn, err := s.listener.Accept()
		if err != nil {
			return
		}
		s.mu.Lock()
		id := s.nextID
		s.nextID++
		s.clients[id] = conn
		s.mu.Unlock()
	}
}

func (s *pcmServer) writeChunk(chunk []byte) {
	if s == nil || len(chunk) == 0 {
		return
	}
	s.mu.Lock()
	clients := make(map[int]net.Conn, len(s.clients))
	for id, conn := range s.clients {
		clients[id] = conn
	}
	s.mu.Unlock()

	var failures []int
	for id, conn := range clients {
		if tcp, ok := conn.(*net.TCPConn); ok {
			_ = tcp.SetWriteDeadline(time.Now().Add(250 * time.Millisecond))
		}
		if _, err := conn.Write(chunk); err != nil {
			failures = append(failures, id)
		}
	}
	for _, id := range failures {
		if conn, ok := clients[id]; ok {
			s.removeClient(id, conn)
		}
	}
}

func (s *pcmServer) removeClient(id int, conn net.Conn) {
	s.mu.Lock()
	if existing, ok := s.clients[id]; ok && existing == conn {
		delete(s.clients, id)
	}
	s.mu.Unlock()
	_ = conn.Close()
}

func (s *pcmServer) close() {
	if s == nil {
		return
	}
	_ = s.listener.Close()
	s.mu.Lock()
	for id, conn := range s.clients {
		_ = conn.Close()
		delete(s.clients, id)
	}
	s.mu.Unlock()
}

type videoFanout struct {
	listener net.Listener
	addr     string

	mu        sync.Mutex
	nextID    int
	clients   map[int]net.Conn
	latestKey *localstream.VideoFrame
	closed    bool
}

func startVideoFanout(addr string) (*videoFanout, error) {
	if addr == "" {
		return nil, nil
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("listen video: %w", err)
	}
	v := &videoFanout{
		listener: ln,
		addr:     ln.Addr().String(),
		clients:  map[int]net.Conn{},
	}
	go v.acceptLoop()
	return v, nil
}

func (v *videoFanout) acceptLoop() {
	for {
		conn, err := v.listener.Accept()
		if err != nil {
			return
		}
		if tcp, ok := conn.(*net.TCPConn); ok {
			_ = tcp.SetNoDelay(true)
		}
		v.mu.Lock()
		if v.closed {
			v.mu.Unlock()
			_ = conn.Close()
			return
		}
		id := v.nextID
		v.nextID++
		v.clients[id] = conn
		key := v.latestKey
		v.mu.Unlock()

		if key != nil {
			if err := localstream.WriteVideoFrame(conn, *key); err != nil {
				v.removeClient(id, conn)
			}
		}
	}
}

func (v *videoFanout) removeClient(id int, conn net.Conn) {
	v.mu.Lock()
	if existing, ok := v.clients[id]; ok && existing == conn {
		delete(v.clients, id)
	}
	v.mu.Unlock()
	_ = conn.Close()
}

func (v *videoFanout) broadcast(frame localstream.VideoFrame) {
	if v == nil || len(frame.Data) == 0 {
		return
	}
	if frame.KeyFrame {
		copyFrame := frame
		copyFrame.Data = append([]byte(nil), frame.Data...)
		v.mu.Lock()
		v.latestKey = &copyFrame
		v.mu.Unlock()
	}

	v.mu.Lock()
	clients := make(map[int]net.Conn, len(v.clients))
	for id, conn := range v.clients {
		clients[id] = conn
	}
	v.mu.Unlock()

	var failures []int
	for id, conn := range clients {
		if tcp, ok := conn.(*net.TCPConn); ok {
			_ = tcp.SetWriteDeadline(time.Now().Add(250 * time.Millisecond))
		}
		if err := localstream.WriteVideoFrame(conn, frame); err != nil {
			failures = append(failures, id)
		}
	}
	for _, id := range failures {
		if conn, ok := clients[id]; ok {
			v.removeClient(id, conn)
		}
	}
}

func (v *videoFanout) close() {
	if v == nil {
		return
	}
	_ = v.listener.Close()
	v.mu.Lock()
	v.closed = true
	for id, conn := range v.clients {
		_ = conn.Close()
		delete(v.clients, id)
	}
	v.mu.Unlock()
}

type delayedAudioChunk struct {
	pcm       []byte
	publishAt time.Time
}

type delayedVideoFrame struct {
	frameInfo *agoraservice.EncodedVideoFrameInfo
	data      []byte
	publishAt time.Time
}

type delayedAudioPublisher struct {
	con   *agoraservice.RtcConnection
	queue chan delayedAudioChunk
}

type sourceAudioChunk struct {
	pcm       []byte
	publishAt time.Time
	isAtmos   bool
}

type sourceAudioMixer struct {
	con *agoraservice.RtcConnection
	in  chan sourceAudioChunk
}

func startSourceAudioMixer(con *agoraservice.RtcConnection) *sourceAudioMixer {
	m := &sourceAudioMixer{
		con: con,
		in:  make(chan sourceAudioChunk, 800),
	}
	go m.run()
	return m
}

func (m *sourceAudioMixer) enqueue(chunk sourceAudioChunk) {
	if m == nil {
		return
	}
	m.in <- chunk
}

func (m *sourceAudioMixer) close() {
	if m == nil {
		return
	}
	close(m.in)
}

func (m *sourceAudioMixer) run() {
	const tick = 10 * time.Millisecond
	silence := make([]byte, 320)
	var commentary []sourceAudioChunk
	var atmos []sourceAudioChunk
	var nextTick time.Time
	elapsedMs := int64(0)
	inputOpen := true

	for inputOpen || len(commentary) > 0 || len(atmos) > 0 {
		if nextTick.IsZero() {
			chunk, ok := <-m.in
			if !ok {
				return
			}
			if chunk.isAtmos {
				atmos = append(atmos, chunk)
			} else {
				commentary = append(commentary, chunk)
			}
			nextTick = chunk.publishAt
		}

		collectUntil := time.Until(nextTick)
		if collectUntil > 0 {
			timer := time.NewTimer(collectUntil)
			collecting := true
			for collecting {
				select {
				case chunk, ok := <-m.in:
					if !ok {
						inputOpen = false
						collecting = false
						break
					}
					if chunk.isAtmos {
						atmos = append(atmos, chunk)
					} else {
						commentary = append(commentary, chunk)
					}
				case <-timer.C:
					collecting = false
				}
			}
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
		}

		drainReady := true
		for drainReady {
			select {
			case chunk, ok := <-m.in:
				if !ok {
					inputOpen = false
					drainReady = false
					break
				}
				if chunk.isAtmos {
					atmos = append(atmos, chunk)
				} else {
					commentary = append(commentary, chunk)
				}
			default:
				drainReady = false
			}
		}

		commentaryPCM, newCommentary := popDueAudioChunk(commentary, nextTick)
		commentary = newCommentary
		atmosPCM, newAtmos := popDueAudioChunk(atmos, nextTick)
		atmos = newAtmos

		output := silence
		switch {
		case commentaryPCM != nil && atmosPCM != nil:
			output = mixPCM(commentaryPCM, atmosPCM, 1.0, 0.35)
		case commentaryPCM != nil:
			output = commentaryPCM
		case atmosPCM != nil:
			output = scalePCM(atmosPCM, 0.35)
		}

		if rc := m.con.PushAudioPcmData(output, 16000, 1, elapsedMs); rc != 0 {
			fmt.Printf("PushAudioPcmData ret=%d size=%d\n", rc, len(output))
		} else {
			signalSourcePublishingStarted()
		}
		nextTick = nextTick.Add(tick)
		elapsedMs += 10
	}
}

func popDueAudioChunk(chunks []sourceAudioChunk, tickAt time.Time) ([]byte, []sourceAudioChunk) {
	if len(chunks) == 0 {
		return nil, chunks
	}
	dueWindow := tickAt.Add(5 * time.Millisecond)
	if chunks[0].publishAt.After(dueWindow) {
		return nil, chunks
	}
	pcm := chunks[0].pcm
	return pcm, chunks[1:]
}

func mixPCM(primary, secondary []byte, primaryVol, secondaryVol float64) []byte {
	n := len(primary)
	if len(secondary) < n {
		n = len(secondary)
	}
	out := make([]byte, n)
	for i := 0; i+1 < n; i += 2 {
		a := float64(int16(binary.LittleEndian.Uint16(primary[i:]))) * primaryVol
		b := float64(int16(binary.LittleEndian.Uint16(secondary[i:]))) * secondaryVol
		mixed := int32(a + b)
		if mixed > math.MaxInt16 {
			mixed = math.MaxInt16
		} else if mixed < math.MinInt16 {
			mixed = math.MinInt16
		}
		binary.LittleEndian.PutUint16(out[i:], uint16(int16(mixed)))
	}
	return out
}

func scalePCM(pcm []byte, vol float64) []byte {
	out := make([]byte, len(pcm))
	for i := 0; i+1 < len(pcm); i += 2 {
		scaled := int32(float64(int16(binary.LittleEndian.Uint16(pcm[i:]))) * vol)
		if scaled > math.MaxInt16 {
			scaled = math.MaxInt16
		} else if scaled < math.MinInt16 {
			scaled = math.MinInt16
		}
		binary.LittleEndian.PutUint16(out[i:], uint16(int16(scaled)))
	}
	return out
}

func startDelayedAudioPublisher(con *agoraservice.RtcConnection) *delayedAudioPublisher {
	p := &delayedAudioPublisher{
		con:   con,
		queue: make(chan delayedAudioChunk, 400),
	}
	go p.run()
	return p
}

func (p *delayedAudioPublisher) enqueue(chunk delayedAudioChunk) {
	if p == nil {
		return
	}
	p.queue <- chunk
}

func (p *delayedAudioPublisher) close() {
	if p == nil {
		return
	}
	close(p.queue)
}

func (p *delayedAudioPublisher) run() {
	elapsedMs := int64(0)
	for chunk := range p.queue {
		if wait := time.Until(chunk.publishAt); wait > 0 {
			time.Sleep(wait)
		}
		if rc := p.con.PushAudioPcmData(chunk.pcm, 16000, 1, elapsedMs); rc != 0 {
			fmt.Printf("PushAudioPcmData ret=%d size=%d\n", rc, len(chunk.pcm))
		} else {
			signalSourcePublishingStarted()
		}
		elapsedMs += 10
	}
}

type delayedVideoPublisher struct {
	con   *agoraservice.RtcConnection
	queue chan delayedVideoFrame
}

func startDelayedVideoPublisher(con *agoraservice.RtcConnection) *delayedVideoPublisher {
	p := &delayedVideoPublisher{
		con:   con,
		queue: make(chan delayedVideoFrame, 400),
	}
	go p.run()
	return p
}

func (p *delayedVideoPublisher) enqueue(frame delayedVideoFrame) {
	if p == nil {
		return
	}
	p.queue <- frame
}

func (p *delayedVideoPublisher) close() {
	if p == nil {
		return
	}
	close(p.queue)
}

func (p *delayedVideoPublisher) run() {
	for frame := range p.queue {
		if wait := time.Until(frame.publishAt); wait > 0 {
			time.Sleep(wait)
		}
		if rc := p.con.PushVideoEncodedData(frame.data, frame.frameInfo); rc != 0 {
			fmt.Printf("PushVideoEncodedData ret=%d size=%d key=%t\n", rc, len(frame.data), frame.frameInfo.FrameType == agoraservice.VideoFrameTypeKeyFrame)
		} else {
			signalSourcePublishingStarted()
		}
	}
}
