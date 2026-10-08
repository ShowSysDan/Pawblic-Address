// Runs on the audio thread. Turns the mic into 48 kHz 16-bit mono PCM and posts it to the
// main thread in chunks of CHUNK samples, but only while sending is on (live). Muted, it
// produces no audio at all. Live or muted, it posts the mic's peak level ({peak}) about
// 20 times a second for the meter.

const OUT_RATE = 48000; // what the server takes
const METER_EVERY_S = 0.05;

class PCMSender extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    this.chunk = opts.chunkSamples || 512;
    this.buf = new Int16Array(this.chunk);
    this.pos = 0;
    this.sending = false;
    // Browsers nearly always honour the 48 kHz we ask for. If one doesn't, resample here
    // (linear interpolation; fine for speech) so the server's format never changes.
    this.step = sampleRate / OUT_RATE; // input samples per output sample
    this.t = 0;                        // next output position, in input samples, from prev
    this.prev = 0;                     // last input sample of the previous block
    this.peak = 0;
    this.meterFrames = 0;
    this.port.onmessage = (e) => {
      this.sending = !!e.data.sending;
      this.pos = 0; // a new talk starts with a fresh chunk
    };
  }

  emit(s) {
    if (s > 1) s = 1; else if (s < -1) s = -1;
    this.buf[this.pos++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    if (this.pos === this.chunk) {
      this.port.postMessage(this.buf.buffer, [this.buf.buffer]); // transfer, no copy
      this.buf = new Int16Array(this.chunk);
      this.pos = 0;
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const x = input[0]; // mono: first channel only
    const n = x.length;

    for (let i = 0; i < n; i++) { const a = Math.abs(x[i]); if (a > this.peak) this.peak = a; }
    this.meterFrames += n;
    if (this.meterFrames >= sampleRate * METER_EVERY_S) {
      this.port.postMessage({ peak: this.peak });
      this.peak = 0;
      this.meterFrames = 0;
    }

    if (this.step === 1) {
      if (this.sending) for (let i = 0; i < n; i++) this.emit(x[i]);
      return true;
    }
    // y = [prev, x[0], ..., x[n-1]]; output every `step` input samples.
    while (this.t < n) {
      const i = Math.floor(this.t);
      const f = this.t - i;
      const a = i === 0 ? this.prev : x[i - 1];
      if (this.sending) this.emit(a + (x[i] - a) * f);
      this.t += this.step;
    }
    this.t -= n;
    this.prev = x[n - 1];
    return true;
  }
}

registerProcessor("pcm-sender", PCMSender);
