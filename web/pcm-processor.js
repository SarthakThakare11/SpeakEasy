class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 24000;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;

    const samples = input[0];

    // Browser AudioContext is commonly 48 kHz. Downsample to 24 kHz.
    const ratio = sampleRate / this.targetRate;
    const newLength = Math.floor(samples.length / ratio);
    const pcm = new Int16Array(newLength);

    for (let i = 0; i < newLength; i++) {
      const position = i * ratio;
      const left = Math.floor(position);
      const right = Math.min(left + 1, samples.length - 1);
      const fraction = position - left;
      const value = samples[left] * (1 - fraction) + samples[right] * fraction;
      const clipped = Math.max(-1, Math.min(1, value));
      pcm[i] = clipped < 0 ? clipped * 32768 : clipped * 32767;
    }

    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}

registerProcessor("pcm-processor", PCMProcessor);
