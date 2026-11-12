"""Build assets and package public Java runtime resources for the isolated probe."""
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(__file__).parent / "build"

if sys.argv[1] == "asset":
    import numpy as np
    from faster_whisper.audio import decode_audio
    audio = decode_audio(str(ROOT / "tests/fixtures/synthetic-ko-utterance.wav"), sampling_rate=16000)
    assert audio.ndim == 1
    (BUILD / "assets").mkdir(parents=True, exist_ok=True)
    (BUILD / "assets/test.pcm").write_bytes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
elif sys.argv[1] == "package":
    resources = {}
    for jar in sorted((ROOT / "android/deps").glob("*.jar")):
        with zipfile.ZipFile(jar) as archive:
            for name in archive.namelist():
                if name.endswith(("/", ".class")) or name.upper() == "META-INF/MANIFEST.MF":
                    continue
                if name.startswith(("META-INF/proguard/", "META-INF/com.android.tools/")):
                    continue
                assert name not in ("AndroidManifest.xml", "resources.arsc", "classes.dex")
                content = archive.read(name)
                if name in resources and resources[name] != content:
                    assert name.startswith("META-INF/services/"), name
                    content = resources[name].rstrip() + b"\n" + content
                resources[name] = content
    with zipfile.ZipFile(BUILD / "unsigned.apk", "a", zipfile.ZIP_DEFLATED) as archive:
        for dex in (BUILD / "dex").glob("*.dex"):
            archive.write(dex, dex.name)
        for name, content in resources.items():
            archive.writestr(name, content)
