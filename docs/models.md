# Model Guide & Hardware Recommendations

Night Ghost is designed to run completely offline using local open-weight language models served through [Ollama](https://ollama.com) or llama.cpp.

---

## 1. Recommended Models

| Model | Recommended Hardware | Context Window | Best For |
|---|---|---|---|
| **`qwen2.5-coder:14b`** *(Recommended)* | 16GB+ VRAM or Unified Memory (Mac M1/M2/M3/M4) | 16K–32K | Best overall performance for Python, TS, edge-case generation, and clean refactoring |
| **`qwen2.5-coder:7b`** | 8GB–12GB VRAM / RAM | 16K | Fast, lightweight runner for laptops with limited memory |
| **`deepseek-coder-v2:16b`** | 16GB+ VRAM / RAM | 16K–32K | Strong reasoning, multi-language test coverage |
| **`gemma4:e4b` / `nemotron-3-nano:4b`** | 4GB–8GB VRAM / RAM | 8K | Minimal resource footprints for low-power continuous background tasks |

---

## 2. Setting Up Ollama

1. **Install Ollama**:
   Follow instructions at [ollama.com](https://ollama.com).

2. **Pull the Recommended Model**:
   ```bash
   ollama pull qwen2.5-coder:14b
   ```

3. **Verify the Model**:
   ```bash
   ollama run qwen2.5-coder:14b "hello"
   ```

4. **Configure `nightghost.yaml`**:
   ```yaml
   model:
     provider: ollama
     name: qwen2.5-coder:14b
     temperature: 0.2
     context_window: 16384
     api_base: http://localhost:11434
   ```

---

## 3. Hardware Requirements

- **Processor**: Apple Silicon (M-series) or modern x86_64 CPU (AVX2 supported).
- **GPU Acceleration**:
  - NVIDIA RTX 3060/4060 or higher (8GB–16GB VRAM) for 7B/14B models.
  - Apple Silicon unified memory (16GB+ RAM recommended for 14B models).
- **Disk Space**: ~10GB per model weights file.
