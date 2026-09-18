/* Playback ring: queue Float32 chunks (already at context rate), flush on barge-in, report played samples. */
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.off = 0;
    this.played = 0;
    this.tick = 0;
    this.wasPlaying = false;
    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'push') this.queue.push(m.data);
      else if (m.type === 'flush') { this.queue = []; this.off = 0; }
    };
  }
  process(_, outputs) {
    const out = outputs[0][0];
    let i = 0;
    while (i < out.length && this.queue.length) {
      const cur = this.queue[0];
      const n = Math.min(out.length - i, cur.length - this.off);
      out.set(cur.subarray(this.off, this.off + n), i);
      i += n; this.off += n; this.played += n;
      if (this.off >= cur.length) { this.queue.shift(); this.off = 0; }
    }
    for (; i < out.length; i++) out[i] = 0;
    const playing = this.queue.length > 0;
    if (++this.tick % 8 === 0 || playing !== this.wasPlaying) {
      this.port.postMessage({ type: 'state', played: this.played, playing });
    }
    this.wasPlaying = playing;
    return true;
  }
}
registerProcessor('playback-processor', PlaybackProcessor);
