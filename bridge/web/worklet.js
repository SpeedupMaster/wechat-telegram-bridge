
class BridgeIO extends AudioWorkletProcessor {
  constructor(o) {
    super();
    const p = (o && o.processorOptions) || {};
    this.setRate(p.net || 16000, p.frame || 320);
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.t === 'pcm') this.push(d.id, d.pcm);
      else if (d.t === 'drop') this.tracks.delete(d.id);
      else if (d.t === 'rate') this.setRate(d.net, d.frame);
    };
  }
  setRate(net, frame) {
    this.net = net; this.frame = frame; this.step = net / sampleRate;
    this.acc = 0; this.sum = 0; this.cnt = 0; this.buf = new Int16Array(frame); this.n = 0;
    this.tracks = new Map();
  }
  push(id, pcm) {
    let t = this.tracks.get(id);
    if (!t) { const size = this.net * 2; t = {b: new Float32Array(size), size, w: 0, r: 0, on: false}; this.tracks.set(id, t); }
    for (let i = 0; i < pcm.length; i++) { t.b[t.w % t.size] = pcm[i] / 32768; t.w++; }
    if (t.w - t.r > this.net * 0.4) t.r = t.w - this.net * 0.12;  // cap latency
  }
  process(inputs, outputs) {
    const inp = inputs[0] && inputs[0][0];
    if (inp) {
      for (let i = 0; i < inp.length; i++) {
        this.sum += inp[i]; this.cnt++; this.acc += this.step;
        if (this.acc >= 1) {
          this.acc -= 1;
          let v = this.sum / this.cnt; this.sum = 0; this.cnt = 0;
          v = v < -1 ? -1 : v > 1 ? 1 : v;
          this.buf[this.n++] = v < 0 ? v * 32768 : v * 32767;
          if (this.n === this.frame) {
            const out = this.buf; this.buf = new Int16Array(this.frame); this.n = 0;
            this.port.postMessage(out.buffer, [out.buffer]);
          }
        }
      }
    }
    const out = outputs[0], ch = out && out[0];
    if (!ch) return true;
    ch.fill(0);
    for (const t of this.tracks.values()) {
      if (!t.on) { if (t.w - t.r >= this.net * 0.06) t.on = true; else continue; }
      for (let i = 0; i < ch.length; i++) {
        if (t.w - t.r < 2) { t.on = false; break; }
        const i0 = Math.floor(t.r), f = t.r - i0;
        const a = t.b[i0 % t.size], b = t.b[(i0 + 1) % t.size];
        ch[i] += a + (b - a) * f; t.r += this.step;
      }
    }
    for (let i = 0; i < ch.length; i++) { const v = ch[i]; ch[i] = v > 1 ? 1 : v < -1 ? -1 : v; }
    for (let c = 1; c < out.length; c++) out[c].set(ch);
    return true;
  }
}
registerProcessor('bridge-io', BridgeIO);
