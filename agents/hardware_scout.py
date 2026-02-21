#!/usr/bin/env python3
"""
hardware_scout.py — Howell Forge Hardware Inventory & Strategy Advisor

Detected hardware (Feb 21, 2026):
  GPU  : NVIDIA T1200 Laptop GPU — 4 GB VRAM
  RAM  : 32 GB system RAM
  CPU  : Intel i7-11850H @ 2.50 GHz — 16 threads (8 cores)
  OS   : Linux (Debian 12 bookworm)

Strategy: HYBRID
  - 4 GB VRAM → too tight for GPU-offloaded 7B models
  - 32 GB RAM  → Ollama can run Q4-quantized 3B models CPU-only (logistics agents)
  - Groq API   → free, fast inference for heavy CAD reasoning (70B class)

Run this any time to re-check hardware and get an updated recommendation.
"""

import subprocess
import json
import shutil


def check_gpu():
    try:
        out = subprocess.check_output(
            ['nvidia-smi',
             '--query-gpu=gpu_name,memory.total,driver_version',
             '--format=csv,nounits,noheader'],
            encoding='utf-8'
        ).strip()
        name, vram_mb, driver = [x.strip() for x in out.split(',')]
        vram_gb = int(vram_mb) / 1024
        return {"detected": True, "name": name, "vram_gb": round(vram_gb, 2),
                "driver": driver}
    except Exception:
        return {"detected": False, "name": None, "vram_gb": 0, "driver": None}


def check_ram():
    try:
        out = subprocess.check_output(['free', '-m'], encoding='utf-8')
        line = [l for l in out.splitlines() if l.startswith('Mem:')][0]
        total_mb = int(line.split()[1])
        return round(total_mb / 1024, 1)
    except Exception:
        return 0


def check_cpu():
    try:
        cores = int(subprocess.check_output(['nproc'], encoding='utf-8').strip())
        with open('/proc/cpuinfo') as f:
            for line in f:
                if 'model name' in line:
                    return {"cores": cores, "model": line.split(':')[1].strip()}
    except Exception:
        pass
    return {"cores": 0, "model": "Unknown"}


def check_ollama():
    return shutil.which('ollama') is not None


def suggest_strategy(gpu, ram_gb):
    vram = gpu["vram_gb"]

    if vram >= 12:
        tier = "FORTRESS GRADE"
        icon = "🚀"
        local_model   = "ollama/llama3.1:8b"
        cloud_model   = "groq/llama-3.3-70b-versatile"
        note = "Full local inference available. Use cloud only for 70B-class reasoning."
    elif vram >= 6:
        tier = "HYBRID — GPU ASSIST"
        icon = "⚖️"
        local_model   = "ollama/phi3:mini"
        cloud_model   = "groq/llama-3.3-70b-versatile"
        note = "GPU-assisted local models for logistics. Cloud for CAD reasoning."
    elif ram_gb >= 16:
        tier = "HYBRID — CPU LOCAL"
        icon = "⚖️"
        local_model   = "ollama/phi3:mini"        # ~2.3 GB, runs fine on 32 GB RAM
        cloud_model   = "groq/llama-3.3-70b-versatile"
        note = f"4 GB VRAM too tight for GPU offload, but {ram_gb} GB RAM handles CPU inference. Use local for lightweight agents (Quartermaster, logistics), Groq for CAD."
    else:
        tier = "CLOUD-DEPENDENT"
        icon = "☁️"
        local_model   = None
        cloud_model   = "groq/llama-3.3-70b-versatile"
        note = "Insufficient RAM/VRAM for local inference. Use Groq API."

    return {
        "tier": tier,
        "icon": icon,
        "local_model":  local_model,
        "cloud_model":  cloud_model,
        "note": note,
    }


def main():
    print("\n--- 🛡️  FORGE HARDWARE SCOUT ---\n")

    gpu = check_gpu()
    ram = check_ram()
    cpu = check_cpu()
    has_ollama = check_ollama()

    if gpu["detected"]:
        print(f"  ✅ GPU   : {gpu['name']}")
        print(f"  📊 VRAM  : {gpu['vram_gb']} GB  (driver {gpu['driver']})")
    else:
        print("  ❌ GPU   : No NVIDIA GPU detected")

    print(f"  🧠 RAM   : {ram} GB")
    print(f"  ⚙️  CPU   : {cpu['model']} ({cpu['cores']} threads)")
    print(f"  🦙 Ollama: {'installed' if has_ollama else 'not installed'}")

    strategy = suggest_strategy(gpu, ram)
    print(f"\n--- 🧠 ARIA'S STRATEGY: {strategy['icon']} {strategy['tier']} ---\n")
    print(f"  Local model  : {strategy['local_model'] or 'N/A'}")
    print(f"  Cloud model  : {strategy['cloud_model']}")
    print(f"  Note         : {strategy['note']}")

    result = {
        "hardware": {"gpu": gpu, "ram_gb": ram, "cpu": cpu, "ollama": has_ollama},
        "strategy": strategy,
    }

    out_path = __file__.replace("hardware_scout.py", "hardware_profile.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Profile saved → {out_path}\n")
    return result


if __name__ == "__main__":
    main()
