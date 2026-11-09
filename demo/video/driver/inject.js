// Injected into the Mock Board page by the demo driver only (never part of the product).
//
// 1. A stand-in microphone: getUserMedia returns a WebAudio stream, and window.__demo.say(url)
//    plays a candidate clip into it. The page and the server treat it as a microphone.
// 2. An event log: every data-channel message from the server, timestamped (epoch ms).
// 3. One audio recording of what a viewer hears: the board's voice (WebRTC), the candidate
//    clips, and any other page audio (the spoken second-attempt question), mixed in sync.
(() => {
  const ctx = new AudioContext({ sampleRate: 48000 });
  // Clips go to one bus; each getUserMedia call gets its own stream from it, because the page
  // stops the tracks it was given (the mic check does, before the sitting asks again).
  const micBus = ctx.createGain();
  const recDest = ctx.createMediaStreamDestination();
  const log = [];
  const note = (kind, data) => log.push({ t: Date.now(), kind, ...data });
  const seenStreams = new WeakSet();
  const seenElements = new WeakSet();

  navigator.mediaDevices.getUserMedia = async () => {
    await ctx.resume();
    const dest = ctx.createMediaStreamDestination();
    micBus.connect(dest);
    note("gum", {});
    return dest.stream;
  };

  // Board audio and any page audio element go to the recording as well as the speakers.
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function (...args) {
    try {
      if (this.srcObject instanceof MediaStream && !seenStreams.has(this.srcObject)) {
        seenStreams.add(this.srcObject);
        ctx.createMediaStreamSource(this.srcObject).connect(recDest);
        note("board_audio_attached", {});
      } else if (!this.srcObject && this.src && !seenElements.has(this)) {
        seenElements.add(this);
        const src = ctx.createMediaElementSource(this);
        src.connect(ctx.destination);
        src.connect(recDest);
        note("page_audio", { src: String(this.src).slice(0, 40) });
      }
    } catch (err) {
      note("audio_hook_error", { message: String(err) });
    }
    return origPlay.apply(this, args);
  };

  // Server events arrive on the data channel the page opens.
  const origCreate = RTCPeerConnection.prototype.createDataChannel;
  RTCPeerConnection.prototype.createDataChannel = function (...args) {
    const channel = origCreate.apply(this, args);
    channel.addEventListener("message", (e) => {
      try {
        const m = JSON.parse(e.data);
        if (m && m.type === "mockboard") note("event", { event: m.event, data: m });
      } catch { /* pings and non-JSON */ }
    });
    return channel;
  };
  // The bot's stream is set on an <audio> element, which autoplays without play(): catch it.
  const origSrcObject = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "srcObject");
  Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
    get() { return origSrcObject.get.call(this); },
    set(stream) {
      origSrcObject.set.call(this, stream);
      if (stream instanceof MediaStream && !seenStreams.has(stream)) {
        seenStreams.add(stream);
        try {
          ctx.createMediaStreamSource(stream).connect(recDest);
          note("board_audio_attached", {});
        } catch (err) { note("audio_hook_error", { message: String(err) }); }
      }
    },
  });

  let recorder = null;
  window.__demo = {
    log,
    note,
    async say(url, label) {
      await ctx.resume();
      const buf = await ctx.decodeAudioData(await (await fetch(url)).arrayBuffer());
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(micBus);
      src.connect(recDest);
      const done = new Promise((resolve) => { src.onended = resolve; });
      note("say_start", { label, seconds: buf.duration });
      src.start();
      await done;
      note("say_end", { label });
      return buf.duration;
    },
    async startRecording() {
      await ctx.resume(); // or the recording waits for the first clip to start the audio clock
      // A silent source keeps audio flowing into the recorder from the first moment.
      const hush = ctx.createConstantSource();
      hush.offset.value = 0;
      hush.connect(recDest);
      hush.start();
      note("ctx_state", { state: ctx.state });
      recorder = new MediaRecorder(recDest.stream, { mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: 160000 });
      recorder.ondataavailable = async (e) => {
        if (!e.data.size) return;
        const b64 = btoa(String.fromCharCode(...new Uint8Array(await e.data.arrayBuffer())));
        window.__demoSaveChunk(b64);
      };
      recorder.onstart = () => note("rec_start", {});
      recorder.start(1000);
    },
    stopRecording() {
      return new Promise((resolve) => {
        recorder.onstop = () => { note("rec_stop", {}); setTimeout(resolve, 300); };
        recorder.stop();
      });
    },
  };
})();
