from fastapi import FastAPI, WebSocket, Query
import asyncio
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import docker
import os
import time
import json
from typing import Dict, Any, List

app = FastAPI(title="Server Monitor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = docker.from_env()

_prev_cpu = None


def _read_file(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return ""


def host_memory() -> Dict[str, Any]:
    meminfo = _read_file("/host/proc/meminfo")
    vals = {}
    for line in meminfo.splitlines():
        parts = line.split(":")
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        val = parts[1].strip().split()[0]
        try:
            vals[key] = int(val) * 1024
        except Exception:
            pass
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", 0)
    used = max(total - avail, 0)
    return {"total": total, "used": used, "free": avail, "used_pct": round((used / total * 100), 2) if total else 0}


def host_cpu_percent() -> float:
    global _prev_cpu
    stat = _read_file("/host/proc/stat").splitlines()
    if not stat:
        return 0.0
    parts = stat[0].split()[1:]
    nums = list(map(int, parts))
    idle = nums[3] + nums[4]
    total = sum(nums)
    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        return 0.0
    prev_idle, prev_total = _prev_cpu
    idle_delta = idle - prev_idle
    total_delta = total - prev_total
    _prev_cpu = (idle, total)
    if total_delta <= 0:
        return 0.0
    return round((1 - idle_delta / total_delta) * 100, 2)


def host_uptime() -> float:
    up = _read_file("/host/proc/uptime").split()
    try:
        return float(up[0])
    except Exception:
        return 0.0


def host_disk() -> Dict[str, Any]:
    st = os.statvfs("/hostfs")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    return {"total": total, "used": used, "free": free, "used_pct": round((used / total * 100), 2) if total else 0}


def list_containers() -> List[Dict[str, Any]]:
    out = []
    for c in client.containers.list(all=True):
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
        out.append({
            "id": c.short_id,
            "name": c.name,
            "image": c.image.tags[0] if c.image.tags else c.image.short_id,
            "status": c.status,
            "state": c.attrs.get("State", {}).get("Status"),
            "health": (c.attrs.get("State", {}).get("Health") or {}).get("Status"),
            "created": c.attrs.get("Created"),
            "ports": ports,
        })
    return out


def list_images() -> List[Dict[str, Any]]:
    imgs = []
    for i in client.images.list():
        imgs.append({
            "id": i.short_id,
            "tags": i.tags,
            "size": i.attrs.get("Size", 0),
        })
    return imgs


@app.get("/")
def index():
    return FileResponse("/app/static/index.html")


@app.get("/api/summary")
def summary():
    containers = list_containers()
    images = list_images()
    running = len([c for c in containers if c["status"] == "running"])
    unhealthy = len([c for c in containers if c.get("health") == "unhealthy"])
    return {
        "server": {
            "cpu_pct": host_cpu_percent(),
            "memory": host_memory(),
            "disk": host_disk(),
            "uptime_sec": host_uptime(),
        },
        "docker": {
            "containers_total": len(containers),
            "containers_running": running,
            "images_total": len(images),
            "unhealthy": unhealthy,
        },
    }


@app.get("/api/containers")
def containers():
    return list_containers()


@app.get("/api/images")
def images():
    return list_images()


@app.get("/api/logs/{container_name}")
def logs(container_name: str, tail: int = Query(200, ge=10, le=2000)):
    try:
        c = client.containers.get(container_name)
        data = c.logs(tail=tail).decode("utf-8", errors="ignore")
        return JSONResponse({"container": container_name, "logs": data})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/api/container/{container_name}/stats")
def container_stats(container_name: str):
    try:
        c = client.containers.get(container_name)
        s = c.stats(stream=False)
        cpu = 0.0
        try:
            cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
            sys_delta = s["cpu_stats"]["system_cpu_usage"] - s["precpu_stats"]["system_cpu_usage"]
            cpus = len(s["cpu_stats"].get("cpu_usage", {}).get("percpu_usage", []) or [1])
            if sys_delta > 0:
                cpu = (cpu_delta / sys_delta) * cpus * 100
        except Exception:
            pass
        mem_used = s.get("memory_stats", {}).get("usage", 0)
        mem_limit = s.get("memory_stats", {}).get("limit", 1)
        return {
            "cpu_pct": round(cpu, 2),
            "mem_used": mem_used,
            "mem_limit": mem_limit,
            "mem_pct": round((mem_used / mem_limit) * 100, 2) if mem_limit else 0,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            payload = {
                "summary": summary(),
                "containers": list_containers(),
                "ts": int(time.time()),
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(2)
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
