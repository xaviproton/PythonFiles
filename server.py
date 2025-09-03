from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from dotenv import load_dotenv
import os, hashlib, shutil
import aiofiles

# ======= Config por .env =======
load_dotenv()  # lee variables de un archivo .env en el mismo directorio

BASE_DIR = Path(__file__).resolve().parent

def parse_size(value: str | None, default_bytes: int) -> int:
    """Convierte '500MB', '1GB', '1024KB', '123456' → bytes. Base 1024."""
    if not value:
        return default_bytes
    s = value.strip().lower().replace(" ", "")
    mult = 1
    for suf, m in (("kb", 1024), ("mb", 1024**2), ("gb", 1024**3), ("tb", 1024**4), ("b", 1)):
        if s.endswith(suf):
            s = s[: -len(suf)]
            mult = m
            break
    try:
        return int(float(s) * mult)
    except ValueError:
        return default_bytes

# Directorio de subidas
_upload_dir_env = os.getenv("UPLOAD_DIR")
UPLOAD_DIR = Path(_upload_dir_env) if _upload_dir_env else (BASE_DIR / "uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Límite de tamaño (0 o negativo = sin límite)
MAX_FILE_BYTES = parse_size(os.getenv("MAX_FILE_BYTES"), 5 * 1024**3)  # 5 GiB por defecto
# Tamaño del chunk
CHUNK_SIZE = max(64 * 1024, parse_size(os.getenv("CHUNK_SIZE"), 1 * 1024**2))  # ≥ 64 KiB

# Orígenes CORS
_origins_env = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost")
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]

# ======= App =======
app = FastAPI(title="FileBeam Backend", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======= Utils =======
def human(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    x = float(n)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024.0
        i += 1
    return f"{x:.2f} {units[i]}"

def safe_unique_path(filename: str) -> Path:
    """Evita traversal y auto-renombra si existe: name (1).ext, name (2).ext…"""
    base = Path(os.path.basename(filename or "archivo"))
    name = base.stem
    ext = base.suffix
    dest = UPLOAD_DIR / base.name
    i = 1
    while dest.exists():
        dest = UPLOAD_DIR / f"{name} ({i}){ext}"
        i += 1
    return dest

async def sha256sum(path: Path) -> str:
    h = hashlib.sha256()
    async with aiofiles.open(path, "rb") as f:
        while True:
            chunk = await f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

# ======= Endpoints =======
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    # 1) Validación rápida por Content-Length (si el cliente la envía)
    cl_header = request.headers.get("content-length")
    content_length = None
    if cl_header:
        try:
            content_length = int(cl_header)
        except ValueError:
            content_length = None

    if MAX_FILE_BYTES > 0 and content_length and content_length > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Archivo demasiado grande (Content-Length)")

    # 2) Verificación de espacio libre en disco
    #    Reservamos un margen de seguridad (64 MiB) para evitar quedarnos a cero.
    _, _, free = shutil.disk_usage(UPLOAD_DIR)
    expected = content_length or MAX_FILE_BYTES or 0
    safety_margin = 64 * 1024**2
    if expected > 0 and free < expected + safety_margin:
        raise HTTPException(status_code=507, detail="Espacio insuficiente en disco")

    # 3) Escritura por chunks
    written = 0
    dest_path = safe_unique_path(file.filename)

    try:
        async with aiofiles.open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)

                # Enforzar límite dinámico aunque no hubiera Content-Length
                if MAX_FILE_BYTES > 0 and written > MAX_FILE_BYTES:
                    await out.flush()
                    await out.close()
                    try:
                        dest_path.unlink(missing_ok=True)  # elimina parcial
                    except Exception:
                        pass
                    raise HTTPException(status_code=413, detail="Archivo demasiado grande")

                # Comprobar espacio libre durante la subida (por si se llena a mitad)
                _, _, free_now = shutil.disk_usage(UPLOAD_DIR)
                if free_now < safety_margin:
                    await out.flush()
                    await out.close()
                    try:
                        dest_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise HTTPException(status_code=507, detail="Espacio insuficiente en disco")
                await out.write(chunk)
    finally:
        await file.close()

    digest = await sha256sum(dest_path)
    return {
        "filename": dest_path.name,
        "bytes": written,
        "size_human": human(written),
        "sha256": digest
    }

@app.get("/api/list")
async def list_files():
    files = []
    for p in sorted(UPLOAD_DIR.iterdir()):
        if p.is_file():
            size = p.stat().st_size
            files.append({"name": p.name, "bytes": size, "size_human": human(size)})
    return {"files": files}

@app.get("/api/download/{name}")
async def download(name: str):
    # tolera espacios y comillas pegadas accidentalmente
    safe_name = os.path.basename(name).strip().strip('"').strip("'")
    path = UPLOAD_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="No encontrado")
    return FileResponse(str(path), media_type="application/octet-stream", filename=safe_name)

@app.get("/api/hash/{name}")
async def file_hash(name: str):
    safe_name = os.path.basename(name).strip().strip('"').strip("'")
    path = UPLOAD_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="No encontrado")
    return {"name": safe_name, "sha256": await sha256sum(path)}
