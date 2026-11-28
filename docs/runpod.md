# Hosting the models on RunPod with vLLM

**Not yet run on real hardware.** The engine side is tested against a stand-in server; the `vllm serve` commands below need a first run on a pod.

**Data protection.** With these settings, page images and extracted text leave the machine that runs the engine. The PRD requires processing inside the deployment boundary, with external providers only where policy allows (ARC-001, SEC-011, GR-025). Use synthetic documents until RunPod is approved for real customer or health data.

## 1. Pod

- A vLLM server serves one model, so run three processes on separate ports.
- Start with a 24 GB GPU and 4-bit loading for the two Qwen models, as the in-process engine does. This sizing is unmeasured. Without quantisation the three sets of weights alone need roughly 19 GB.
- On a card without native bfloat16 (for example a T4), add `--dtype float16` to each command.
- Expose HTTP ports `8000`, `8001` and `8002`.
- Use a recent vLLM release. GLM-OCR support is newer than Qwen3 support; check its [vLLM recipe](https://docs.vllm.ai/projects/recipes/en/latest/GLM/GLM-OCR.html) for the required vLLM and Transformers versions.

## 2. Servers

`python scripts/serve_vllm.py --host 0.0.0.0` (with `VLLM_API_KEY` set) starts all three and waits until each is ready; `python scripts/check_vllm.py` then verifies them. For Kaggle see [kaggle/vllm.md](kaggle/vllm.md). The equivalent commands:

```bash
export VLLM_API_KEY="$(openssl rand -hex 32)"   # keep it; the engine needs the same value

vllm serve Qwen/Qwen3-VL-4B-Instruct --port 8000 --api-key "$VLLM_API_KEY" \
  --quantization bitsandbytes --max-model-len 16384 --gpu-memory-utilization 0.40 &

vllm serve Qwen/Qwen3-4B-Instruct-2507 --port 8001 --api-key "$VLLM_API_KEY" \
  --quantization bitsandbytes --max-model-len 16384 --gpu-memory-utilization 0.35 &

vllm serve zai-org/GLM-OCR --port 8002 --api-key "$VLLM_API_KEY" \
  --max-model-len 8192 --gpu-memory-utilization 0.20 &
```

Start them one at a time, waiting for each to report ready, so that their memory reservations do not collide.

## 3. Engine

```bash
export VLLM_API_BASE=https://<pod-id>-8000.proxy.runpod.net/v1
export VLLM_TEXT_API_BASE=https://<pod-id>-8001.proxy.runpod.net/v1
export VLLM_OCR_API_BASE=https://<pod-id>-8002.proxy.runpod.net/v1
export VLLM_API_KEY=<the same key>
python -m doc_extractor.app
```

- The model names the engine requests must match the served names. If you set `DOC_DEFAULT_MODEL`, serve that model on port 8000. A separate `DOC_ROUTER_MODEL` is not supported with one vision server.
- A URL left unset runs that model in-process, which needs a local GPU.
- A public host must use `https` and an API key; redirects are refused.
- The RunPod HTTP proxy closes requests after about 100 seconds. A page that takes longer fails.

## 4. Check

```bash
curl -H "Authorization: Bearer $VLLM_API_KEY" https://<pod-id>-8000.proxy.runpod.net/v1/models
```

## 5. Load test

`vllm bench serve` measures one server. Run it from a machine with vLLM installed, starting low and raising `--max-concurrency` (1, 5, 10):

```bash
vllm bench serve --backend openai-chat --base-url http://127.0.0.1:8001 \
  --endpoint /v1/chat/completions --model Qwen/Qwen3-4B-Instruct-2507 \
  --dataset-name random --random-input-len 2000 --random-output-len 1000 \
  --num-prompts 50 --max-concurrency 5
```

For a remote server add `--header "Authorization=Bearer $VLLM_API_KEY"`. This covers the text model only: it does not measure time per page through the whole engine.
