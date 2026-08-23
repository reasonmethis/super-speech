# Third-party notices

Super Speech's Electron application and original source code are MIT licensed.
The installer is an aggregate that also contains a separate frozen speech
engine and model assets under their respective licenses.

## Speech engine

- `kokoro-onnx` is MIT licensed by thewh1teagle
- Kokoro-82M model weights and voice data are Apache-2.0 licensed by hexgrad
- `phonemizer-fork` is GPL-3.0 licensed by the Phonemizer contributors
- eSpeak NG code and data are GPL-3.0-or-later licensed by the eSpeak NG contributors
- ONNX Runtime is MIT licensed by Microsoft
- NumPy is BSD-3-Clause licensed with separately licensed bundled components
- python-sounddevice is MIT licensed by Matthias Geier

The frozen engine combines with the GPL phonemization components and is
distributed under GPLv3-compatible terms. The Electron application remains a
separate process and communicates with the engine through files in the user's
Super Speech runtime directory.

Full dependency license files are included inside the frozen engine directory.
Corresponding Super Speech source and build instructions are available at
https://github.com/reasonmethis/super-speech. Upstream sources are available at:

- https://github.com/thewh1teagle/kokoro-onnx
- https://huggingface.co/hexgrad/Kokoro-82M
- https://github.com/bootphon/phonemizer
- https://github.com/espeak-ng/espeak-ng
- https://github.com/microsoft/onnxruntime
- https://github.com/numpy/numpy
- https://github.com/spatialaudio/python-sounddevice
