
class BridgeIO extends AudioWorkletProcessor {
  constructor(o) {
    super();
    const p = (o && o.processorOptions) || {};
    this.tx = true;
    this.tracks = new Map();
    this.setRate(p.net || 16000, p.frame || 320);
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.t === 'pcm') this.push(d.id, d.pcm);
      else if (d.t === 'drop') this.tracks.delete(d.id);
      else if (d.t === 'rate') this.setRate(d.net, d.frame);
      else if (d.t === 'tx') this.tx = !!d.on;
    };
  }
  setRate(net, frame) {
    const rateChanged = this.net !== net;
    this.net = net; this.frame = frame; this.step = net / sampleRate;
    this.acc = 0; this.sum = 0; this.cnt = 0; this.prevIn = 0;
    this.buf = new Int16Array(frame); this.n = 0;
    if (rateChanged) this.tracks = new Map();
  }
  emitSample(v) {
    if (!this.tx) return;
    v = v < -1 ? -1 : v > 1 ? 1 : v;
    this.buf[this.n++] = v < 0 ? v * 32768 : v * 32767;
    if (this.n === this.frame) {
      const out = this.buf; this.buf = new Int16Array(this.frame); this.n = 0;
      this.port.postMessage(out.buffer, [out.buffer]);
    }
  }
  push(id, pcm) {
    let t = this.tracks.get(id);
    if (!t) {
      const size = this.net * 2;
      t = {b: new Float32Array(size), size, w: 0, r: 0, on: false, last: 0, fadeFrom: 0, fadePos: 0, fadeLen: 0};
      this.tracks.set(id, t);
    }
    for (let i = 0; i < pcm.length; i++) { t.b[t.w % t.size] = pcm[i] / 32768; t.w++; }
    if (t.w - t.r > this.net * 0.4) {
      t.fadeFrom = t.last;
      t.fadeLen = Math.max(16, Math.round(sampleRate * 0.004));
      t.fadePos = 0;
      t.r = t.w - Math.round(this.net * 0.1);
    }
  }
  process(inputs, outputs) {
    const inp = inputs[0] && inputs[0][0];
    if (inp && this.tx) {
      if (this.step <= 1) {
        for (let i = 0; i < inp.length; i++) {
          this.sum += inp[i]; this.cnt++; this.acc += this.step;
          if (this.acc >= 1) {
            this.acc -= 1;
            this.emitSample(this.sum / this.cnt);
            this.sum = 0; this.cnt = 0;
          }
        }
      } else {
        const inv = 1 / this.step;
        for (let i = 0; i < inp.length; i++) {
          const s = inp[i], p = this.prevIn;
          while (this.acc < 1) {
            this.emitSample(p + (s - p) * this.acc);
            this.acc += inv;
          }
          this.acc -= 1;
          this.prevIn = s;
        }
      }
    } else if (inp && inp.length) {
      this.prevIn = inp[inp.length - 1];
      this.acc = 0; this.sum = 0; this.cnt = 0; this.n = 0;
    }
    const out = outputs[0], ch = out && out[0];
    if (!ch) return true;
    ch.fill(0);
    for (const t of this.tracks.values()) {
      if (!t.on) {
        if (t.w - t.r >= this.net * 0.04) {
          t.on = true;
          t.fadeFrom = t.last;
          t.fadeLen = Math.max(8, Math.round(sampleRate * 0.002));
          t.fadePos = 0;
        } else if (Math.abs(t.last) > 1e-4) {
          for (let i = 0; i < ch.length && Math.abs(t.last) > 1e-4; i++) {
            t.last *= 0.85;
            ch[i] += t.last;
          }
          if (Math.abs(t.last) <= 1e-4) t.last = 0;
          continue;
        } else {
          continue;
        }
      }
      const avail = t.w - t.r;
      const playStep = avail > this.net * 0.16 ? this.step * 1.015 : this.step;
      for (let i = 0; i < ch.length; i++) {
        if (t.w - t.r < 2) {
          t.on = false;
          for (; i < ch.length && Math.abs(t.last) > 1e-4; i++) {
            t.last *= 0.85;
            ch[i] += t.last;
          }
          if (Math.abs(t.last) <= 1e-4) t.last = 0;
          break;
        }
        const i0 = Math.floor(t.r), f = t.r - i0;
        const a = t.b[i0 % t.size], b = t.b[(i0 + 1) % t.size];
        let s = a + (b - a) * f;
        if (t.fadePos < t.fadeLen) {
          const alpha = t.fadePos / t.fadeLen;
          s = t.fadeFrom + (s - t.fadeFrom) * alpha;
          t.fadePos++;
        }
        t.last = s;
        ch[i] += s;
        t.r += playStep;
      }
    }
    for (let i = 0; i < ch.length; i++) { const v = ch[i]; ch[i] = v > 1 ? 1 : v < -1 ? -1 : v; }
    for (let c = 1; c < out.length; c++) out[c].set(ch);
    return true;
  }
}
registerProcessor('bridge-io', BridgeIO);
