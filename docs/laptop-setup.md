# Running the app on a laptop (Windows, with uv)

Two ways to run it. Pick one.

| Option | Needs | Models run on |
|---|---|---|
| **A. Laptop + remote GPU** (recommended) | Any laptop with internet | RunPod / AWS / Kaggle server (set in step 4) |
| **B. Laptop with an NVIDIA GPU** | NVIDIA card with 16 GB+ VRAM | The laptop itself (no vLLM) |

A laptop without an NVIDIA GPU cannot run the models at a usable speed, so use option A.

**Data:** with option A, pages are sent to the GPU server. Use made-up documents unless the data policy approves the server.

## 1. Install uv (once)

Open **PowerShell** and run:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen PowerShell, then check: `uv --version`

## 2. Get the code (once)

```powershell
git clone -b fix/qwen3-only-and-validation-bugs https://github.com/Devadn11206/DOC_Extractor_QUAPT.git
cd DOC_Extractor_QUAPT
```

Check the commit with `git log -1 --oneline`. It should match the latest on GitHub.

## 3. Install the packages (once)

```powershell
uv venv --python 3.12 .venv
.venv\Scripts\activate
uv pip install -e ".[ui]"
```

`uv` downloads Python 3.12 if needed. The first install takes several minutes.

If `bitsandbytes` fails to install on Windows, option A still works. Ask for help with that error and skip it for now.

## 4. Point the app at the GPU server (every time)

Replace the addresses and key with the ones from the server owner. Do this in the same PowerShell window:

```powershell
$env:VLLM_API_BASE = "https://<pod-id>-8000.proxy.runpod.net/v1"
$env:VLLM_TEXT_API_BASE = "https://<pod-id>-8001.proxy.runpod.net/v1"
$env:VLLM_OCR_API_BASE = "https://<pod-id>-8002.proxy.runpod.net/v1"
$env:VLLM_API_KEY = "<the key>"
$env:VLLM_TIMEOUT_SECONDS = "300"
$env:GRADIO_SHARE = "false"
```

For option B, skip this step and run the app without the `VLLM_*` variables. The models then load on the laptop's GPU.

## 5. Start the app

```powershell
.venv\Scripts\activate
cd src
python -m doc_extractor.app
```

Open **http://127.0.0.1:7860** in your browser.

Only this laptop can open that address. Stop the app with `Ctrl+C`.

## Every time you come back

1. `cd DOC_Extractor_QUAPT` and `.venv\Scripts\activate`
2. Set the variables in step 4 again (they are cleared when PowerShell closes)
3. `cd src` and `python -m doc_extractor.app`

## Update to the latest code

```powershell
git pull
uv pip install -e ".[ui]"
```

## Check the GPU server

```powershell
python scripts\check_vllm.py
```

Expected: `4/4 checks passed`.

## Problems

| Message | Fix |
|---|---|
| `uv` not recognised | Close and reopen PowerShell |
| `VLLM_API_KEY` or 401 error | Check the key matches the server's |
| `could not be reached` | The GPU server is off or the address is wrong. Ask the server owner |
| `public host ... use https` | Use `https://` for RunPod and AWS addresses |
| Page times out | Raise `VLLM_TIMEOUT_SECONDS` to `600` |
| Browser shows the page but uploads fail | Check the PowerShell window for the error text |
