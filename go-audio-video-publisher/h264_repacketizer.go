package main

type h264Repacketizer struct {
	sps      []byte
	pps      []byte
	sentSEI  bool
	keyCount int
}

func (r *h264Repacketizer) repacketize(au h264AccessUnit) h264AccessUnit {
	nals := scanNALs(au.data, true)
	if len(nals) == 0 {
		return h264AccessUnit{}
	}

	var kept [][]byte
	key := false
	for _, nal := range nals {
		nalBytes := append([]byte(nil), au.data[nal.start:nal.end+1]...)
		switch nal.nalType {
		case 7:
			r.sps = nalBytes
		case 8:
			r.pps = nalBytes
		case 5:
			key = true
		}
	}

	if key {
		if len(r.sps) > 0 {
			kept = append(kept, r.sps)
		}
		if len(r.pps) > 0 {
			kept = append(kept, r.pps)
		}
		if !r.sentSEI {
			for _, nal := range nals {
				if nal.nalType == 6 {
					kept = append(kept, append([]byte(nil), au.data[nal.start:nal.end+1]...))
					r.sentSEI = true
					break
				}
			}
		}
		for _, nal := range nals {
			if nal.nalType == 5 {
				kept = append(kept, append([]byte(nil), au.data[nal.start:nal.end+1]...))
			}
		}
	} else {
		for _, nal := range nals {
			if nal.nalType == 1 {
				kept = append(kept, append([]byte(nil), au.data[nal.start:nal.end+1]...))
			}
		}
	}

	if len(kept) == 0 {
		return h264AccessUnit{}
	}

	var out []byte
	for _, nal := range kept {
		out = append(out, nal...)
	}
	r.keyCount++
	return h264AccessUnit{data: out, isKeyFrame: key}
}
