package main

import (
	"fmt"
	"net"
	"sync"
	"time"

	agoraservice "github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2/go_sdk/rtc"
	"github.com/benweekes/go-audio-video-publisher/internal/localstream"
)

type pcmServer struct {
	listener net.Listener
	addr     string

	mu   sync.Mutex
	conn net.Conn
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
		if s.conn != nil {
			_ = s.conn.Close()
		}
		s.conn = conn
		s.mu.Unlock()
	}
}

func (s *pcmServer) writeChunk(chunk []byte) {
	if s == nil || len(chunk) == 0 {
		return
	}
	s.mu.Lock()
	conn := s.conn
	s.mu.Unlock()
	if conn == nil {
		return
	}
	if tcp, ok := conn.(*net.TCPConn); ok {
		_ = tcp.SetWriteDeadline(time.Now().Add(250 * time.Millisecond))
	}
	if _, err := conn.Write(chunk); err != nil {
		s.mu.Lock()
		if s.conn == conn {
			_ = s.conn.Close()
			s.conn = nil
		}
		s.mu.Unlock()
	}
}

func (s *pcmServer) close() {
	if s == nil {
		return
	}
	_ = s.listener.Close()
	s.mu.Lock()
	if s.conn != nil {
		_ = s.conn.Close()
		s.conn = nil
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
