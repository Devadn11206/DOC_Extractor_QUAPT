# Testing vLLM on Kaggle

**Not yet run on Kaggle.** The engine code is tested against a stand-in server. This first run is the real test.

Everything runs inside the notebook, over `127.0.0.1`: no public address and no API key are needed. Use synthetic documents.

## 1. Session settings

**Internet: ON**, **Accelerator: GPU T4 x2**. A single T4 is likely too tight for three servers, and vLLM is unlikely to run on a P100.

## 2. Get the code

The notebook `src/doc_extractor/notebookbdb318963a.ipynb` does steps 2 to 7 in one cell. To do them by hand:

```python
!git clone -b fix/qwen3-only-and-validation-bugs https://github.com/Devadn11206/DOC_Extractor_QUAPT.git /kaggle/working/DOC_Extractor_QUAPT
%cd /kaggle/working/DOC_Extractor_QUAPT
```

## 3. Install the engine

```python
!pip install -q uv
!uv pip install --system -e ".[ui]"
```

## 4. Install vLLM in its own environment

vLLM pins its own PyTorch, which can break the notebook's packages, so keep it separate.

```python
!uv venv --python 3.12 /kaggle/tmp/vllm-env
!uv pip install -p /kaggle/tmp/vllm-env/bin/python --torch-backend=auto vllm bitsandbytes
```

## 5. Start the servers

```python
!python scripts/serve_vllm.py --detach --vllm-bin /kaggle/tmp/vllm-env/bin/vllm
```

The first start downloads about 20 GB. On two GPUs the vision model gets GPU 0; the text and OCR models share GPU 1.

## 6. Check them

```python
import os
os.environ["VLLM_API_BASE"] = "http://127.0.0.1:8000/v1"
os.environ["VLLM_TEXT_API_BASE"] = "http://127.0.0.1:8001/v1"
os.environ["VLLM_OCR_API_BASE"] = "http://127.0.0.1:8002/v1"
!python scripts/check_vllm.py
```

Expected last line: `4/4 checks passed`.

## 7. Run the app

```python
!cd src && python -m doc_extractor.app
```

Stop the servers with `!python scripts/serve_vllm.py --stop`.

The app prints a **public** `gradio.live` link. Set `os.environ["GRADIO_SHARE"] = "False"` to turn it off, but you then cannot open the app from outside the notebook.

## If a server does not start

The launcher prints the end of its log (full logs are in `vllm-logs/`).

| Log shows | Try |
| --- | --- |
| Out of memory, or no memory left for the KV cache | `--max-model-len 4096`, or raise that server's share, for example `--text-memory 0.65 --ocr-memory 0.28` |
| An error about the custom all-reduce or compilation on T4 | `--extra "--enforce-eager"` |
| `bitsandbytes` cannot load a model | `--no-quantize` (the text model may then not fit beside the OCR model) |
| GLM-OCR architecture not supported, or a Transformers version error | Upgrade in the vLLM environment: `uv pip install -p /kaggle/tmp/vllm-env/bin/python -U vllm transformers` |
| `bfloat16` not supported | `--dtype float16` |

If the OCR server cannot start at all, the vision and text servers can still be tested: leave `VLLM_OCR_API_BASE` unset.
