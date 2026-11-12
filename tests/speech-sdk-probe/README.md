# Pleos speech SDK integration probe

This isolated instrumentation APK uses the public SpeechToText 3.3.1 SDK in
`Mode.ON_DEVICE`. It sends the existing Microsoft Heami synthetic Korean fixture
as 16 kHz signed 16-bit mono PCM. It declares no microphone permission, never
calls `request()`, and does not read or change the conversation app's data.

Build from the project root with `tests/speech-sdk-probe/build.ps1`, install the
generated `build/speech-sdk-probe.apk` on the emulator user, and run:

```text
adb shell am instrument --user 10 -w local.pleos.cabin.speechprobe/local.pleos.cabin.speechprobe.SpeechSdkProbe
```

The fixture contains multiple sentences. This probe stops streaming when the
service ends its first utterance, then waits three seconds for late final text.
It checks SDK initialization and one real recognition result, not continuous
microphone capture, seat identification, or four-person recognition accuracy.

Verified on 2026-11-12 against emulator CAAS 4.2.10-coral-aosp:

- Initialization succeeded without project registration or cloud credentials.
- `onEndedRecognition` arrived at 2807 ms.
- `onUpdated(text, true)` arrived at 2908 ms, **101 ms after end**.
- Exactly one final result: `저는 커피를 마시고 싶어요`.
- No SDK error; instrumentation result code `-1` (success).

This ordering is why `NativeSpeech` retains the current listener for a bounded
final-result grace period after the native end event. A missing final is never
replaced with an interim hypothesis.

Official references:

- [SpeechToText introduction](https://document.pleos.ai/en/docs/pleos-only/connect-sdk-pleos/SpeechToText/intro)
- [Audio buffer input](https://document.pleos.ai/en/docs/pleos-only/connect-sdk-pleos/SpeechToText/sending-audio-data)
- [Result listeners](https://document.pleos.ai/en/docs/pleos-only/connect-sdk-pleos/SpeechToText/result-listeners)

The generated APK and assets remain under this directory's ignored `build/`.
