# Pollinations for Krita

Generate an image onto a new layer, or edit the active layer, using your
Pollinations account. No GPU, no local model download: you pay per image with
your own Pollen.

This is the difference from [krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion),
the popular local-model plug-in: that one needs ComfyUI and a GPU behind it. This
one needs only a Pollinations account, so it works on a laptop with no discrete
GPU, and the model list follows the service rather than a local checkpoint.

## Install

1. In Krita, **Settings → Manage Resources → Open Resource Folder**.
2. Copy both of these into the `pykrita` folder inside it:
   - `pollinations_krita.desktop`
   - the `pollinations_krita/` directory
3. Restart Krita, then enable **Pollinations** under **Settings → Configure Krita → Python Plugin Manager**.

## Use

**Tools → Scripts → Pollinations…** generates an image onto a new layer.
**Tools → Scripts → Pollinations: edit layer…** sends the active layer to an
image-editing model and puts the result on a new layer, leaving the source
untouched.

Connect once with **Connect account**: the dialog shows a short code, opens the
approval page, and waits. Nothing is pasted into Krita. The model dropdown is
filled from your account's live `/image/models` catalogue, including community
models. Generating and polling both run on worker threads, so Krita does not
freeze while an image is on its way.

The authorization is stored owner-only on POSIX; on Windows it is DPAPI-encrypted
and bound to the Windows user, so a copied configuration blob is useless
elsewhere. It is only ever sent as a bearer header to Pollinations, and response
bodies are never displayed because an error body can echo the credential.

## Verification

```sh
python -m unittest -v test_api
```

The suite runs the transport against a real localhost HTTP server: requests
genuinely go over the wire and responses are genuinely parsed, with no external
credentials and no Pollen spent. It covers the device flow (pending, `slow_down`,
cancelled, declined), error-to-sentence mapping, the guarantee that a response
body never reaches a message, catalogue filtering, base64 decoding, and the edit
route.

The module is loaded by path rather than by package import, because
`pollinations_krita/__init__.py` imports Krita and Qt, which exist only inside
Krita.

## Honest gaps

- The Qt dialog and the layer logic in `__init__.py` are **not covered by the
  test suite** — they need a running Krita, which was not available here. The
  transport, which is where the API contract lives, is covered.
- Live billed generation and a real device-flow approval were not run
  automatically; those need a signed-in account and spend Pollen.
- Tested on Windows. Linux and macOS paths are written but not exercised.

## Contracts

- [BYOP device flow](https://github.com/pollinations/pollinations/blob/main/BRING_YOUR_OWN_POLLEN.md)
- [API docs](https://gen.pollinations.ai/docs), image model list
- [Krita Python plug-in manual](https://docs.krita.org/en/user_manual/python_scripting.html)
