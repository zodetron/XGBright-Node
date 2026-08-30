/*
 * Browser microphone capture for push-to-talk.
 *
 * Deliberately not MediaRecorder: that produces WebM/Opus, and the server has no
 * codec decoder — it reads WAV through libsndfile and feeds Whisper directly. So this
 * taps raw PCM from the Web Audio graph, downsamples to the 16 kHz mono the pipeline
 * expects, and builds a WAV header in the browser. Nothing has to be transcoded
 * server-side, and the bytes that reach Whisper are the bytes the mic produced.
 */

const TARGET_SAMPLE_RATE = 16000

/** Average-decimate a Float32 buffer down to the target rate. */
function downsample(input: Float32Array, fromRate: number, toRate: number) {
  if (fromRate === toRate) return input
  if (fromRate < toRate) throw new Error('cannot upsample microphone input')

  const ratio = fromRate / toRate
  const output = new Float32Array(Math.floor(input.length / ratio))
  for (let i = 0; i < output.length; i++) {
    // Average across the whole source window rather than picking one sample, so
    // content above the new Nyquist folds down as a dull average instead of sharp
    // aliasing.
    const start = Math.floor(i * ratio)
    const end = Math.min(Math.floor((i + 1) * ratio), input.length)
    let sum = 0
    for (let j = start; j < end; j++) sum += input[j]
    output[i] = end > start ? sum / (end - start) : 0
  }
  return output
}

/** Wrap Float32 samples in a 16-bit mono WAV container. */
function encodeWav(samples: Float32Array, sampleRate: number) {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)

  const writeText = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i))
  }

  writeText(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeText(8, 'WAVE')
  writeText(12, 'fmt ')
  view.setUint32(16, 16, true)          // PCM chunk size
  view.setUint16(20, 1, true)           // format: PCM
  view.setUint16(22, 1, true)           // channels: mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)  // byte rate
  view.setUint16(32, 2, true)           // block align
  view.setUint16(34, 16, true)          // bits per sample
  writeText(36, 'data')
  view.setUint32(40, samples.length * 2, true)

  let offset = 44
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
    offset += 2
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

export type LevelCallback = (level: number) => void

/**
 * Record from the default microphone for `durationMs` and return a 16 kHz mono WAV.
 *
 * `onLevel` is called with a 0-1 RMS level so the UI can show that something is
 * actually being heard — a silent meter during a recording is the fastest way to spot
 * a muted or wrong input device.
 */
export async function recordWav(durationMs: number, onLevel?: LevelCallback) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  })

  const context = new AudioContext()
  const source = context.createMediaStreamSource(stream)
  // ScriptProcessor is deprecated in favour of AudioWorklet, but a worklet needs a
  // separate module file and a message channel for what is, here, a plain buffer
  // copy. For a local dashboard the simpler node is the better trade.
  const processor = context.createScriptProcessor(4096, 1, 1)
  const chunks: Float32Array[] = []

  processor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0)
    chunks.push(new Float32Array(input))

    if (onLevel) {
      let sum = 0
      for (let i = 0; i < input.length; i++) sum += input[i] * input[i]
      onLevel(Math.min(1, Math.sqrt(sum / input.length) * 4))
    }
  }

  source.connect(processor)
  // ScriptProcessor only fires while connected to a destination. Routing it through a
  // muted gain node keeps it running without the mic being echoed out of the speakers.
  const mute = context.createGain()
  mute.gain.value = 0
  processor.connect(mute)
  mute.connect(context.destination)

  await new Promise((resolve) => setTimeout(resolve, durationMs))

  processor.disconnect()
  mute.disconnect()
  source.disconnect()
  stream.getTracks().forEach((track) => track.stop())
  const captureRate = context.sampleRate
  await context.close()
  onLevel?.(0)

  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const merged = new Float32Array(total)
  let offset = 0
  for (const chunk of chunks) {
    merged.set(chunk, offset)
    offset += chunk.length
  }

  return encodeWav(downsample(merged, captureRate, TARGET_SAMPLE_RATE),
                   TARGET_SAMPLE_RATE)
}

/** Play back 16 kHz mono PCM returned by the server as base64. */
export async function playPcmBase64(base64: string, sampleRate = TARGET_SAMPLE_RATE) {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)

  const samples = new Int16Array(bytes.buffer)
  const context = new AudioContext()
  const buffer = context.createBuffer(1, samples.length, sampleRate)
  const channel = buffer.getChannelData(0)
  for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768

  const source = context.createBufferSource()
  source.buffer = buffer
  source.connect(context.destination)
  source.start()

  await new Promise((resolve) => { source.onended = resolve })
  await context.close()
}
