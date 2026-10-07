// Runs on the audio thread. Collects 128-frame blocks from the mic into
// ~CHUNK_MS chunks of 16-bit PCM and posts each one to the main thread.

class PCMSender extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const chunkMs = (options.processorOptions && options.processorOptions.chunkMs) || 20;
    this.chunkSamples = Math.max(128, Math.round(sampleRate * chunkMs / 1000));
    this.buf = new Int16Array(this.chunkSamples);
    this.pos = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0]; // mono: first channel only

    for (let i = 0; i < ch.length; i++) {
      let s = ch[i];
      if (s > 1) s = 1; else if (s < -1) s = -1;
      this.buf[this.pos++] = s < 0 ? s * 0x8000 : s * 0x7fff;

      if (this.pos === this.chunkSamples) {
        this.port.postMessage(this.buf.buffer, [this.buf.buffer]); // transfer, no copy
        this.buf = new Int16Array(this.chunkSamples);
        this.pos = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-sender", PCMSender);
