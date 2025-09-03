from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os, hashlib
import aiofiles

UPLOAD_DIR = "uploads"
CHUNK_SIZE = 1 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024 * 1024

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="FileBeam Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost",
        "http://127.0.0.1",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def safe_unique_path(filename: str) -> str:
    base = os.path.basename(filename) or "archivo"
    name, ext = os.path.splitext(base)
    dest = os.path.join(UPLOAD_DIR, base)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(UPLOAD_DIR, f"{name} ({i}){ext}")
        i += 1
    return dest

async def sha256sum(path: str) -> str:
    h = hashlib.sha256()
    async with aiofiles.open(path, "rb") as f:
        while True:
            chunk = await f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def human(n: int) -> str:
    units = ["B","KB","MB","GB","TB"]
    i = 0
    x = float(n)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024.0
        i += 1
    return f"{x:.2f} {units[i]}"

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    written = 0
    dest_path = safe_unique_path(file.filename)

    try:
        async with aiofiles.open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_FILE_BYTES:
                    await out.flush()
                    await out.close()
                    try:
                        os.remove(dest_path)
                    except FileNotFoundError:
                        pass
                    raise HTTPException(status_code=413, detail="Archivo demasiado grande")
                await out.write(chunk)
    finally:
        await file.close()

    digest = await sha256sum(dest_path)
    return {
        "filename": os.path.basename(dest_path),
        "bytes": written,
        "size_human": human(written),
        "sha256": digest
    }

@app.get("/api/list")
async def list_files():
    files = []
    for f in sorted(os.listdir(UPLOAD_DIR)):
        p = os.path.join(UPLOAD_DIR, f)
        if os.path.isfile(p):
            size = os.path.getsize(p)
            files.append({"name": f, "bytes": size, "size_human": human(size)})
    return {"files": files}

@app.get("/api/download/{name}")
async def download(name: str):
    safe = os.path.basename(name).strip().strip('"')
    path = os.path.join(UPLOAD_DIR, safe)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No encontrado")
    return FileResponse(path, media_type="application/octet-stream", filename=safe)

@app.get("/api/hash/{name}")
async def file_hash(name: str):
    safe = os.path.basename(name)
    path = os.path.join(UPLOAD_DIR, safe)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No encontrado")
    return {"name": safe, "sha256": await sha256sum(path)}
