package main

type h264NAL struct {
	start     int
	end       int
	headerPos int
	nalType   byte
}

type h264AccessUnit struct {
	data       []byte
	isKeyFrame bool
}

type h264AUParser struct {
	buf            []byte
	seenKeyFrame   bool
	droppedPreRoll int
}

func (p *h264AUParser) appendAndExtract(data []byte, flush bool) []h264AccessUnit {
	if len(data) == 0 {
		return nil
	}
	p.buf = append(p.buf, data...)
	var out []h264AccessUnit
	for {
		au, consumed, ok := extractH264AU(p.buf, flush)
		if !ok {
			// Trim leading junk before the first start code.
			if idx := findStartCode(p.buf, 0); idx > 0 {
				p.buf = append([]byte(nil), p.buf[idx:]...)
			}
			break
		}
		if consumed <= 0 || consumed > len(p.buf) {
			break
		}
		p.buf = append([]byte(nil), p.buf[consumed:]...)
		if !p.seenKeyFrame {
			if !au.isKeyFrame {
				p.droppedPreRoll++
				continue
			}
			p.seenKeyFrame = true
		}
		out = append(out, au)
	}
	return out
}

func extractH264AU(buf []byte, allowEOF bool) (h264AccessUnit, int, bool) {
	nals := scanNALs(buf, allowEOF)
	if len(nals) == 0 {
		return h264AccessUnit{}, 0, false
	}
	firstVCL := -1
	for i, nal := range nals {
		if isVCLNAL(nal.nalType) {
			firstVCL = i
			break
		}
	}
	if firstVCL < 0 {
		return h264AccessUnit{}, 0, false
	}

	prevFirstMB := -1
	prevNALType := byte(0)
	isKey := false
	for i := firstVCL; i < len(nals); i++ {
		nal := nals[i]
		if !isVCLNAL(nal.nalType) {
			continue
		}
		firstMB, sliceType, ok := parseSliceHeader(buf[nal.headerPos : nal.end+1])
		if !ok {
			return h264AccessUnit{}, 0, false
		}
		if i == firstVCL {
			if nal.nalType == 5 {
				isKey = true
			} else {
				st := sliceType % 5
				isKey = st == 2 || st == 4
			}
			prevFirstMB = firstMB
			prevNALType = nal.nalType
			continue
		}
		if prevNALType != nal.nalType || firstMB <= prevFirstMB {
			start := nals[0].start
			end := nal.start
			return h264AccessUnit{
				data:       append([]byte(nil), buf[start:end]...),
				isKeyFrame: isKey,
			}, end, true
		}
		prevFirstMB = firstMB
		prevNALType = nal.nalType
	}

	for i := firstVCL + 1; i < len(nals); i++ {
		nal := nals[i]
		if nal.nalType == 9 || nal.nalType == 7 || nal.nalType == 8 {
			start := nals[0].start
			end := nal.start
			return h264AccessUnit{
				data:       append([]byte(nil), buf[start:end]...),
				isKeyFrame: isKey,
			}, end, true
		}
	}

	if allowEOF {
		start := nals[0].start
		return h264AccessUnit{
			data:       append([]byte(nil), buf[start:]...),
			isKeyFrame: isKey,
		}, len(buf), true
	}
	return h264AccessUnit{}, 0, false
}

func scanNALs(buf []byte, allowEOF bool) []h264NAL {
	var nals []h264NAL
	for pos := findStartCode(buf, 0); pos >= 0; {
		scLen := startCodeLen(buf, pos)
		if scLen == 0 || pos+scLen >= len(buf) {
			break
		}
		next := findStartCode(buf, pos+scLen)
		if next < 0 {
			if !allowEOF {
				break
			}
			next = len(buf)
		}
		headerPos := pos + scLen
		nals = append(nals, h264NAL{
			start:     pos,
			end:       next - 1,
			headerPos: headerPos,
			nalType:   buf[headerPos] & 0x1f,
		})
		if next >= len(buf) {
			break
		}
		pos = next
	}
	return nals
}

func findStartCode(buf []byte, start int) int {
	for i := start; i+3 < len(buf); i++ {
		if buf[i] == 0 && buf[i+1] == 0 {
			if buf[i+2] == 1 {
				return i
			}
			if i+3 < len(buf) && buf[i+2] == 0 && buf[i+3] == 1 {
				return i
			}
		}
	}
	return -1
}

func startCodeLen(buf []byte, pos int) int {
	if pos+3 >= len(buf) {
		return 0
	}
	if buf[pos] == 0 && buf[pos+1] == 0 && buf[pos+2] == 1 {
		return 3
	}
	if pos+4 <= len(buf) && buf[pos] == 0 && buf[pos+1] == 0 && buf[pos+2] == 0 && buf[pos+3] == 1 {
		return 4
	}
	return 0
}

func isVCLNAL(t byte) bool {
	return t == 1 || t == 5
}

func parseSliceHeader(payload []byte) (firstMB int, sliceType int, ok bool) {
	if len(payload) < 2 {
		return 0, 0, false
	}
	br := bitReader{data: payload[1:]}
	firstMB, ok = br.readUE()
	if !ok {
		return 0, 0, false
	}
	sliceType, ok = br.readUE()
	if !ok {
		return 0, 0, false
	}
	return firstMB, sliceType, true
}

type bitReader struct {
	data []byte
	bit  int
}

func (r *bitReader) readBit() (int, bool) {
	if r.bit >= len(r.data)*8 {
		return 0, false
	}
	v := int((r.data[r.bit/8] >> (7 - uint(r.bit%8))) & 1)
	r.bit++
	return v, true
}

func (r *bitReader) readUE() (int, bool) {
	zeros := 0
	for {
		bit, ok := r.readBit()
		if !ok {
			return 0, false
		}
		if bit == 1 {
			break
		}
		zeros++
	}
	value := 1
	for i := 0; i < zeros; i++ {
		bit, ok := r.readBit()
		if !ok {
			return 0, false
		}
		value = (value << 1) | bit
	}
	return value - 1, true
}
