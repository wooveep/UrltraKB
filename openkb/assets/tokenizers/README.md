# Offline text measurement

`cl100k_base.tiktoken` is the fixed rank table referenced by tiktoken 0.13.0.
Source: https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
SHA-256: `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`.

The adjacent `TIKTOKEN-LICENSE.txt` is copied from the installed, pinned tiktoken
distribution. `openkb.text_measurement` loads this bundled file directly, checks
its digest, and uses the 0.13.0 cl100k_base pattern with ordinary-text encoding.
It does not access the network or use a model's tokenizer alias or cache.
Changing the ranks, regex or counting semantics requires a new measurement
fingerprint. This policy classifies frozen text; model request capacity remains
a separate decision.
