
class BridgeIO extends AudioWorkletProcessor {
  constructor(o) {
    super();
    const p = (o && o.processorOptions) || {};
    this.tx = true;
    this.speaker = false;
    this.stat = !!p.stat;
    this.ticks = 0;
    this.txFrames = 0;
    this.txNonZero = 0;
    this.rxFrames = 0;
    this.inPeak = 0;
    this.txPeak = 0;
    this.framePeak = 0;
    this.rxEnv = 0;
    this.micGain = 1;
    this.tracks = new Map();
    this.setRate(p.net || 16000, p.frame || 320);
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.t === 'pcm') this.push(d.id, d.pcm);
      else if (d.t === 'drop') this.tracks.delete(d.id);
      else if (d.t === 'rate') this.setRate(d.net, d.frame);
      else if (d.t === 'tx') this.tx = !!d.on;
      else if (d.t === 'speaker') this.speaker = !!d.on;
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
    if (!Number.isFinite(v)) v = 0;
    v = v < -1 ? -1 : v > 1 ? 1 : v;
    const s16 = v < 0 ? Math.round(v * 32768) : Math.round(v * 32767);
    this.buf[this.n++] = s16;
    const a16 = s16 < 0 ? -s16 : s16;
    if (a16 > this.framePeak) this.framePeak = a16;
    if (this.n === this.frame) {
      this.txFrames++;
      if (this.framePeak > 0) this.txNonZero++;
      if (this.framePeak > this.txPeak) this.txPeak = this.framePeak;
      this.framePeak = 0;
      const out = this.buf; this.buf = new Int16Array(this.frame); this.n = 0;
      this.port.postMessage(out.buffer, [out.buffer]);
    }
  }
  push(id, pcm) {
    this.rxFrames++;
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
    // 1) Render far-end receive tracks first and update acoustic echo tail envelope (rxEnv)
    const out = outputs[0], ch = out && out[0];
    if (ch) {
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
      let rxEnergy = 0;
      for (let i = 0; i < ch.length; i++) {
        let v = ch[i];
        v = v > 1 ? 1 : v < -1 ? -1 : v;
        ch[i] = v;
        rxEnergy += v * v;
      }
      for (let c = 1; c < out.length; c++) out[c].set(ch);
      const rxRms = ch.length ? Math.sqrt(rxEnergy / ch.length) : 0;
      const decaySec = this.speaker ? 0.18 : 0.14;
      const holdDecay = Math.exp(-ch.length / (sampleRate * decaySec));
      this.rxEnv = rxRms > this.rxEnv ? (this.rxEnv + (rxRms - this.rxEnv) * 0.65) : (this.rxEnv * holdDecay);
    }

    // 2) Capture & resample near-end microphone with multi-channel downmix and residual echo suppression
    const inChans = inputs[0];
    const ch0 = inChans && inChans[0];
    let inp = ch0;
    if (inChans && inChans.length > 1 && ch0 && ch0.length) {
      if (!this.monoBuf || this.monoBuf.length !== ch0.length) this.monoBuf = new Float32Array(ch0.length);
      let activeChans = 0, soleChan = null;
      for (let c = 0; c < inChans.length; c++) {
        const chArr = inChans[c];
        if (!chArr) continue;
        for (let i = 0; i < chArr.length; i++) {
          if (chArr[i] !== 0 && Number.isFinite(chArr[i])) { activeChans++; soleChan = chArr; break; }
        }
      }
      if (activeChans === 1 && soleChan) {
        inp = soleChan;
      } else if (activeChans > 1) {
        const scale = 1 / activeChans;
        this.monoBuf.fill(0);
        for (let c = 0; c < inChans.length; c++) {
          const chArr = inChans[c];
          if (!chArr) continue;
          for (let i = 0; i < ch0.length; i++) {
            const v = chArr[i];
            if (Number.isFinite(v)) this.monoBuf[i] += v * scale;
          }
        }
        inp = this.monoBuf;
      }
    }
    if (inp && inp.length) {
      for (let i = 0; i < inp.length; i++) {
        const v = inp[i];
        if (Number.isFinite(v)) {
          const av = v < 0 ? -v : v;
          if (av > this.inPeak) this.inPeak = av;
        }
      }
    }
    if (inp && this.tx) {
      let targetGain = 1;
      const refEnv = this.speaker ? this.rxEnv * 1.35 : this.rxEnv;
      if (refEnv > 0.015 && inp.length) {
        let txEnergy = 0;
        for (let i = 0; i < inp.length; i++) {
          const v = Number.isFinite(inp[i]) ? inp[i] : 0;
          txEnergy += v * v;
        }
        const txRms = Math.sqrt(txEnergy / inp.length);
        // Suppress speakerphone bleed when far-end dominates; open immediately on near-end double-talk
        const dtThresh = Math.max(this.speaker ? 0.055 : 0.045, refEnv * (this.speaker ? 0.65 : 0.55));
        if (txRms < dtThresh) {
          const floor = this.speaker ? 0.10 : 0.15;
          targetGain = floor + (1 - floor) * Math.pow(txRms / dtThresh, 2);
        }
      }
      const gAlpha = targetGain > this.micGain ? 0.25 : 0.08;
      if (this.step <= 1) {
        for (let i = 0; i < inp.length; i++) {
          const raw = Number.isFinite(inp[i]) ? inp[i] : 0;
          this.micGain += (targetGain - this.micGain) * gAlpha;
          this.sum += raw * this.micGain; this.cnt++; this.acc += this.step;
          if (this.acc >= 1) {
            this.acc -= 1;
            this.emitSample(this.sum / this.cnt);
            this.sum = 0; this.cnt = 0;
          }
        }
      } else {
        const inv = 1 / this.step;
        for (let i = 0; i < inp.length; i++) {
          const raw = Number.isFinite(inp[i]) ? inp[i] : 0;
          this.micGain += (targetGain - this.micGain) * gAlpha;
          const s = raw * this.micGain, p = this.prevIn;
          while (this.acc < 1) {
            this.emitSample(p + (s - p) * this.acc);
            this.acc += inv;
          }
          this.acc -= 1;
          this.prevIn = s;
        }
      }
    } else if (inp && inp.length) {
      this.prevIn = 0;
      this.micGain = 1;
      this.acc = 0; this.sum = 0; this.cnt = 0; this.n = 0; this.framePeak = 0;
    }
    this.ticks++;
    if (this.stat && (this.ticks === 1 || (this.ticks & 63) === 0)) {
      const inPeak16 = Math.min(32767, Math.round(this.inPeak * 32767));
      const txPeak16 = this.txPeak;
      this.inPeak = 0;
      this.txPeak = 0;
      this.port.postMessage({
        t: 'stat', ticks: this.ticks, tx: this.txFrames, txNz: this.txNonZero,
        inPeak: inPeak16, txPeak: txPeak16, rx: this.rxFrames, hasIn: !!(inp && inp.length),
      });
    }
    return true;
  }
}
registerProcessor('bridge-io', BridgeIO);
