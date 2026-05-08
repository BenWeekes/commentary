package localstream

import (
	"encoding/binary"
	"fmt"
	"io"
	"time"
)

var videoMagic = [4]byte{'L', 'V', 'F', '1'}

const videoHeaderSize = 40

type VideoFrame struct {
	Data      []byte
	Width     int
	Height    int
	FPS       int
	KeyFrame  bool
	ReceiveAt time.Time
}

func WriteVideoFrame(w io.Writer, frame VideoFrame) error {
	header := make([]byte, videoHeaderSize)
	copy(header[:4], videoMagic[:])
	if frame.KeyFrame {
		header[4] = 1
	}
	binary.BigEndian.PutUint32(header[8:12], uint32(frame.Width))
	binary.BigEndian.PutUint32(header[12:16], uint32(frame.Height))
	binary.BigEndian.PutUint32(header[16:20], uint32(frame.FPS))
	binary.BigEndian.PutUint64(header[20:28], uint64(frame.ReceiveAt.UnixNano()))
	binary.BigEndian.PutUint32(header[28:32], uint32(len(frame.Data)))
	if _, err := w.Write(header); err != nil {
		return err
	}
	if len(frame.Data) == 0 {
		return nil
	}
	_, err := w.Write(frame.Data)
	return err
}

func ReadVideoFrame(r io.Reader) (VideoFrame, error) {
	var frame VideoFrame
	header := make([]byte, videoHeaderSize)
	if _, err := io.ReadFull(r, header); err != nil {
		return frame, err
	}
	if string(header[:4]) != string(videoMagic[:]) {
		return frame, fmt.Errorf("invalid video frame magic")
	}
	payloadLen := int(binary.BigEndian.Uint32(header[28:32]))
	if payloadLen < 0 {
		return frame, fmt.Errorf("invalid payload length")
	}
	data := make([]byte, payloadLen)
	if payloadLen > 0 {
		if _, err := io.ReadFull(r, data); err != nil {
			return frame, err
		}
	}
	frame = VideoFrame{
		Data:      data,
		Width:     int(binary.BigEndian.Uint32(header[8:12])),
		Height:    int(binary.BigEndian.Uint32(header[12:16])),
		FPS:       int(binary.BigEndian.Uint32(header[16:20])),
		KeyFrame:  header[4] == 1,
		ReceiveAt: time.Unix(0, int64(binary.BigEndian.Uint64(header[20:28]))),
	}
	return frame, nil
}
